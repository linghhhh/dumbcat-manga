"""作品 / 章节 / 页面，以及取图需要的 Referer。

前端只拿得到 ``uid``，真实数据留在这里——这样详情页刷新不用重新抓一遍，
图片代理也才知道该用哪个图源、带什么 Referer。

P1 起数据落在 SQLite（:mod:`dumbcat.server.db`），本类只保留两层 LRU 作为读
缓存：进程重启、容量淘汰、图源被关掉之后，书架和历史仍然能靠库里的快照打开。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from dumbcat.server import db

log = logging.getLogger("dumbcat.store")


@dataclass
class SeriesRecord:
    uid: str
    provider: str
    provider_name: str
    raw: dict[str, Any]
    public: dict[str, Any]
    base_url: str = ""
    nsfw: bool = False
    created_at: float = field(default_factory=time.time)
    chapters_at: float = 0.0


@dataclass
class ChapterList:
    series_uid: str
    items: list[dict[str, Any]]
    raw_items: list[dict[str, Any]] = field(default_factory=list)
    at: float = field(default_factory=time.time)


@dataclass
class PageList:
    chapter_uid: str
    series_uid: str
    items: list[dict[str, Any]]
    referer: str = ""
    at: float = field(default_factory=time.time)


def series_of_chapter(chapter_uid: str) -> str:
    """章节 uid = ``<图源>.<作品摘要>.<章节摘要>``。"""
    return chapter_uid.rsplit(".", 1)[0] if "." in chapter_uid else chapter_uid


def _pages_file(chapter_uid: str):
    """旧版章节页列表的落盘路径（只用于迁移读取）。"""
    from dumbcat.server.config import PAGES_CACHE_DIR

    digest = hashlib.sha1(chapter_uid.encode("utf-8")).hexdigest()[:24]
    return PAGES_CACHE_DIR / f"{digest}.json"


def _load_legacy_pages(chapter_uid: str) -> "PageList | None":
    try:
        path = _pages_file(chapter_uid)
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    items = raw.get("items")
    if not isinstance(items, list) or not items:
        return None
    return PageList(
        str(raw.get("chapterUid") or chapter_uid),
        str(raw.get("seriesUid") or series_of_chapter(chapter_uid)),
        [item for item in items if isinstance(item, dict)],
        str(raw.get("referer") or ""),
        float(raw.get("at") or 0.0),
    )


def _row_to_series(row) -> SeriesRecord:
    return SeriesRecord(
        uid=str(row["uid"]),
        provider=str(row["provider"]),
        provider_name=str(row["provider_name"] or ""),
        raw=db.loads(row["raw"], {}) or {},
        public=db.loads(row["public"], {}) or {},
        base_url=str(row["base_url"] or ""),
        nsfw=bool(row["nsfw"]),
        created_at=float(row["created_at"] or 0.0),
        chapters_at=float(row["chapters_at"] or 0.0),
    )


class Store:
    def __init__(self, max_series: int = 1500, max_pages: int = 400) -> None:
        self._lock = threading.RLock()
        self._series: OrderedDict[str, SeriesRecord] = OrderedDict()
        self._chapters: OrderedDict[str, ChapterList] = OrderedDict()
        self._pages: OrderedDict[str, PageList] = OrderedDict()
        self._max_series = max_series
        self._max_pages = max_pages
        self._db_ready = False

    # ------------------------------------------------------------------ 内部
    def _database(self):
        """按需建库建表；失败时退化为纯内存（不阻断抓取）。"""
        if self._db_ready:
            return True
        try:
            with db.lock():
                db.conn()
            self._db_ready = True
            return True
        except Exception as exc:  # pragma: no cover - 极端环境
            log.warning("数据库不可用，退化为内存缓存：%s", exc)
            return False

    def _trim_series(self) -> None:
        while len(self._series) > self._max_series:
            self._series.popitem(last=False)

    def _trim_pages(self) -> None:
        while len(self._pages) > self._max_pages:
            self._pages.popitem(last=False)

    def _write_series(self, record: SeriesRecord) -> None:
        if not self._database():
            return
        try:
            now = time.time()
            db.execute(
                "INSERT INTO series (uid, provider, provider_name, base_url, nsfw, raw, public,"
                " created_at, chapters_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(uid) DO UPDATE SET provider=excluded.provider,"
                " provider_name=excluded.provider_name, base_url=excluded.base_url,"
                " nsfw=excluded.nsfw, raw=excluded.raw, public=excluded.public,"
                " chapters_at=excluded.chapters_at, updated_at=excluded.updated_at",
                (
                    record.uid,
                    record.provider,
                    record.provider_name,
                    record.base_url,
                    int(record.nsfw),
                    db.dumps(record.raw),
                    db.dumps(record.public),
                    float(record.created_at or now),
                    float(record.chapters_at or 0.0),
                    now,
                ),
            )
        except Exception as exc:  # pragma: no cover - 坏盘
            log.warning("写入作品失败 %s：%s", record.uid, exc)

    # ---------- 作品 ----------
    def put_series(self, record: SeriesRecord) -> SeriesRecord:
        with self._lock:
            self._series[record.uid] = record
            self._series.move_to_end(record.uid)
            self._trim_series()
        self._write_series(record)
        return record

    def get_series(self, uid: str) -> SeriesRecord | None:
        with self._lock:
            record = self._series.get(uid)
            if record is not None:
                self._series.move_to_end(uid)
                return record
        if not self._database():
            return None
        try:
            row = db.query_one("SELECT * FROM series WHERE uid=?", (uid,))
        except Exception as exc:  # pragma: no cover
            log.warning("读取作品失败 %s：%s", uid, exc)
            return None
        if row is None:
            return None
        record = _row_to_series(row)
        with self._lock:
            self._series[uid] = record
            self._series.move_to_end(uid)
            self._trim_series()
        return record

    def update_series_public(self, uid: str, patch: dict[str, Any]) -> None:
        clean = {k: v for k, v in patch.items() if v is not None}
        if not clean:
            return
        with self._lock:
            record = self._series.get(uid)
        if record is None:
            record = self.get_series(uid)
        if record is None:
            return
        record.public.update(clean)
        self._write_series(record)

    # ---------- 章节 ----------
    def put_chapters(
        self,
        series_uid: str,
        items: list[dict[str, Any]],
        raw_items: list[dict[str, Any]] | None = None,
    ) -> None:
        raw_list = list(raw_items or [])
        entry = ChapterList(series_uid, items, raw_list)
        with self._lock:
            self._chapters[series_uid] = entry
            self._chapters.move_to_end(series_uid)
        if not self._database():
            return
        now = time.time()
        try:
            with db.lock(), db.transaction() as connection:
                connection.execute("DELETE FROM chapters WHERE series_uid=?", (series_uid,))
                rows = []
                for position, item in enumerate(items):
                    if not isinstance(item, dict):
                        continue
                    raw = raw_list[position] if position < len(raw_list) else None
                    rows.append(
                        (
                            series_uid,
                            position,
                            str(item.get("uid") or ""),
                            db.dumps(item),
                            db.dumps(raw) if isinstance(raw, dict) else None,
                        )
                    )
                if rows:
                    connection.executemany(
                        "INSERT INTO chapters (series_uid, position, uid, public, raw)"
                        " VALUES (?,?,?,?,?)",
                        rows,
                    )
                connection.execute(
                    "UPDATE series SET chapters_at=?, updated_at=? WHERE uid=?",
                    (now, now, series_uid),
                )
            with self._lock:
                record = self._series.get(series_uid)
                if record is not None:
                    record.chapters_at = now
        except Exception as exc:  # pragma: no cover
            log.warning("写入章节失败 %s：%s", series_uid, exc)

    def _load_chapters(self, series_uid: str) -> ChapterList | None:
        if not self._database():
            return None
        try:
            rows = db.query_all(
                "SELECT position, public, raw FROM chapters WHERE series_uid=? ORDER BY position",
                (series_uid,),
            )
            if not rows:
                return None
            series_row = db.query_one("SELECT chapters_at FROM series WHERE uid=?", (series_uid,))
            at = float(series_row["chapters_at"] or 0.0) if series_row is not None else 0.0
        except Exception as exc:  # pragma: no cover
            log.warning("读取章节失败 %s：%s", series_uid, exc)
            return None
        items: list[dict[str, Any]] = []
        raw_items: list[dict[str, Any]] = []
        for row in rows:
            public = db.loads(row["public"], {}) or {}
            if not isinstance(public, dict):
                continue
            items.append(public)
            raw = db.loads(row["raw"], None)
            raw_items.append(raw if isinstance(raw, dict) else {})
        if not items:
            return None
        entry = ChapterList(series_uid, items, raw_items, at or time.time())
        with self._lock:
            self._chapters[series_uid] = entry
            self._chapters.move_to_end(series_uid)
        return entry

    def get_chapters(self, series_uid: str, ttl: float = 3600 * 6) -> ChapterList | None:
        with self._lock:
            entry = self._chapters.get(series_uid)
        if entry is None:
            entry = self._load_chapters(series_uid)
            if entry is None:
                return None
        if ttl and time.time() - entry.at > ttl:
            return None
        with self._lock:
            self._chapters.move_to_end(series_uid)
        return entry

    def find_chapter(self, series_uid: str, chapter_uid: str) -> dict[str, Any] | None:
        entry = self.get_chapters(series_uid, ttl=0)
        if entry is None:
            return None
        for index, item in enumerate(entry.items):
            if item.get("uid") == chapter_uid:
                if index < len(entry.raw_items):
                    raw = entry.raw_items[index]
                    if isinstance(raw, dict) and raw:
                        return raw
                return item
        return None

    def find_chapter_public(self, series_uid: str, chapter_uid: str) -> dict[str, Any] | None:
        entry = self.get_chapters(series_uid, ttl=0)
        if entry is None:
            return None
        for item in entry.items:
            if item.get("uid") == chapter_uid:
                return item
        return None

    # ---------- 页面 ----------
    def put_pages(self, entry: PageList) -> None:
        with self._lock:
            self._pages[entry.chapter_uid] = entry
            self._pages.move_to_end(entry.chapter_uid)
            self._trim_pages()
        if not self._database():
            return
        try:
            db.execute(
                "INSERT INTO chapter_pages (chapter_uid, series_uid, referer, items, at)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(chapter_uid) DO UPDATE SET series_uid=excluded.series_uid,"
                " referer=excluded.referer, items=excluded.items, at=excluded.at",
                (
                    entry.chapter_uid,
                    entry.series_uid,
                    entry.referer,
                    db.dumps(entry.items),
                    float(entry.at or time.time()),
                ),
            )
        except Exception as exc:  # pragma: no cover
            log.warning("写入图片列表失败 %s：%s", entry.chapter_uid, exc)

    def _load_pages(self, chapter_uid: str) -> PageList | None:
        if not self._database():
            return None
        try:
            row = db.query_one("SELECT * FROM chapter_pages WHERE chapter_uid=?", (chapter_uid,))
        except Exception as exc:  # pragma: no cover
            log.warning("读取图片列表失败 %s：%s", chapter_uid, exc)
            return None
        if row is not None:
            items = db.loads(row["items"], []) or []
            if not isinstance(items, list) or not items:
                return None
            entry = PageList(
                chapter_uid,
                str(row["series_uid"] or series_of_chapter(chapter_uid)),
                [item for item in items if isinstance(item, dict)],
                str(row["referer"] or ""),
                float(row["at"] or 0.0),
            )
            return entry
        # 旧版本把图片列表写在 pages/*.json 里，读到顺手迁移进库
        legacy = _load_legacy_pages(chapter_uid)
        if legacy is not None:
            self.put_pages(legacy)
        return legacy

    def get_pages(self, chapter_uid: str, ttl: float = 3600 * 3) -> PageList | None:
        with self._lock:
            entry = self._pages.get(chapter_uid)
        if entry is None:
            entry = self._load_pages(chapter_uid)
            if entry is None:
                return None
            with self._lock:
                self._pages[chapter_uid] = entry
                self._pages.move_to_end(chapter_uid)
                self._trim_pages()
        if ttl and time.time() - entry.at > ttl:
            # 过期不丢：离线时（图源连不上）还要靠它把看过的章节打开，
            # 与 P0 之前「过期后从磁盘再读一次」的语义保持一致。
            log.debug("图片列表已过期，继续使用缓存：%s", chapter_uid)
            return entry
        with self._lock:
            self._pages.move_to_end(chapter_uid)
        return entry

    # ---------- 维护 ----------
    def clear(self) -> None:
        """只清内存缓存；库里的长期数据不动。"""
        with self._lock:
            self._series.clear()
            self._chapters.clear()
            self._pages.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            memory = {
                "series": len(self._series),
                "chapters": len(self._chapters),
                "pages": len(self._pages),
            }
        if not self._database():
            return memory
        try:
            for key, table in (("series", "series"), ("chapters", "chapters"), ("pages", "chapter_pages")):
                row = db.query_one(f"SELECT COUNT(*) AS n FROM {table}")
                memory["db_" + key] = int(row["n"] if row is not None else 0)
        except Exception:  # pragma: no cover
            pass
        return memory


store = Store()
