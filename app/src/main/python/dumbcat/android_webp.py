"""安卓上借系统解码器读 WebP（Chaquopy → android.graphics.BitmapFactory）。

Chaquopy 打包的 Pillow 没有编 libwebp：`Image.open()` 一碰到禁漫天堂的 WebP 原图就抛
`UnidentifiedImageError`，于是切条还原和封面缩略图都会静默退化成原图。这里用 Android
自带的 BitmapFactory 把 WebP 解成 PNG 字节，再交给原来那套纯 Pillow 实现处理。

桌面（CPython）上没有 `java` 模块，`available()` 返回 False，调用方自然走原路径。
"""
from __future__ import annotations

import io
import logging
import os
import tempfile

log = logging.getLogger(__name__)

_state: dict = {}


def _android():
    """返回 (BitmapFactory, CompressFormat) 或 None（非安卓环境）。"""
    if _state.get("done"):
        return _state.get("api")
    api = None
    try:
        from java import jclass  # type: ignore[import-not-found]  # 仅 Chaquopy 提供

        api = (
            jclass("android.graphics.BitmapFactory"),
            jclass("android.graphics.Bitmap$CompressFormat"),
        )
    except Exception:  # noqa: BLE001 - 桌面没有 java 模块，属正常情况
        api = None
    _state["done"] = True
    _state["api"] = api
    return api


def available() -> bool:
    """当前环境能不能借系统解码器解 WebP。"""
    return _android() is not None


def webp_writable() -> bool:
    """Pillow 能不能写 WebP（Chaquopy 版同样没有编码器）。"""
    try:
        from PIL import features

        return bool(features.check("webp"))
    except Exception:  # noqa: BLE001
        return False


def _bitmap_to_bytes(bitmap, CompressFormat, fmt_name: str = "PNG") -> bytes | None:
    """Bitmap → (PNG|JPEG) 字节。直接写临时文件，不依赖 jarray ↔ bytes 转换。"""
    try:
        from java import jclass  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return None

    fmt = getattr(CompressFormat, fmt_name)
    quality = 100 if fmt_name == "PNG" else 92
    fd, tmp = tempfile.mkstemp(suffix=".imgout", prefix="dcout_")
    os.close(fd)
    try:
        stream = jclass("java.io.FileOutputStream")(tmp)
        try:
            ok = bool(bitmap.compress(fmt, quality, stream))
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        if not ok:
            return None
        with open(tmp, "rb") as fh:
            return fh.read()
    except Exception:  # noqa: BLE001
        log.warning("Bitmap 转字节失败", exc_info=True)
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def decode(data: bytes, fmt_name: str = "PNG") -> bytes | None:
    """用系统解码器把图片（含 WebP）转成 PNG/JPEG 字节；失败返回 None。"""
    api = _android()
    if api is None or not data:
        return None
    BitmapFactory, CompressFormat = api
    tmp = None
    bitmap = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".img", prefix="dcdec_")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        bitmap = BitmapFactory.decodeFile(tmp)
        if bitmap is None:
            log.warning("系统解码器也解不开这张图（%d 字节）", len(data))
            return None
        return _bitmap_to_bytes(bitmap, CompressFormat, fmt_name)
    except Exception:  # noqa: BLE001
        log.warning("借系统解码器解图失败", exc_info=True)
        return None
    finally:
        if bitmap is not None:
            try:
                bitmap.recycle()
            except Exception:  # noqa: BLE001
                pass
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def open_image(data: bytes):
    """打开图片，返回 (PIL.Image, 是否借了系统解码器)。Pillow 失败才走系统解码。

    调用方负责 `close()`；借系统解码时返回的是 PNG/JPEG 副本，跟原始格式无关。
    """
    import warnings

    from PIL import Image

    try:
        with warnings.catch_warnings():
            # 安卓 Pillow 没编 webp，探测格式时会打 UserWarning；功能不受影响，
            # 但每页都刷一条日志太吵，这里静音（后面会走系统解码器兜底）。
            warnings.simplefilter("ignore")
            img = Image.open(io.BytesIO(data))
            img.load()
        return img, False
    except Exception:  # noqa: BLE001 - 极可能就是缺 WebP 解码
        if not available():
            raise
    png = decode(data, "PNG")
    if png is None:
        raise ValueError("图片解码失败：本机 Pillow 与系统解码器都没成功")
    img = Image.open(io.BytesIO(png))
    img.load()
    return img, True
