"""SQLite 真源：写入 → 读回 → 模拟重启 → 仍能读回。

覆盖 P1 的核心承诺：进程重启（内存缓存清空、连接关掉）之后，作品 / 章节 /
图片列表都不丢，`find_chapter` 仍能取回带 Referer 的原始章节。
"""

from __future__ import annotations

import time

from dumbcat.server import db
from dumbcat.server.store import PageList, SeriesRecord, Store


def _series(uid: str = "qimanga.s1") -> SeriesRecord:
    return SeriesRecord(
        uid=uid,
        provider="qimanga",
        provider_name="奇漫",
        raw={"uid": uid, "url": "https://example.test/s/1"},
        public={"uid": uid, "title": "测试作品"},
        base_url="https://example.test",
        nsfw=False,
    )


def test_series_chapters_pages_survive_restart() -> None:
    store = Store()
    record = _series()
    assert store.put_series(record) is record
    assert store.get_series(record.uid) is not None

    chapters = [
        {"uid": "qimanga.s1.c1", "title": "第 1 话"},
        {"uid": "qimanga.s1.c2", "title": "第 2 话"},
        {"uid": "qimanga.s1.c3", "title": "第 3 话"},
    ]
    raw_chapters = [
        {"uid": item["uid"], "url": f"https://example.test/c/{index}"}
        for index, item in enumerate(chapters, start=1)
    ]
    store.put_chapters(record.uid, chapters, raw_chapters)
    assert len(store.get_chapters(record.uid).items) == 3

    pages = PageList(
        chapter_uid="qimanga.s1.c2",
        series_uid=record.uid,
        items=[{"url": "https://img.test/1.jpg"}, {"url": "https://img.test/2.jpg"}],
        referer="https://example.test/c/2",
    )
    store.put_pages(pages)

    # 模拟进程重启：清内存缓存 + 关闭连接
    store.clear()
    db.close()

    fresh = Store()
    restored = fresh.get_series(record.uid)
    assert restored is not None, "重启后作品应该还在"
    assert restored.provider == "qimanga"
    assert len(fresh.get_chapters(record.uid).items) == 3
    assert fresh.find_chapter(record.uid, "qimanga.s1.c2")["url"] == "https://example.test/c/2"
    assert fresh.find_chapter_public(record.uid, "qimanga.s1.c2")["title"] == "第 2 话"

    cached = fresh.get_pages("qimanga.s1.c2")
    assert cached is not None
    assert cached.referer == "https://example.test/c/2"
    assert len(cached.items) == 2
    assert cached.at <= time.time() + 1

    stats = fresh.stats()
    assert stats["db_series"] >= 1
    assert stats["db_chapters"] >= 3
    assert stats["db_pages"] >= 1


def test_chapters_overwrite_keeps_latest_order() -> None:
    store = Store()
    record = _series("qimanga.s2")
    store.put_series(record)
    store.put_chapters("qimanga.s2", [{"uid": "qimanga.s2.c1", "title": "旧"}], [{"uid": "qimanga.s2.c1"}])
    store.put_chapters(
        "qimanga.s2",
        [{"uid": "qimanga.s2.c2", "title": "新 1"}, {"uid": "qimanga.s2.c1", "title": "新 2"}],
        [{"uid": "qimanga.s2.c2"}, {"uid": "qimanga.s2.c1"}],
    )
    items = store.get_chapters("qimanga.s2").items
    assert [item["uid"] for item in items] == ["qimanga.s2.c2", "qimanga.s2.c1"]


def test_expired_pages_still_readable() -> None:
    """离线要能打开看过的章节：超时也不该把缓存扔掉。"""
    store = Store()
    record = _series("qimanga.s3")
    store.put_series(record)
    store.put_chapters("qimanga.s3", [{"uid": "qimanga.s3.c1"}], [{"uid": "qimanga.s3.c1"}])
    store.put_pages(
        PageList(chapter_uid="qimanga.s3.c1", series_uid="qimanga.s3", items=[{"url": "https://img.test/a.jpg"}])
    )
    store.clear()
    cached = store.get_pages("qimanga.s3.c1", ttl=-1)
    assert cached is not None and cached.items
