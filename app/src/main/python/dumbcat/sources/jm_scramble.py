"""禁漫（JM）图片切条还原。

站点会把每张图按某个条数切成横条再倒序拼接。条数随图变化（站点换过算法，
按官方客户端的 hash 公式硬套并不可靠），所以这里直接看图像自身的接缝特征：
乱序图在条与条的边界会有一次明显的"跳变"，还原正确的条数会让这些跳变消失。

判断不出来就原样返回，不会把正常图片弄坏。

实现只依赖 Pillow 与标准库（安卓 Chaquopy 环境没有 numpy）。为了让 band
判定与旧 numpy 版本保持一致，灰度图按行存成 ``bytes``：

- 切条/换条是对整行的搬运，直接做字节切片与拼接，等价于 numpy 的
  ``out[dst:dst+height] = arr[src:src+height]``；
- 行间落差用 ``ImageChops.difference`` + 逐行 ``sum`` 求，等价于
  ``np.abs(np.diff(gray, axis=0)).mean(axis=1)``。
"""

from __future__ import annotations

import hashlib
import io
import logging

__all__ = ["band_count", "detect_bands", "unscramble", "looks_scrambled"]

log = logging.getLogger("dumbcat.sources.jm_scramble")

_MAX_BANDS = 24

#: 检测时把图缩到的宽度：只缩宽度、保持高度（切条是横向的，纵向一降采样条界就对不上）
_GRAY_WIDTH = 200


def band_count(scramble_id: int | str, aid: int | str, filename: str) -> int:
    """按禁漫官方客户端的规则猜条数（只当候选，最终以检测结果为准）。"""
    try:
        sid = int(scramble_id)
        aidv = int(aid)
    except (TypeError, ValueError):
        return 0
    if aidv < sid:
        return 0
    if aidv < 268850:
        return 10
    x = 10 if aidv < 421926 else 8
    last = hashlib.md5(f"{aidv}{filename}".encode()).hexdigest()[-1]
    return (ord(last) % x) * 2 + 2


def _prepare(image, width: int = _GRAY_WIDTH):
    """灰度化并缩到检测宽度，返回 ``(按行排列的灰度 bytes, 宽, 高)``。"""
    from PIL import Image, ImageFilter

    small = image.convert("L")
    w, h = small.size
    if w > width:
        # 显式传 BICUBIC：与旧实现 ``resize((width, h))`` 的默认重采样一致，
        # 也避开 Pillow 9.3+ 才推荐的 Image.Resampling 枚举。
        small = small.resize((width, h), Image.BICUBIC)
    try:
        small = small.filter(ImageFilter.GaussianBlur(0.8))
    except Exception:
        pass
    return small.tobytes(), small.size[0], small.size[1]


def _jump_score(data: bytes, width: int, height: int) -> float:
    """最大的一批行落差：图被切过条时这些"断口"会明显偏大。"""
    from PIL import Image, ImageChops

    if height < 21:  # 旧实现：rows.shape[0] = height - 1，少于 20 直接 0
        return 0.0
    image = Image.frombytes("L", (width, height), data)
    # offset(0, 1) 让每一行与上一行对齐（第 0 行是循环回来的底行，丢弃）；
    # difference 后第 y 行就是 |row[y] - row[y-1]|。
    diff = ImageChops.difference(image, ImageChops.offset(image, 0, 1))
    raw = memoryview(diff.tobytes())
    rows = [
        sum(raw[y * width:(y + 1) * width]) / width
        for y in range(1, height)
    ]
    rows.sort()
    take = 15
    return sum(rows[-take:]) / take


def _decode_bands(data: bytes, width: int, height: int, num: int) -> bytes:
    """按 num 条做「底条翻到顶」的还原（字节版，等价于旧 _decode_arrays）。"""
    over = height % num
    step = height // num
    if step <= 0:
        return data
    parts: list[bytes] = []
    for i in range(num):
        band_height = step + over if i == 0 else step
        src = height - step * (i + 1) - over
        parts.append(data[src * width:(src + band_height) * width])
    return b"".join(parts)


def detect_bands(image) -> int:
    """检测一张图被切成了几条；0 = 看不出切割（原样即可）。"""
    try:
        data, width, height = _prepare(image)
    except Exception:
        # 安卓上曾因缺少依赖在这里被静默吞掉；失败必须能从 logcat 看到。
        log.warning("禁漫切条检测：灰度预处理失败，按原图处理", exc_info=True)
        return 0
    if height < 60:
        return 0

    base = _jump_score(data, width, height)
    if base <= 1e-6:
        return 0
    best_num, best_score = 0, base
    for num in range(2, _MAX_BANDS + 1, 2):
        if height < num * 8:
            continue
        score = _jump_score(_decode_bands(data, width, height, num), width, height)
        if score < best_score:
            best_num, best_score = num, score
    # 必须把断口明显抹平才算数，避免把正常图片切坏
    if best_num and best_score < base * 0.85:
        return best_num
    return 0


def _decode_image(image, num: int):
    """把整张图按 num 条重排（原分辨率，像素级等价于旧 numpy 实现）。"""
    from PIL import Image

    width, height = image.size
    over = height % num
    step = height // num
    out = Image.new(image.mode, (width, height))
    for i in range(num):
        band_height = step + over if i == 0 else step
        src = height - step * (i + 1) - over
        dst = 0 if i == 0 else step * i + over
        band = image.crop((0, src, width, src + band_height))
        out.paste(band, (0, dst))
    return out


def _reencode(image, fmt: str) -> bytes:
    buf = io.BytesIO()
    fmt = (fmt or "WEBP").upper()
    if fmt in {"JPG", "JPEG"}:
        image.convert("RGB").save(buf, "JPEG", quality=92, optimize=True)
    elif fmt == "PNG":
        image.save(buf, "PNG", optimize=True)
    else:
        from dumbcat import android_webp

        if android_webp.webp_writable():
            image.save(buf, "WEBP", quality=90, method=4)
        else:  # 安卓 Chaquopy 的 Pillow 没编 webp 编码器，退回 JPEG
            image.convert("RGB").save(buf, "JPEG", quality=92, optimize=True)
    return buf.getvalue()


def unscramble(data: bytes, num: int | None = None) -> bytes:
    """把切条乱序的图片还原；num 为 0/None 时自动检测，检测不到就原样返回。"""
    try:
        from dumbcat import android_webp

        # 禁漫原图是 WebP：安卓上 Chaquopy 版 Pillow 没有 libwebp，这里会借系统
        # 解码器转成 PNG 再继续（桌面 Pillow 自带 webp，走上面直通分支）。
        image, bridged = android_webp.open_image(data)
        fmt = "JPEG" if bridged else (image.format or "WEBP")
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        if num is None:
            num = detect_bands(image)
        if not num or num < 2 or image.size[1] < num * 4:
            return data
        fixed = _decode_image(image, int(num))
        return _reencode(fixed, fmt)
    except Exception:
        # 任何异常都退回原图，但必须留下日志（安卓 logcat 可查）。
        log.warning("禁漫切条还原失败，按原图返回", exc_info=True)
        return data


def looks_scrambled(data: bytes) -> int:
    """只看不还原：返回检测到的条数（0 = 看不出切割）。"""
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(data))
        image.load()
        return detect_bands(image)
    except Exception:
        log.warning("禁漫切条检测（looks_scrambled）失败，按未切条处理", exc_info=True)
        return 0
