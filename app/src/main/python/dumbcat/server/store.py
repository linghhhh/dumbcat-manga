"""进程内数据仓库：作品 / 章节 / 页面，以及取图需要的 Referer。

前端只拿得到 ``uid``，真实数据留在这里——这样详情页刷新不用重新抓一遍，
图片代理也才知道该用哪个图源、带什么 Referer。容量有上限，超了按最久未用淘汰。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any


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


class Store:
    def __init__(self, max_series: int = 1500, max_pages: int = 400) -> None:
        self._lock = threading.RLock()
        self._series: OrderedDict[str, SeriesRecord] = OrderedDict()
        self._chapters: OrderedDict[str, ChapterList] = OrderedDict()
        self._pages: OrderedDict[str, PageList] = OrderedDict()
        self._max_series = max_series
        self._max_pages = max_pages

    # ---------- 作品 ----------
    def put_series(self, record: SeriesRecord) -> SeriesRecord:
        with self._lock:
            self._series[record.uid] = record
            self._series.move_to_end(record.uid)
            while len(self._series) > self._max_series:
                self._series.popitem(last=False)
        return record

    def get_series(self, uid: str) -> SeriesRecord | None:
        with self._lock:
            record = self._series.get(uid)
            if record is not None:
                self._series.move_to_end(uid)
            return record

    def update_series_public(self, uid: str, patch: dict[str, Any]) -> None:
        with self._lock:
            record = self._series.get(uid)
            if record is not None:
                record.public.update({k: v for k, v in patch.items() if v is not None})

    # ---------- 章节 ----------
    def put_chapters(
        self,
        series_uid: str,
        items: list[dict[str, Any]],
        raw_items: list[dict[str, Any]] | None = None,
    ) -> None:
        with self._lock:
            self._chapters[series_uid] = ChapterList(series_uid, items, list(raw_items or []))
            record = self._series.get(series_uid)
            if record is not None:
                record.chapters_at = time.time()

    def get_chapters(self, series_uid: str, ttl: float = 3600 * 6) -> ChapterList | None:
        with self._lock:
            entry = self._chapters.get(series_uid)
            if entry is None:
                return None
            if ttl and time.time() - entry.at > ttl:
                return None
            return entry

    def find_chapter(self, series_uid: str, chapter_uid: str) -> dict[str, Any] | None:
        entry = self.get_chapters(series_uid, ttl=0)
        if entry is None:
            return None
        for index, item in enumerate(entry.items):
            if item.get("uid") == chapter_uid:
                if index < len(entry.raw_items):
                    return entry.raw_items[index]
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
            while len(self._pages) > self._max_pages:
                self._pages.popitem(last=False)

    def get_pages(self, chapter_uid: str, ttl: float = 3600 * 3) -> PageList | None:
        with self._lock:
            entry = self._pages.get(chapter_uid)
            if entry is None:
                return None
            if ttl and time.time() - entry.at > ttl:
                return None
            self._pages.move_to_end(chapter_uid)
            return entry

    # ---------- 维护 ----------
    def clear(self) -> None:
        with self._lock:
            self._series.clear()
            self._chapters.clear()
            self._pages.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "series": len(self._series),
                "chapters": len(self._chapters),
                "pages": len(self._pages),
            }


store = Store()
