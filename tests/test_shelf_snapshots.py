"""书架快照与批量操作。

覆盖：收藏时存章节快照 → 断网/重启后 `offline_detail` 仍能给出章节；
历史自动入架；置顶与批量移出 / 移动；历史条数上限按设置生效。
"""

from __future__ import annotations

from dumbcat import shelf
from dumbcat.server import config


def _series(uid: str, title: str = "测试作品") -> dict:
    return {
        "uid": uid,
        "source": "qimanga",
        "title": title,
        "cover": "https://img.test/cover.jpg",
        "author": "作者",
        "url": f"https://example.test/s/{uid}",
        "providerName": "奇漫",
        "status": "连载中",
        "description": "简介",
    }


def _chapters(uid: str, count: int = 2) -> list[dict]:
    return [
        {"uid": f"{uid}.c{index}", "title": f"第 {index} 话", "number": index, "index": index - 1, "locked": False}
        for index in range(1, count + 1)
    ]


def test_add_item_keeps_chapter_snapshot() -> None:
    uid = "qimanga.t_snap"
    item = shelf.add_item(
        shelf.DEFAULT_SHELF_ID,
        _series(uid),
        chapters=_chapters(uid, 3),
        summary={"description": "掉落简介", "status": "连载中"},
    )
    assert item["uid"] == uid
    assert len(item["chapters"]) == 3
    assert item["chapters"][0]["uid"] == f"{uid}.c1"

    offline = shelf.offline_detail(uid)
    assert offline is not None
    assert offline["offline"] is True
    assert offline["series"]["title"] == "测试作品"
    assert [row["uid"] for row in offline["chapters"]] == [f"{uid}.c{index}" for index in (1, 2, 3)]
    assert shelf.find_item_any_shelf(uid) is not None


def test_add_item_is_idempotent() -> None:
    uid = "qimanga.t_idem"
    first = shelf.add_item(shelf.DEFAULT_SHELF_ID, _series(uid), chapters=_chapters(uid, 1))
    second = shelf.add_item(shelf.DEFAULT_SHELF_ID, _series(uid), chapters=_chapters(uid, 4))
    assert first["uid"] == second["uid"]
    rows = [item for item in shelf.list_items(shelf.DEFAULT_SHELF_ID) if item["uid"] == uid]
    assert len(rows) == 1
    assert len(rows[0]["chapters"]) == 4, "再收藏一次应该刷新章节快照"


def test_history_auto_adds_to_shelf() -> None:
    uid = "qimanga.t_hist"
    shelf.history_add({"uid": uid, "title": "只看过没收藏", "chapterUid": f"{uid}.c2", "page": 5, "total": 20})
    item = shelf.find_item_any_shelf(uid)
    assert item is not None, "看过的作品应该自动进书架"
    rows = shelf.history_list(50)
    assert rows[0]["uid"] == uid


def test_pin_and_bulk_actions() -> None:
    base = shelf.DEFAULT_SHELF_ID
    other = shelf.create_shelf("测试书架")

    for index in range(3):
        uid = f"qimanga.t_bulk{index}"
        shelf.add_item(base, _series(uid, f"批量{index}"))

    shelf.pin_item(base, "qimanga.t_bulk0", True)
    order = [item["uid"] for item in shelf.list_items(base)]
    assert order[0] == "qimanga.t_bulk0"

    result = shelf.bulk_update(base, ["qimanga.t_bulk1", "qimanga.t_bulk2"], "pin")
    assert result["count"] == 2
    pinned = [item for item in shelf.list_items(base) if int(item.get("pin") or 0) > 0]
    assert len(pinned) == 3

    result = shelf.bulk_update(base, ["qimanga.t_bulk1", "qimanga.t_bulk2"], "move", other["id"])
    assert result["count"] == 2
    assert sorted(item["uid"] for item in shelf.list_items(other["id"])) == ["qimanga.t_bulk1", "qimanga.t_bulk2"]

    result = shelf.bulk_update(base, ["qimanga.t_bulk0"], "remove")
    assert result["count"] == 1
    assert all(item["uid"] != "qimanga.t_bulk0" for item in shelf.list_items(base))


def test_bulk_rejects_empty_selection() -> None:
    try:
        shelf.bulk_update(shelf.DEFAULT_SHELF_ID, [], "remove")
    except shelf.ShelfError as exc:
        assert "没有选中" in str(exc)
    else:
        raise AssertionError("空选择应该报错")


def test_history_limit_from_settings() -> None:
    settings = config.load_settings(refresh=True)
    previous = settings.history_limit
    settings.history_limit = 100
    config.save_settings(settings)
    try:
        for index in range(130):
            shelf.history_add({"uid": f"qimanga.t_limit{index}", "title": f"历史{index}", "ts": 1000 + index})
        rows = shelf.history_list(500)
        assert len(rows) == 100
        assert rows[0]["uid"] == "qimanga.t_limit129"
    finally:
        settings.history_limit = previous
        config.save_settings(settings)
