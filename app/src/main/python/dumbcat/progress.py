"""阅读进度：记在 SQLite 里，卸载重装/换设备之外都能接上。

以前进度只存在 WebView 的 ``localStorage`` 里，清缓存或换壳就丢；现在前端每次
上报都写库，打开章节时先问一次服务端。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from dumbcat.server import db

log = logging.getLogger("dumbcat.progress")


def save(series_uid: str, chapter_uid: str, page: int = 0, offset_ratio: float = 0.0) -> dict[str, Any]:
    series_uid = str(series_uid or "").strip()
    chapter_uid = str(chapter_uid or "").strip()
    if not series_uid or not chapter_uid:
        raise ValueError("缺少 seriesUid / chapterUid")
    ratio = max(0.0, min(1.0, float(offset_ratio or 0.0)))
    page_value = max(0, int(page or 0))
    with db.lock(), db.transaction() as connection:
        connection.execute(
            "INSERT INTO progress (series_uid, chapter_uid, page, offset_ratio, updated_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(series_uid) DO UPDATE SET chapter_uid=excluded.chapter_uid,"
            " page=excluded.page, offset_ratio=excluded.offset_ratio,"
            " updated_at=excluded.updated_at",
            (series_uid, chapter_uid, page_value, ratio, time.time()),
        )
    return {
        "seriesUid": series_uid,
        "chapterUid": chapter_uid,
        "page": page_value,
        "offsetRatio": ratio,
    }


def get(series_uid: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM progress WHERE series_uid=?", (str(series_uid),))
    if row is None:
        return None
    return {
        "seriesUid": str(row["series_uid"]),
        "chapterUid": str(row["chapter_uid"]),
        "page": int(row["page"] or 0),
        "offsetRatio": float(row["offset_ratio"] or 0.0),
        "updatedAt": float(row["updated_at"] or 0.0),
    }


def clear(series_uid: str = "") -> int:
    if series_uid:
        cursor = db.execute("DELETE FROM progress WHERE series_uid=?", (str(series_uid),))
    else:
        cursor = db.execute("DELETE FROM progress")
    return int(cursor.rowcount or 0)
