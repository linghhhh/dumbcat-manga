"""封面 / 漫画页的本地磁盘缓存。

图源大多防盗链（Referer、Cookie），浏览器直连会 403，所以图片一律由后端代理：
先查磁盘缓存，没有就带正确的 Referer 抓一次再落盘。这样翻页、回退都很快，
也不会把站点打疼。
"""
from __future__ import annotations

import hashlib
import io
import os
import threading
import time
from pathlib import Path
from typing import Callable

from dumbcat.server.config import IMAGE_CACHE_DIR, load_settings

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),
    (b"BM", "image/bmp"),
)


def sniff_mime(data: bytes) -> str:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    head = data[:200].lstrip().lower()
    if head.startswith(b"<svg") or b"<svg" in head:
        return "image/svg+xml"
    return "application/octet-stream"


def make_thumb(data: bytes, width: int) -> bytes | None:
    """把图片等比缩到指定宽度，返回缩略图字节；缩不了（或缺 Pillow）返回 None。

    列表页一屏十几种封面，原图动辄几百 KB 到几 MB，缩略图能省掉一个数量级的
    传输量；解码成本留在后端一次，之后都命中磁盘缓存。

    安卓上 Chaquopy 版 Pillow 没有 libwebp：解码走 `dumbcat.android_webp` 的系统
    解码器兜底，编码在没有 webp 编码器时退回 JPEG（封面原图多是 WebP）。
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - 没装 Pillow 就退回原图，不影响功能
        return None
    try:
        from dumbcat import android_webp

        img, _bridged = android_webp.open_image(data)
    except Exception:  # noqa: BLE001 - 坏图 / 格式不支持时退回原图
        return None
    try:
        if getattr(img, "is_animated", False):
            return None
        if img.width <= width:
            return None
        ratio = width / float(img.width)
        height = max(1, int(round(img.height * ratio)))
        target = img.convert("RGB") if img.mode not in ("RGB", "L") else img
        resized = target.resize((width, height), Image.LANCZOS)
        out = io.BytesIO()
        try:
            from dumbcat import android_webp as _aw

            webp_ok = _aw.webp_writable()
        except Exception:  # noqa: BLE001
            webp_ok = True
        if webp_ok:
            resized.save(out, format="WEBP", quality=82, method=4)
        else:  # 安卓 Chaquopy 的 Pillow 没编 webp 编码器
            resized.convert("RGB").save(out, format="JPEG", quality=82, optimize=True)
        return out.getvalue()
    except Exception:  # noqa: BLE001 - 坏图 / 格式不支持时退回原图
        return None
    finally:
        try:
            img.close()
        except Exception:  # noqa: BLE001
            pass


def _ext(mime: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/svg+xml": ".svg",
    }.get(mime, ".bin")


class ImageCache:
    # 缓存键前缀：解码/缩略图策略一变就改这里，让老版本留下的缓存整体作废。
    # v2 = 3.2.1 起「WebP 走系统解码、缩略图可能是 JPEG」——3.2.0 缓存的
    # 乱序 WebP 页与旧缩略图因此不会被继续命中。
    SCHEMA = "v2"

    def __init__(self, root: Path = IMAGE_CACHE_DIR) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._inflight: dict[str, threading.Lock] = {}
        self._last_prune = 0.0

    # ---------- 键与路径 ----------
    @staticmethod
    def key(url: str, referer: str = "") -> str:
        raw = f"{ImageCache.SCHEMA}\n{url}\n{referer}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def path_for(self, key: str) -> Path | None:
        folder = self.root / key[:2]
        try:
            if not folder.exists():
                return None
            for child in folder.iterdir():
                if child.name.startswith(key):
                    return child
        except OSError:
            return None
        return None

    def read(self, key: str) -> tuple[bytes, str] | None:
        path = self.path_for(key)
        if path is None:
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if not data:
            return None
        mime = sniff_mime(data)
        # 顺手更新 mtime，作为 LRU 依据
        try:
            os.utime(path, None)
        except OSError:
            pass
        return data, mime

    def write(self, key: str, data: bytes) -> str:
        mime = sniff_mime(data)
        folder = self.root / key[:2]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{key}{_ext(mime)}"
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        self._maybe_prune()
        return mime

    # ---------- 取图（带合并的同键并发） ----------
    def get_or_fetch(
        self,
        url: str,
        referer: str,
        fetcher: Callable[[], bytes],
    ) -> tuple[bytes, str]:
        key = self.key(url, referer)
        cached = self.read(key)
        if cached is not None:
            return cached
        with self._lock:
            guard = self._inflight.get(key)
            if guard is None:
                guard = threading.Lock()
                self._inflight[key] = guard
                if len(self._inflight) > 512:
                    # 只留正在用的锁，避免长时间运行后字典膨胀
                    self._inflight = {
                        k: v for k, v in self._inflight.items() if v.locked()
                    }
        # 同一张图只让一个线程去抓，其余在锁上等，醒来后直接命中缓存
        with guard:
            cached = self.read(key)
            if cached is not None:
                return cached
            data = fetcher()
            if not data:
                raise ValueError("空图片")
            mime = self.write(key, data)
            return data, mime

    # ---------- 缩略图 ----------
    def thumb_key(self, url: str, referer: str, width: int) -> str:
        raw = "%s|%s|%s|w%d" % (self.SCHEMA, url, referer, int(width))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def get_thumb(
        self,
        url: str,
        referer: str,
        width: int,
        fetcher: Callable[[], bytes],
    ) -> tuple[bytes, str]:
        """取（或第一次生成）宽度不超过 width 的缩略图；生成失败退回原图。"""
        width = int(width or 0)
        if width <= 0:
            return self.get_or_fetch(url, referer, fetcher)
        width = max(64, min(width, 1600))
        tkey = self.thumb_key(url, referer, width)
        cached = self.read(tkey)
        if cached is not None:
            return cached
        data, mime = self.get_or_fetch(url, referer, fetcher)
        # 同一张缩略图只让一个线程去压缩，其余等它写好缓存
        with self._lock:
            guard = self._inflight.get(tkey)
            if guard is None:
                guard = threading.Lock()
                self._inflight[tkey] = guard
        with guard:
            cached = self.read(tkey)
            if cached is not None:
                return cached
            small = make_thumb(data, width)
            if small is None:
                return data, mime
            small_mime = self.write(tkey, small)
            return small, small_mime

    # ---------- 清理 ----------
    def _maybe_prune(self) -> None:
        now = time.time()
        if now - self._last_prune < 120:
            return
        self._last_prune = now
        limit_mb = load_settings().image_cache_mb
        if limit_mb <= 0:
            return
        self.prune(limit_mb)

    def prune(self, limit_mb: int) -> int:
        """按 mtime 从旧到新删，直到总占用低于上限。返回删除数量。"""
        budget = limit_mb * 1024 * 1024
        files: list[tuple[float, int, Path]] = []
        total = 0
        for folder in self.root.iterdir():
            if not folder.is_dir():
                continue
            for child in folder.iterdir():
                try:
                    stat = child.stat()
                except OSError:
                    continue
                if not child.is_file():
                    continue
                total += stat.st_size
                files.append((stat.st_mtime, stat.st_size, child))
        if total <= budget:
            return 0
        files.sort(key=lambda item: item[0])
        removed = 0
        for _mtime, size, path in files:
            if total <= budget:
                break
            try:
                path.unlink()
                total -= size
                removed += 1
            except OSError:
                continue
        return removed

    def size_bytes(self) -> int:
        total = 0
        try:
            for folder in self.root.iterdir():
                if not folder.is_dir():
                    continue
                for child in folder.iterdir():
                    try:
                        total += child.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
        return total

    def clear(self) -> int:
        removed = 0
        try:
            for folder in self.root.iterdir():
                if not folder.is_dir():
                    continue
                for child in folder.iterdir():
                    try:
                        child.unlink()
                        removed += 1
                    except OSError:
                        pass
        except OSError:
            pass
        return removed


image_cache = ImageCache()
