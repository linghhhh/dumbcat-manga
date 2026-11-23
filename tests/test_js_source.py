"""JS 脚本图源：包装层的字段归一。

QuickJS 只在安卓上有，这里把 `js_bridge.call` 换成假的，验证 Python 这一侧的
字段映射（链接补全、缺省标题、tags 归一、章节与图片、fetch_image 两种返回）。
"""

from __future__ import annotations

import pytest

from dumbcat.plugins import js_bridge
from dumbcat.plugins.api import PluginContext, PluginError
from dumbcat.plugins.js_source import JsSourcePlugin


class DemoSource(JsSourcePlugin):
    SCRIPT = "registerSource({key:'demo_js'})"
    SCRIPT_PATH = "demo.js"
    META = {
        "key": "demo_js",
        "name": "演示图源",
        "baseUrl": "https://demo.test",
        "pageSize": 3,
    }


class FakeContext(PluginContext):
    def __init__(self) -> None:  # 不真的建 http 客户端
        pass


@pytest.fixture()
def source():
    return DemoSource(FakeContext())


def _patch(monkeypatch, mapping):
    def fake_call(key, fn, payload=None):
        if key != "demo_js":
            raise AssertionError(f"意外的 key: {key}")
        handler = mapping.get(fn)
        if handler is None:
            raise PluginError(f"图源不支持 {fn}")
        return handler(payload) if callable(handler) else handler

    monkeypatch.setattr(js_bridge, "call", fake_call)


def test_browse_normalizes_items(source, monkeypatch) -> None:
    _patch(monkeypatch, {
        "browse": lambda payload: {
            "items": [
                {"title": "作品 A", "url": "/s/1", "cover": "/c/1.jpg", "tags": "动作，冒险"},
                {"url": "https://other.test/s/2"},
            ],
            "hasMore": True,
        },
    })
    page = source.browse(1, "")
    assert page.page == 1
    assert page.has_more is True
    assert page.items[0]["url"] == "https://demo.test/s/1"
    assert page.items[0]["poster"] == "https://demo.test/c/1.jpg"
    assert page.items[0]["tags"] == ["动作", "冒险"]
    assert page.items[1]["url"] == "https://other.test/s/2"
    assert page.items[1]["title"] == "未命名作品"


def test_search_page_size_infers_has_more(source, monkeypatch) -> None:
    seen = {}

    def handler(payload):
        seen.update(payload)
        return {"items": [{"title": "结果", "url": "https://demo.test/s/9"}], "page": payload["page"]}

    _patch(monkeypatch, {"search": handler})
    page = source.search("关键词", 2)
    assert seen["query"] == "关键词"
    assert seen["page"] == 2
    assert seen["pageSize"] == 3
    assert page.has_more is False, "条数少于 pageSize 时判定没有下一页"


def test_detail_optional(source, monkeypatch) -> None:
    _patch(monkeypatch, {})
    assert source.detail({"url": "https://demo.test/s/1"}) == {}, "脚本没写 detail 时不该炸"


def test_chapters_and_pages(source, monkeypatch) -> None:
    _patch(monkeypatch, {
        "chapters": [
            {"title": "第 1 话", "url": "/c/1"},
            {"title": "第 2 话", "url": "https://demo.test/c/2"},
        ],
        "pages": {"urls": ["/p/1.jpg", "/p/2.jpg"], "referer": "https://demo.test/c/1"},
    })
    series = {"url": "https://demo.test/s/1"}
    chapters = source.chapters(series)
    assert [row["url"] for row in chapters] == ["https://demo.test/c/1", "https://demo.test/c/2"]

    chapter = {"url": "https://demo.test/c/1"}
    pages = source.pages(series, chapter)
    assert [row["url"] for row in pages] == [
        "https://demo.test/p/1.jpg",
        "https://demo.test/p/2.jpg",
    ]
    assert [row["index"] for row in pages] == [1, 2]
    assert source.referer(series, chapter) == "https://demo.test/c/1"


def test_fetch_image_base64(source, monkeypatch) -> None:
    _patch(monkeypatch, {"fetchImage": {"base64": "UE5HREFUQQ=="}})
    payload = source.fetch_image("https://demo.test/p/1.jpg", "https://demo.test/c/1")
    assert payload == b"PNGDATA"


def test_missing_function_reports(source, monkeypatch) -> None:
    _patch(monkeypatch, {})
    with pytest.raises(PluginError, match="不支持"):
        source.chapters({"url": "https://demo.test/s/1"})
