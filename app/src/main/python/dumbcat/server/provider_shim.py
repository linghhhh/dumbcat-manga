"""把插件适配成旧下载器认识的 ``SeriesProvider``。

为什么复用旧下载器：11k 行代码里最值钱的就是那套「并发 + .part 原子替换 +
跳过已完整章节 + 付费章节识别」的管线，重写只会引入新 bug。这里做的是适配，
不是分叉。
"""
from __future__ import annotations

import os
import threading
import urllib.parse
from pathlib import Path
from typing import Any

from dumbcat.naming import sanitize_path_component
from dumbcat.sources.base import SeriesProvider
from dumbcat.server.registry import SourceAdapter


class PluginProviderShim(SeriesProvider):
    """让 ``adapter``（用户插件）满足 :class:`SeriesProvider` 的最小接口。"""

    def __init__(self, adapter: SourceAdapter) -> None:
        # 不走父类 __init__：插件有自己的 HTTP 客户端，不需要 ApiClient
        self.adapter = adapter
        self.key = adapter.key
        self.display_name = adapter.name
        self.output_prefix = ()
        self.browse_page_size = 30
        self.api = None  # type: ignore[assignment]

    def site_base_url(self) -> str:
        return self.adapter.info.base_url

    def decorate_series(self, series: dict[str, Any]) -> dict[str, Any]:
        item = dict(series)
        item["_provider"] = self.key
        item["_source_label"] = self.display_name
        return item

    def decorate_chapter(self, series: dict[str, Any], chapter: dict[str, Any]) -> dict[str, Any]:
        item = dict(chapter)
        item["_provider"] = self.key
        item["_source_label"] = self.display_name
        return item

    def get_chapters(self, series: dict[str, Any],
                     cancel_event: threading.Event | None = None) -> list[dict[str, Any]]:
        return self.adapter.chapters(series)

    def get_chapter_pages(
        self,
        series: dict[str, Any],
        chapter: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        return self.adapter.pages(series, chapter)

    def chapter_referer(self, series: dict[str, Any], chapter: dict[str, Any]) -> str:
        return self.adapter.referer(series, chapter)

    def fetch_image_bytes(self, url: str, referer: str = "") -> bytes:
        return self.adapter.fetch_image(url, referer)

    def download_file(
        self,
        url: str,
        destination: Path,
        referer: str = "",
        cancel_event: threading.Event | None = None,
    ) -> str:
        destination = Path(destination)
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled"
        try:
            if destination.is_file() and destination.stat().st_size > 0:
                return "skipped"
        except OSError:
            pass
        data = self.adapter.fetch_image(url, referer)
        if not data:
            raise RuntimeError("图片为空")
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(
            destination.name + f".{os.getpid()}.{threading.get_ident()}.part"
        )
        try:
            tmp.write_bytes(data)
            os.replace(tmp, destination)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return "downloaded"

    def download_native(self, series, chapters, output_root, emit, cancel_event):
        return None

    def cleanup_chapter_cache(self) -> int:
        return 0

    def output_root(self, base: Path, series: dict[str, Any]) -> Path:
        # 插件不额外加层级；显示名本身已经是目录名
        return Path(base)

    def close(self) -> None:
        pass


def series_folder_name(series: dict[str, Any]) -> str:
    return sanitize_path_component(
        series.get("title") or series.get("slug") or "未命名作品", "未命名作品", 90
    )


def safe_join(base: Path, *parts: str) -> Path:
    """拼路径并确保没有跳出 base（章节标题来自站点，必须当作不可信数据）。"""
    root = Path(base).resolve()
    target = root
    for part in parts:
        target = target / sanitize_path_component(part, "x", 120)
    try:
        target.resolve().relative_to(root)
    except ValueError as exc:
        raise RuntimeError("非法输出路径") from exc
    return target


def url_file_name(url: str, fallback: str = "image.jpg") -> str:
    name = Path(urllib.parse.urlparse(url).path).name
    return sanitize_path_component(name, fallback, 80)
