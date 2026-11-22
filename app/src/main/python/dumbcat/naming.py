"""文件名清洗、字节/章节号格式化、Retry-After 解析。

从旧的主模块拆出；manga_core 会重新导出这些名字。
"""
from __future__ import annotations

import email.utils
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

# 模块级常量随实现一起迁出，避免循环依赖。
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_path_component(value: Any, fallback: str, max_length: int = 100) -> str:
    text = unicodedata.normalize("NFC", str(value or "")).strip()
    text = INVALID_FILENAME_CHARS.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = fallback
    if text.upper() in WINDOWS_RESERVED_NAMES:
        text = f"_{text}"
    elif text.partition(".")[0].upper() in WINDOWS_RESERVED_NAMES:
        # Windows 的保留设备名对任意扩展名都生效（CON.jpg、NUL.txt 同样被拒），
        # 因此不能只做整串匹配。
        stem, dot, suffix = text.partition(".")
        text = f"_{stem}{dot}{suffix}"
    return text[:max_length].rstrip(" .") or fallback


def format_bytes(value: int) -> str:
    size = max(0, int(value))
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(size)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{size} B"


def chapter_number_value(chapter: dict[str, Any]) -> float:
    raw = chapter.get("number", chapter.get("chapter_number", 0))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def chapter_sort_key(chapter: dict[str, Any]) -> tuple[int, float, str]:
    raw = chapter.get("number", chapter.get("chapter_number", 0))
    try:
        return (0, float(raw), "")
    except (TypeError, ValueError):
        return (1, 0.0, str(raw))


def chapter_number_text(chapter: dict[str, Any]) -> str:
    number = chapter_number_value(chapter)
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def format_chapter_number(chapter: dict[str, Any]) -> str:
    number = chapter_number_value(chapter)
    if number.is_integer():
        return f"{int(number):04d}"
    return sanitize_path_component(f"{number:g}", "0000", 20)


def format_view_count(value: Any) -> str:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return "0"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}K"
    return str(number)


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(
            0.0,
            (retry_at - datetime.now(timezone.utc)).total_seconds(),
        )
