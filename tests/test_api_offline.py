"""HTTP 层：离线兜底、本地图、导出在桌面版的错误码。

不联网：作品/章节/图片都用假数据塞进 store 或书架，再清空 store 模拟重启。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from dumbcat.server import app as app_mod
from dumbcat.server import store as store_mod


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app_mod.app)


@pytest.fixture(scope="module")
def headers() -> dict:
    return {"x-dumbcat-token": app_mod.TOKEN}


SERIES = {
    "uid": "qimanga.api1",
    "source": "qimanga",
    "key": "qimanga",
    "title": "接口作品",
    "cover": "https://img.test/c.jpg",
    "coverUrl": "https://img.test/c.jpg",
    "author": "作者",
    "tags": ["标签"],
    "nsfw": False,
    "url": "https://example.test/s/api1",
    "providerName": "奇漫",
    "status": "连载中",
    "description": "简介",
}

CHAPTERS = [
    {"uid": "qimanga.api1.c1", "title": "第 1 话", "number": 1, "index": 0, "locked": False},
    {"uid": "qimanga.api1.c2", "title": "第 2 话", "number": 2, "index": 1, "locked": False},
]


def test_shelf_add_and_offline_detail(client, headers) -> None:
    res = client.post(
        "/api/shelf/add",
        json={
            "shelfId": "s_default",
            "uid": SERIES["uid"],
            "source": SERIES["source"],
            "series": SERIES,
            "chapters": CHAPTERS,
            "summary": {"description": "简介", "status": "连载中"},
        },
        headers=headers,
    )
    assert res.status_code == 200, res.text

    store_mod.store.clear()
    res = client.get(f"/api/series/{SERIES['uid']}", params={"token": app_mod.TOKEN})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["offline"] is True
    assert body["series"]["title"] == "接口作品"
    assert [row["uid"] for row in body["chapters"]] == ["qimanga.api1.c1", "qimanga.api1.c2"]


def test_shelf_items_are_slim(client, headers) -> None:
    res = client.get("/api/shelf/items", params={"shelfId": "s_default", "token": app_mod.TOKEN})
    assert res.status_code == 200
    rows = res.json()["items"]
    row = next(item for item in rows if item["uid"] == SERIES["uid"])
    assert row["chapterCount"] == 2
    assert "chapters" not in row, "列表接口不该把整份章节快照塞给前端"


def test_export_rejected_on_desktop(client, headers) -> None:
    res = client.post("/api/export", json={"path": ""}, headers=headers)
    assert res.status_code == 501
    assert "手机版" in res.json()["detail"]


def test_progress_roundtrip(client, headers) -> None:
    res = client.post(
        "/api/progress",
        json={"seriesUid": SERIES["uid"], "chapterUid": "qimanga.api1.c2", "page": 7, "offsetRatio": 0.5},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    res = client.get(f"/api/progress/{SERIES['uid']}", params={"token": app_mod.TOKEN})
    assert res.status_code == 200
    progress = res.json()["progress"]
    assert progress["chapterUid"] == "qimanga.api1.c2"
    assert progress["page"] == 7
    assert progress["offsetRatio"] == pytest.approx(0.5)


def test_local_page_image_served_from_disk(client, headers, tmp_path) -> None:
    """图源连不上时，已下载的图仍能按 /img/page 取出来。"""
    from dumbcat.server.config import load_settings

    output = tmp_path / "out"
    chapter_dir = output / "接口作品" / "Chapter 0002 - 第 2 话"
    chapter_dir.mkdir(parents=True)
    (chapter_dir / "0008.jpg").write_bytes(b"\xff\xd8\xff\xe0JFIF-fake")

    settings = load_settings(refresh=True)
    previous = settings.output_dir
    settings.output_dir = str(output)
    from dumbcat.server.config import save_settings

    save_settings(settings)
    try:
        store_mod.store.clear()
        res = client.get(
            "/img/page/qimanga.api1.c2/7",
            params={"token": app_mod.TOKEN},
        )
        assert res.status_code == 200, res.text
        assert res.headers["content-type"].startswith("image/")
    finally:
        settings.output_dir = previous
        save_settings(settings)


def test_unknown_series_reports_404(client, headers) -> None:
    res = client.get("/api/series/qimanga.nope", params={"token": app_mod.TOKEN})
    assert res.status_code == 404
    assert "过期" in json.dumps(res.json(), ensure_ascii=False)
