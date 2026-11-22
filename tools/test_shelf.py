"""书架后端自测（不联网、不需要真图源）。

运行：
    C:/Users/linghhhh/AppData/Local/Programs/Python/Python310/python.exe tools/test_shelf.py

覆盖：默认架 / 建架改名删架 / source 架拒删 / 多架并存与幂等 / remove 的 remote 三态 /
历史去重与 500 上限 / legacy 迁移幂等 / 远端异常降级 / shelves.json 损坏重建。

注意：桌面版 ``dumbcat.app_config`` 只读 ``APPDATA``，安卓版才读 ``DUMBCAT_APPDATA``，
所以这里两个都指向同一个临时目录（与契约的 DUMBCAT_APPDATA 用法等价）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = Path(__file__).resolve().parent
ANDROID_ROOT = HERE.parent
V3_ROOT = ANDROID_ROOT.parent / "呆猫漫画v3"
sys.path.insert(0, str(V3_ROOT))

TMP = Path(tempfile.mkdtemp(prefix="dumbcat_shelf_test_"))
os.environ["DUMBCAT_APPDATA"] = str(TMP)
os.environ["APPDATA"] = str(TMP)
os.environ["DUMBCAT_ALLOW_TEST_HOST"] = "1"
os.environ["DUMBCAT_NO_WARMUP"] = "1"

APP_CONFIG = TMP / "DumbCatManga"
APP_CONFIG.mkdir(parents=True, exist_ok=True)
LEGACY_FILE = APP_CONFIG / "legacy_downloads.json"
LEGACY_FILE.write_text(
    json.dumps(
        [
            {
                "id": "999",
                "title": "旧作A",
                "provider": "18comic",
                "providerName": "禁漫天堂",
                "state": "done",
                "finishedAt": 1700000000,
            },
            {
                "id": "998",
                "title": "旧作B",
                "provider": "18comic",
                "providerName": "禁漫天堂",
                "state": "done",
                "finishedAt": 1700000001,
            },
            {
                "id": "999",
                "title": "旧作A（重复行）",
                "provider": "18comic",
                "finishedAt": 1700000002,
            },
        ],
        ensure_ascii=False,
    ),
    encoding="utf-8",
)

from fastapi.testclient import TestClient  # noqa: E402

from dumbcat import shelf as shelf_store  # noqa: E402
from dumbcat.server import app as appmod  # noqa: E402
from dumbcat.sources.base import FavoritesUnsupported, LoginRequired  # noqa: E402

CLIENT = TestClient(appmod.app)
HEADERS = {"x-dumbcat-token": appmod.TOKEN}
STORE_FILE = APP_CONFIG / "shelves.json"

ITEM_FIELDS = {
    "id",
    "shelfId",
    "uid",
    "source",
    "key",
    "title",
    "cover",
    "coverUrl",
    "author",
    "tags",
    "nsfw",
    "url",
    "providerName",
    "added",
    "remote",
}
SHELF_FIELDS = {"id", "name", "kind", "source", "count", "created", "updated"}
HISTORY_FIELDS = {
    "uid",
    "source",
    "title",
    "cover",
    "coverUrl",
    "chapterUid",
    "chapterTitle",
    "page",
    "total",
    "ts",
}
SOURCES_FIELDS = {"key", "name", "supportsFavorites", "loggedIn", "username", "shelfId"}


# ---------------------------------------------------------------------------
# 假 provider / 假 adapter
# ---------------------------------------------------------------------------


class FakeSession:
    def __init__(self, username: str = "tester") -> None:
        self.data = {"username": username}
        self.confirmed = bool(username)


class FakeProvider:
    def __init__(self) -> None:
        self.supported = True
        self.logged = True
        self.mode = "ok"  # ok | unsupported | login | boom | false
        self.added: list[dict] = []
        self.removed: list[dict] = []
        self.favorites: list[dict] = []
        self.display_name = "禁漫天堂"
        self._session = FakeSession("tester")

    def supports_favorites(self) -> bool:
        return self.supported

    def is_logged_in(self) -> bool:
        return self.logged

    def _maybe_raise(self) -> bool:
        if self.mode == "unsupported":
            raise FavoritesUnsupported("该图源不支持收藏夹")
        if self.mode == "login":
            raise LoginRequired("需要登录", source="18comic")
        if self.mode == "boom":
            raise RuntimeError("远端炸了")
        return self.mode != "false"

    def add_favorite(self, series: dict, folder=None) -> bool:
        self.added.append(dict(series))
        return self._maybe_raise()

    def remove_favorite(self, series: dict) -> bool:
        self.removed.append(dict(series))
        return self._maybe_raise()

    def favorite_series(self, folder: str = "") -> list[dict]:
        self._maybe_raise()
        return [dict(row) for row in self.favorites]


class FakeAdapter:
    key = "18comic"
    name = "禁漫天堂"

    def __init__(self, provider: FakeProvider) -> None:
        self.provider = provider
        info = type("Info", (), {})()
        info.base_url = "https://example.test"
        info.nsfw = True
        self.info = info


FAKE_PROVIDER = FakeProvider()
FAKE_ADAPTER = FakeAdapter(FAKE_PROVIDER)
appmod.registry.get = lambda key: FAKE_ADAPTER if key == "18comic" else None  # type: ignore[assignment]
appmod.registry.sources = lambda: [  # type: ignore[assignment]
    {"key": "18comic", "name": "禁漫天堂", "kind": "builtin", "enabled": True}
]


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def api(method: str, path: str, body: dict | None = None):
    return CLIENT.request(method, path, json=body, headers=HEADERS)


def ok_json(method: str, path: str, body: dict | None = None) -> dict:
    resp = api(method, path, body)
    assert resp.status_code == 200, f"{method} {path} -> {resp.status_code}: {resp.text}"
    return resp.json()


def expect_error(
    method: str, path: str, body: dict | None = None, *, status: int = 400, detail: str = ""
) -> dict:
    resp = api(method, path, body)
    assert resp.status_code == status, f"{method} {path} -> {resp.status_code}: {resp.text}"
    data = resp.json()
    assert isinstance(data.get("detail"), str) and data["detail"], f"detail 缺失: {data}"
    if detail:
        assert detail in data["detail"], f"detail={data['detail']!r} 不含 {detail!r}"
    return data


def shelves_by_id(data: dict) -> dict[str, dict]:
    return {shelf["id"]: shelf for shelf in data["shelves"]}


def find_shelf(data: dict, name: str) -> dict:
    for shelf in data["shelves"]:
        if shelf["name"] == name:
            return shelf
    raise AssertionError(f"没有找到书架 {name!r}: {data['shelves']}")


def overview() -> dict:
    return ok_json("GET", "/api/shelf")


FAILED: list[tuple[str, BaseException]] = []


def check(name: str, fn) -> None:
    try:
        fn()
    except BaseException as exc:  # noqa: BLE001
        FAILED.append((name, exc))
        print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
        traceback.print_exc()
    else:
        print(f"[PASS] {name}")


# ---------------------------------------------------------------------------
# 各段测试（按顺序执行，后面的段依赖前面的状态）
# ---------------------------------------------------------------------------


def t_routes_registered() -> None:
    paths = [route.path for route in appmod.app.routes if route.path.startswith("/api/shelf")]
    expected = {
        "/api/shelf",
        "/api/shelf/items",
        "/api/shelf/status",
        "/api/shelf/history",
        "/api/shelf/create",
        "/api/shelf/rename",
        "/api/shelf/delete",
        "/api/shelf/add",
        "/api/shelf/remove",
        "/api/shelf/sync",
        "/api/shelf/history/clear",
        "/api/shelf/history/remove",
    }
    assert set(paths) == expected, f"路由不符: {sorted(set(paths))}"
    assert len(paths) == 13, f"应为 13 个路由（history 同时 GET/POST），实际 {len(paths)}"


def t_overview_and_legacy_migration() -> None:
    data = overview()
    assert set(data) == {"shelves", "defaultShelfId", "sources", "historyCount"}, data.keys()
    shelves = shelves_by_id(data)
    assert "s_default" in shelves, shelves
    assert data["defaultShelfId"] == "s_default"
    default = shelves["s_default"]
    assert set(default) == SHELF_FIELDS, default.keys()
    assert default["name"] == "我的书架"
    assert default["kind"] == "local"
    assert default["count"] == 0
    assert "s_legacy" in shelves, "旧版下载没有迁移进来"
    legacy = shelves["s_legacy"]
    assert legacy["name"] == "旧版下载"
    assert legacy["kind"] == "local"
    assert legacy["count"] == 2, f"旧下载去重后应为 2，实际 {legacy['count']}"
    assert data["historyCount"] == 0
    sources = data["sources"]
    assert len(sources) == 1, sources
    source = sources[0]
    assert set(source) == SOURCES_FIELDS, source.keys()
    assert source["key"] == "18comic"
    assert source["name"] == "禁漫天堂"
    assert source["supportsFavorites"] is True
    assert source["loggedIn"] is True
    assert source["username"] == "tester"
    assert source["shelfId"] == "s_source_18comic"
    items = ok_json("GET", "/api/shelf/items?shelfId=s_legacy")["items"]
    assert len(items) == 2
    assert {item["title"] for item in items} == {"旧作A", "旧作B"}
    for item in items:
        assert set(item) == ITEM_FIELDS, item.keys()


def t_legacy_migration_idempotent() -> None:
    shelf_store.load_store()
    shelf_store.load_store()
    data = overview()
    assert shelves_by_id(data)["s_legacy"]["count"] == 2


def t_create_rename() -> None:
    first = ok_json("POST", "/api/shelf/create", {"name": ""})["shelf"]
    assert first["name"] == "书架 2", first
    assert set(first) == SHELF_FIELDS
    second = ok_json("POST", "/api/shelf/create", {"name": ""})["shelf"]
    assert second["name"] == "书架 3", second
    custom = ok_json("POST", "/api/shelf/create", {"name": " 自定义 "})["shelf"]
    assert custom["name"] == "自定义", custom
    renamed = ok_json("POST", "/api/shelf/rename", {"id": custom["id"], "name": "  改名  "})
    assert renamed["shelf"]["name"] == "改名"
    expect_error("POST", "/api/shelf/rename", {"id": custom["id"], "name": "   "}, detail="不能为空")
    expect_error("POST", "/api/shelf/rename", {"id": "s_nope", "name": "x"}, detail="书架不存在")
    # 重名允许
    dup = ok_json("POST", "/api/shelf/create", {"name": "改名"})["shelf"]
    assert dup["name"] == "改名"
    t_create_rename.second_id = second["id"]  # type: ignore[attr-defined]


def t_delete_shelf() -> None:
    expect_error("POST", "/api/shelf/delete", {"id": "s_nope"}, detail="书架不存在")
    result = ok_json("POST", "/api/shelf/delete", {"id": "s_legacy"})
    assert result == {"ok": True}, result
    data = overview()
    assert "s_legacy" not in shelves_by_id(data)
    assert ok_json("GET", "/api/shelf/items?shelfId=s_legacy")["items"] == []


def t_add_remove_idempotent() -> None:
    series = {
        "uid": "18comic.abc123",
        "provider": "18comic",
        "providerName": "禁漫天堂",
        "title": "测试作品",
        "coverUrl": "/img/cover/18comic.abc123?token=OLD",
        "author": "作者甲",
        "tags": ["标签1", "标签2"],
        "nsfw": True,
        "url": "https://example.test/album/5555",
    }
    first = ok_json("POST", "/api/shelf/add", {"shelfId": "s_default", "uid": series["uid"], "source": "18comic", "series": series})
    assert set(first) == {"item", "remote", "shelf"}, first.keys()
    item = first["item"]
    assert set(item) == ITEM_FIELDS, item.keys()
    assert item["uid"] == "18comic.abc123"
    assert item["source"] == "18comic" and item["key"] == "18comic"
    assert item["title"] == "测试作品"
    assert item["cover"] == item["coverUrl"] == "/img/cover/18comic.abc123", item
    assert item["providerName"] == "禁漫天堂"
    assert item["tags"] == ["标签1", "标签2"]
    assert item["nsfw"] is True
    assert first["remote"] == "synced"
    assert first["shelf"]["id"] == "s_default" and first["shelf"]["count"] == 1
    assert len(FAKE_PROVIDER.added) == 1
    assert FAKE_PROVIDER.added[0].get("id") == "5555", "应从详情地址认出禁漫作品 id"

    again = ok_json("POST", "/api/shelf/add", {"shelfId": "s_default", "uid": series["uid"], "source": "18comic", "series": series})
    assert again["item"]["id"] == item["id"], "同架同 uid 应幂等返回同一 item"
    assert len(FAKE_PROVIDER.added) == 1, "幂等命中不应重复推远端"

    second_shelf_id = t_create_rename.second_id  # type: ignore[attr-defined]
    other = ok_json("POST", "/api/shelf/add", {"shelfId": second_shelf_id, "uid": series["uid"], "source": "18comic", "series": series})
    assert other["item"]["id"] != item["id"]
    assert other["remote"] == "synced"

    status = ok_json("GET", f"/api/shelf/status?uid={series['uid']}")
    assert status["saved"] is True
    assert {shelf["id"] for shelf in status["shelves"]} == {"s_default", second_shelf_id}
    assert all(set(shelf) == {"id", "name"} for shelf in status["shelves"])

    all_items = ok_json("GET", "/api/shelf/items")["items"]
    assert sum(1 for row in all_items if row["uid"] == series["uid"]) == 2

    kept = ok_json("POST", "/api/shelf/remove", {"shelfId": "s_default", "uid": series["uid"]})
    assert kept == {"ok": True, "remote": "kept"}, kept
    assert FAKE_PROVIDER.removed == [], "仍被别的书架引用时不能动远端"

    last = ok_json("POST", "/api/shelf/remove", {"shelfId": second_shelf_id, "uid": series["uid"]})
    assert last == {"ok": True, "remote": "synced"}, last
    assert len(FAKE_PROVIDER.removed) == 1
    assert FAKE_PROVIDER.removed[0].get("id") == "5555", FAKE_PROVIDER.removed[0]

    again_remove = ok_json("POST", "/api/shelf/remove", {"shelfId": second_shelf_id, "uid": series["uid"]})
    assert again_remove["ok"] is True and again_remove["remote"] == ""
    assert ok_json("GET", f"/api/shelf/status?uid={series['uid']}") == {"shelves": [], "saved": False}


def t_add_cover_field_and_source_fallback() -> None:
    # 前端卡片可能只带 cover、不带 source；uid 从 series 里兜底
    series = {
        "uid": "18comic.cover1",
        "provider": "18comic",
        "title": "封面兼容",
        "cover": "https://img.test/cover1.jpg?token=X&size=2",
        "tags": "单标签, 第二个",
    }
    result = ok_json("POST", "/api/shelf/add", {"shelfId": "s_default", "series": series})
    item = result["item"]
    assert item["source"] == "18comic", item
    assert item["cover"] == item["coverUrl"] == "https://img.test/cover1.jpg?size=2", item
    assert item["tags"] == ["单标签", "第二个"]
    assert item["nsfw"] is False
    ok_json("POST", "/api/shelf/remove", {"shelfId": "s_default", "uid": series["uid"]})


def t_remote_degradation() -> None:
    def add(uid: str) -> dict:
        series = {
            "uid": uid,
            "provider": "18comic",
            "title": f"作品{uid}",
            "coverUrl": "",
            "url": f"https://example.test/album/{uid.split('-')[-1]}",
        }
        return ok_json("POST", "/api/shelf/add", {"shelfId": "s_default", "uid": uid, "source": "18comic", "series": series})

    FAKE_PROVIDER.mode = "unsupported"
    assert add("18comic.u2")["remote"] == "unsupported"
    FAKE_PROVIDER.mode = "login"
    assert add("18comic.u3")["remote"] == "not-logged-in"
    FAKE_PROVIDER.mode = "boom"
    assert add("18comic.u4")["remote"] == "failed"
    FAKE_PROVIDER.mode = "false"
    assert add("18comic.u5")["remote"] == "failed"

    FAKE_PROVIDER.mode = "ok"
    calls = len(FAKE_PROVIDER.added)
    FAKE_PROVIDER.logged = False
    item6 = add("18comic.u6")["item"]
    assert item6["remote"] == "not-logged-in"
    assert len(FAKE_PROVIDER.added) == calls, "未登录不应调用远端"

    FAKE_PROVIDER.supported = False
    item7 = add("18comic.u7")["item"]
    assert item7["remote"] == "unsupported"
    assert len(FAKE_PROVIDER.added) == calls, "不支持收藏时不应调用远端"

    FAKE_PROVIDER.supported = True
    FAKE_PROVIDER.logged = True
    rows = ok_json("GET", "/api/shelf/items?shelfId=s_default")["items"]
    remote_by_uid = {row["uid"]: row["remote"] for row in rows}
    assert remote_by_uid["18comic.u2"] == "unsupported"
    assert remote_by_uid["18comic.u3"] == "not-logged-in"
    assert remote_by_uid["18comic.u4"] == "failed"
    assert remote_by_uid["18comic.u5"] == "failed"
    assert remote_by_uid["18comic.u6"] == "not-logged-in"
    assert remote_by_uid["18comic.u7"] == "unsupported"
    # 远端失败不能影响本地保存
    assert all(uid in remote_by_uid for uid in ("18comic.u2", "18comic.u3", "18comic.u4", "18comic.u5", "18comic.u6", "18comic.u7"))

    for uid in list(remote_by_uid):
        if uid.startswith("18comic.u"):
            ok_json("POST", "/api/shelf/remove", {"shelfId": "s_default", "uid": uid})


def _favorite_row(ident: str, title: str) -> dict:
    return {
        "id": ident,
        "slug": ident,
        "title": title,
        "url": f"https://example.test/album/{ident}",
        "poster": f"https://img.test/{ident}.jpg",
        "author": f"作者{ident}",
        "tags": ["收藏"],
        "nsfw": True,
    }


def t_sync_source_shelf() -> None:
    FAKE_PROVIDER.mode = "ok"
    FAKE_PROVIDER.logged = True
    FAKE_PROVIDER.supported = True
    FAKE_PROVIDER.favorites = [
        _favorite_row("1001", "收藏A"),
        _favorite_row("1002", "收藏B"),
        _favorite_row("1003", "收藏C"),
    ]
    local_before = len(ok_json("GET", "/api/shelf/items?shelfId=s_default")["items"])
    result = ok_json("POST", "/api/shelf/sync", {"source": "18comic"})
    assert {"shelf", "added", "removed", "total", "at"} <= set(result), result.keys()
    assert result["added"] == 3 and result["removed"] == 0 and result["total"] == 3
    shelf = result["shelf"]
    assert set(shelf) == SHELF_FIELDS, shelf.keys()
    assert shelf["id"] == "s_source_18comic"
    assert shelf["kind"] == "source" and shelf["source"] == "18comic"
    assert shelf["name"] == "禁漫天堂 收藏"
    assert shelf["count"] == 3
    assert isinstance(result["at"], int) and result["at"] > 0

    items = ok_json("GET", "/api/shelf/items?shelfId=s_source_18comic")["items"]
    assert len(items) == 3
    assert {row["title"] for row in items} == {"收藏A", "收藏B", "收藏C"}
    for row in items:
        assert set(row) == ITEM_FIELDS
        assert row["remote"] == "synced"
        assert row["source"] == "18comic" and row["key"] == "18comic"
        assert row["providerName"] == "禁漫天堂"
        # normalize 会把封面换成本地代理地址 /img/cover/<uid>；存进书架时 token 已去掉
        assert row["cover"] == row["coverUrl"] == f"/img/cover/{row['uid']}", row
        assert "token=" not in row["coverUrl"]
        assert row["tags"] == ["收藏"]
        assert row["nsfw"] is True
    # 不动 local 架
    assert len(ok_json("GET", "/api/shelf/items?shelfId=s_default")["items"]) == local_before

    # 增量：远端少了一个 → removed=1，且条目从 source 架消失
    FAKE_PROVIDER.favorites = [_favorite_row("1001", "收藏A"), _favorite_row("1002", "收藏B")]
    again = ok_json("POST", "/api/shelf/sync", {"source": "18comic"})
    assert again["added"] == 0 and again["removed"] == 1 and again["total"] == 2, again
    assert again["shelf"]["count"] == 2
    titles = {row["title"] for row in ok_json("GET", "/api/shelf/items?shelfId=s_source_18comic")["items"]}
    assert titles == {"收藏A", "收藏B"}

    # 未登录 / 不支持 / 远端异常都是 400 + 中文
    FAKE_PROVIDER.logged = False
    expect_error("POST", "/api/shelf/sync", {"source": "18comic"}, detail="还没登录禁漫天堂")
    FAKE_PROVIDER.logged = True
    FAKE_PROVIDER.supported = False
    expect_error("POST", "/api/shelf/sync", {"source": "18comic"}, detail="不支持官方收藏夹")
    FAKE_PROVIDER.supported = True
    FAKE_PROVIDER.mode = "boom"
    expect_error("POST", "/api/shelf/sync", {"source": "18comic"}, detail="同步收藏失败")
    FAKE_PROVIDER.mode = "ok"

    # source 架拒删
    expect_error(
        "POST",
        "/api/shelf/delete",
        {"id": "s_source_18comic"},
        detail="图源书架不能删除，可以先退出图源账号",
    )
    # 未知图源
    expect_error("POST", "/api/shelf/sync", {"source": "nope"}, detail="图源不存在")


def t_history() -> None:
    expect_error("POST", "/api/shelf/history", {"title": "x"}, detail="缺少作品 uid")
    record = {
        "uid": "18comic.read1",
        "source": "18comic",
        "title": "阅读作品",
        "cover": "/img/cover/read1?token=OLD",
        "chapterUid": "18comic.read1.c1",
        "chapterTitle": "第1话",
        "page": 3,
        "total": 24,
    }
    assert ok_json("POST", "/api/shelf/history", record) == {"ok": True}
    rows = ok_json("GET", "/api/shelf/history")["items"]
    assert len(rows) == 1
    entry = rows[0]
    assert set(entry) == HISTORY_FIELDS, entry.keys()
    assert entry["cover"] == entry["coverUrl"] == "/img/cover/read1"
    assert entry["page"] == 3 and entry["total"] == 24
    assert entry["chapterTitle"] == "第1话"
    assert isinstance(entry["ts"], int) and entry["ts"] > 0

    # 同一个 uid 只留一条，新的覆盖旧的
    updated = dict(record)
    updated.update({"page": 8, "chapterUid": "18comic.read1.c2", "chapterTitle": "第2话"})
    ok_json("POST", "/api/shelf/history", updated)
    rows = ok_json("GET", "/api/shelf/history")["items"]
    assert len(rows) == 1 and rows[0]["page"] == 8 and rows[0]["chapterTitle"] == "第2话"

    ok_json("POST", "/api/shelf/history", {**record, "uid": "18comic.read2", "title": "第二本"})
    rows = ok_json("GET", "/api/shelf/history")["items"]
    assert len(rows) == 2 and rows[0]["uid"] == "18comic.read2", "应按 ts 倒序"
    assert ok_json("GET", "/api/shelf")["historyCount"] == 2

    cleared = ok_json("POST", "/api/shelf/history/clear")
    assert cleared == {"ok": True, "removed": 2}, cleared
    assert ok_json("GET", "/api/shelf/history")["items"] == []

    # 500 条上限
    for index in range(505):
        resp = api(
            "POST",
            "/api/shelf/history",
            {
                "uid": f"18comic.cap{index}",
                "source": "18comic",
                "title": f"上限{index}",
                "chapterUid": f"18comic.cap{index}.c1",
                "page": 1,
                "total": 10,
            },
        )
        assert resp.status_code == 200, resp.text
    capped = ok_json("GET", "/api/shelf/history?limit=1000")["items"]
    assert len(capped) == 500, f"历史上限应为 500，实际 {len(capped)}"
    assert capped[0]["uid"] == "18comic.cap504"
    assert capped[-1]["uid"] == "18comic.cap5"
    # 默认 limit=100
    assert len(ok_json("GET", "/api/shelf/history")["items"]) == 100

    removed = ok_json("POST", "/api/shelf/history/remove", {"uid": "18comic.cap504"})
    assert removed == {"ok": True}
    rows = ok_json("GET", "/api/shelf/history?limit=1000")["items"]
    assert len(rows) == 499 and rows[0]["uid"] == "18comic.cap503"
    assert ok_json("POST", "/api/shelf/history/remove", {"uid": "18comic.nope"}) == {"ok": True}
    assert ok_json("POST", "/api/shelf/history/clear")["removed"] == 499


def t_bad_requests() -> None:
    expect_error("POST", "/api/shelf/add", {"shelfId": "s_default", "series": {"title": "无 uid"}}, detail="缺少作品 uid")
    expect_error(
        "POST",
        "/api/shelf/add",
        {"shelfId": "s_nope", "uid": "18comic.x", "source": "18comic", "series": {"title": "x"}},
        detail="书架不存在",
    )
    expect_error("POST", "/api/shelf/remove", {"uid": "18comic.x"}, detail="缺少书架 id")
    expect_error("POST", "/api/shelf/remove", {"shelfId": "s_default"}, detail="缺少作品 uid")
    expect_error("GET", "/api/shelf/status", detail="缺少作品 uid")


def t_corrupt_store_rebuild() -> None:
    STORE_FILE.write_text("{ 这不是 JSON", encoding="utf-8")
    data = overview()
    shelves = shelves_by_id(data)
    assert "s_default" in shelves, "损坏后应重建默认架"
    assert "s_legacy" in shelves and shelves["s_legacy"]["count"] == 2, "损坏重建后应重新迁移旧下载"
    payload = json.loads(STORE_FILE.read_text(encoding="utf-8"))
    assert payload["version"] == 1 and payload["migrated"] is True
    assert isinstance(payload["shelves"], list) and isinstance(payload["items"], list)
    assert isinstance(payload["history"], list)


# ---------------------------------------------------------------------------
# 跑
# ---------------------------------------------------------------------------


def main() -> int:
    checks = [
        ("13 个路由已注册", t_routes_registered),
        ("默认架 + legacy 迁移 + GET /api/shelf 字段", t_overview_and_legacy_migration),
        ("legacy 迁移幂等", t_legacy_migration_idempotent),
        ("建架 / 空名自动命名 / 改名 / 重名", t_create_rename),
        ("删架 / 删不存在的架", t_delete_shelf),
        ("加收藏幂等 / 多架并存 / remove 的 kept 与远端删除", t_add_remove_idempotent),
        ("cover 字段兼容 / source 兜底", t_add_cover_field_and_source_fallback),
        ("远端异常降级（unsupported/not-logged-in/failed）", t_remote_degradation),
        ("图源收藏同步（增量合并 / source 架拒删）", t_sync_source_shelf),
        ("阅读历史（去重 / 上限 500 / 清空 / 单条删除）", t_history),
        ("400 与中文 detail", t_bad_requests),
        ("shelves.json 损坏后重建", t_corrupt_store_rebuild),
    ]
    for name, fn in checks:
        check(name, fn)

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)} / {len(checks)}")
        for name, exc in FAILED:
            print(f"  - {name}: {type(exc).__name__}: {exc}")
        print(f"临时数据目录：{TMP}")
        return 1
    print(f"ALL PASS ({len(checks)}/{len(checks)})")
    print(f"临时数据目录：{TMP}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
