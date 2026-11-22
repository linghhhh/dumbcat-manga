"""JSON 图源：一个 ``.json`` 文件就是一个图源，用 CSS 选择器描述怎么抓。

为什么要有它：HTML 型站点（列表 / 详情 / 章节 / 图片）大多只是选择器不同，
把它们写成一个可分享的 JSON，比让每个人都复制一份 Python 插件省事得多,
也方便从别处（例如 GitHub 上的图源清单）搬规则。

文件放在 ``用户插件/`` 目录下，名字随便，但 ``key`` 必须唯一：

.. code-block:: json

    {
      "key": "example",
      "name": "示例图源",
      "base_url": "https://example.com",
      "browse": {
        "url": "/list/{category}/{page}.html",
        "list": ".book-list .item",
        "title": "a.title",
        "link": "a@href",
        "cover": "img@data-src"
      },
      "search": {
        "url": "/search?q={query}&page={page}",
        "list": ".result .item",
        "title": "a",
        "link": "a@href"
      },
      "chapters": {
        "url": "{url}",
        "list": ".chapter-list li a",
        "title": "",
        "link": "@href"
      },
      "pages": {
        "url": "{url}",
        "list": "#page img",
        "attr": "data-original"
      }
    }

选择器写法
==========

字段值可以是三种形式：

- ``"a.title"`` —— 取该节点里的文字
- ``"a@href"``  —— 取该节点的属性
- ``{"sel": "a", "attr": "href", "re": r"/book/(\\d+)", "group": 1}``
  —— 需要正则时用这种写法，``re`` 匹配不到就用 ``default``

``list`` 是所有条目共用的「重复节点」选择器；条目字段都是相对该节点找的。
``url`` 永远指「要抓的那一页」的地址模板（可带 ``{page}`` / ``{category}`` /
``{query}`` / ``{url}``），条目自己的链接用 ``link``（默认 ``a@href``，
章节默认 ``@href``）。
用 ``{url}`` / ``{chapter_url}`` 引用上一步拿到、并且已经是绝对地址的链接。
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from dumbcat.plugins.api import Category, Doc, Page, SourcePlugin

__all__ = ["JsonRuleSource", "config_errors"]

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_SAFE_CHARS = ":/?#[]@!$&'()*+,;=%"


def _join(base: str, value: str) -> str:
    """把站点返回的链接补成绝对地址（``//x`` 也算）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        return "https:" + text
    if text.startswith(("http://", "https://", "data:")):
        return text
    return urllib.parse.urljoin(base or "", text)


def _split_rule(rule: str) -> tuple[str, str]:
    text = str(rule or "").strip()
    if "@" in text:
        selector, attr = text.rsplit("@", 1)
        return selector.strip(), attr.strip()
    return text, ""


class JsonRuleSource(SourcePlugin):
    """按 JSON 规则抓页面的通用图源。"""

    #: 由注册表在加载时塞进来
    CONFIG: dict[str, Any] = {}

    icon = "📄"
    version = "1.0.0"

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        cfg = dict(self.CONFIG or {})
        self.cfg = cfg
        self.base = str(cfg.get("base_url") or self.base_url or "").strip().rstrip("/")
        self.base_url = str(cfg.get("base_url") or self.base_url or "")
        self.headers = {
            str(key): str(value) for key, value in (cfg.get("headers") or {}).items()
        }
        self.page_size = int(cfg.get("page_size") or 30)
        self._referer = str(cfg.get("referer") or "")

    # ------------------------------------------------------------------ 基础
    def _url(self, template: str, **values: Any) -> str:
        text = str(template or "")
        if not text:
            return self.base
        for name, value in values.items():
            text = text.replace(
                "{" + name + "}",
                urllib.parse.quote(str(value or ""), safe=_SAFE_CHARS),
            )
        return _join(self.base, text)

    def _load(self, url: str) -> Doc:
        html = self.ctx.http.get_text(url, headers=self.headers or None)
        return self.ctx.parse(html)

    @staticmethod
    def _field(scope: Doc, rule: Any) -> str:
        """按规则从节点里取一个字符串。"""
        if rule is None or rule == "":
            return ""
        default = ""
        if isinstance(rule, dict):
            default = str(rule.get("default") or "")
            selector = str(rule.get("sel") or "")
            attr = str(rule.get("attr") or "")
            node = scope.one(selector) if selector else scope
        else:
            selector, attr = _split_rule(str(rule))
            node = scope.one(selector) if selector else scope
        if node is None:
            return default
        text = node.attr(attr) if attr else node.text()
        text = str(text or "").strip()
        if isinstance(rule, dict) and rule.get("re"):
            match = re.search(str(rule["re"]), text)
            if match:
                group = rule.get("group", 1)
                try:
                    text = match.group(int(group))
                except (IndexError, ValueError):
                    text = match.group(0)
            elif default:
                text = default
            else:
                text = ""
        return text

    def _block_items(self, doc: Doc, block: dict[str, Any]) -> list[dict[str, Any]]:
        selector = str(block.get("list") or "").strip()
        nodes = doc.select(selector) if selector else [doc]
        items: list[dict[str, Any]] = []
        for node in nodes:
            href = self._field(node, block.get("link") or "a@href")
            if not href:
                continue
            title = self._field(node, block.get("title") or "a")
            cover = self._field(node, block.get("cover") or "img@src")
            item = {
                "title": title or "未命名作品",
                "url": _join(self.base, href),
            }
            if cover:
                item["poster"] = _join(self.base, cover)
            for name in ("author", "status", "latest", "summary"):
                if block.get(name):
                    value = self._field(node, block[name])
                    if value:
                        item[name] = value
            if block.get("tags"):
                tags = [self._field(node, block["tags"])]
                tags = [tag for tag in tags if tag]
                if tags:
                    item["tags"] = tags
            items.append(item)
        return items

    def _page(self, block_name: str, page: int, **values: Any) -> Page:
        block = self.cfg.get(block_name)
        if not isinstance(block, dict) or not block.get("url"):
            return Page(items=[], page=page, has_more=False)
        url = self._url(block["url"], page=page, **values)
        doc = self._load(url)
        items = self._block_items(doc, block)
        has_more = len(items) >= int(block.get("page_size") or self.page_size)
        return Page(items=items, page=page, has_more=has_more)

    # ---------------------------------------------------------------- 对外接口
    def categories(self) -> list[Category]:
        out: list[Category] = []
        for item in self.cfg.get("categories") or []:
            if isinstance(item, str):
                out.append(Category(token=item, label=item))
            elif isinstance(item, dict) and item.get("token"):
                out.append(
                    Category(
                        token=str(item["token"]),
                        label=str(item.get("label") or item["token"]),
                        kind=str(item.get("kind") or "genre"),
                    )
                )
        return out

    def browse(self, page: int = 1, category: str = "") -> Page:
        return self._page("browse", page, category=category or "")

    def search(self, query: str, page: int = 1) -> Page:
        return self._page("search", page, query=query or "", q=query or "")

    def detail(self, series: dict[str, Any]) -> dict[str, Any]:
        block = self.cfg.get("detail")
        if not isinstance(block, dict):
            return {}
        url = self._url(block.get("url") or "{url}", url=series.get("url") or "")
        doc = self._load(url)
        extra: dict[str, Any] = {}
        for name in ("author", "status", "summary", "latest"):
            if block.get(name):
                value = self._field(doc, block[name])
                if value:
                    extra[name] = value
        if block.get("tags"):
            tag = self._field(doc, block["tags"])
            if tag:
                extra["tags"] = [part for part in re.split(r"[,，/\s]+", tag) if part]
        return extra

    def chapters(self, series: dict[str, Any]) -> list[dict[str, Any]]:
        block = self.cfg.get("chapters")
        if not isinstance(block, dict):
            return []
        url = self._url(block.get("url") or "{url}", url=series.get("url") or "")
        doc = self._load(url)
        selector = str(block.get("list") or "").strip()
        nodes = doc.select(selector) if selector else []
        chapters: list[dict[str, Any]] = []
        for node in nodes:
            href = self._field(node, block.get("link") or "@href")
            if not href:
                continue
            title = self._field(node, block.get("title") or "")
            item: dict[str, Any] = {"url": _join(self.base, href)}
            if title:
                item["title"] = title
            if block.get("number"):
                number = self._field(node, block["number"])
                if number:
                    item["number"] = number
            chapters.append(item)
        if block.get("reverse"):
            chapters.reverse()
        return chapters

    def pages(self, series: dict[str, Any], chapter: dict[str, Any]) -> list[dict[str, Any]]:
        block = self.cfg.get("pages")
        if not isinstance(block, dict):
            return []
        chapter_url = str(chapter.get("url") or "")
        url = self._url(block.get("url") or "{url}", url=chapter_url, chapter_url=chapter_url)
        doc = self._load(url)
        selector = str(block.get("list") or "img").strip()
        attr = str(block.get("attr") or "src")
        nodes = doc.select(selector)
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for node in nodes:
            raw = node.attr(attr) or node.attr("src") or ""
            image = _join(self.base, raw)
            if not image or image in seen:
                continue
            pattern = block.get("re")
            if pattern and not re.search(str(pattern), image):
                continue
            seen.add(image)
            out.append({"index": len(out) + 1, "image_url": image, "url": image})
        return out

    def chapter_referer(self, series: dict[str, Any], chapter: dict[str, Any]) -> str:
        if self._referer:
            return self._referer
        return str(chapter.get("url") or series.get("url") or self.base_url)


def config_errors(cfg: Any) -> list[str]:
    """检查一份 JSON 图源配置，返回人话错误列表（空列表=没问题）。"""
    errors: list[str] = []
    if not isinstance(cfg, dict):
        return ["整个文件必须是一个 JSON 对象"]
    key = str(cfg.get("key") or "").strip()
    if not KEY_RE.match(key):
        errors.append("key 必须是小写字母开头、只含 a-z0-9_ 的 2-32 位字符串")
    if not str(cfg.get("name") or "").strip():
        errors.append("缺少 name（图源的显示名）")
    base = str(cfg.get("base_url") or "").strip()
    if not base.startswith(("http://", "https://")):
        errors.append("base_url 必须以 http:// 或 https:// 开头")
    for name in ("browse", "search", "chapters", "pages"):
        block = cfg.get(name)
        if block is None:
            continue
        if not isinstance(block, dict):
            errors.append(f"{name} 必须是一个对象")
            continue
        if name in ("chapters", "pages") and not block.get("list"):
            errors.append(f"{name}.list 不能为空")
        if name in ("browse", "search") and not block.get("list"):
            errors.append(f"{name}.list 不能为空")
    if not isinstance(cfg.get("chapters"), dict):
        errors.append("缺少 chapters（至少要知道章节目录怎么抓）")
    if not isinstance(cfg.get("pages"), dict):
        errors.append("缺少 pages（至少要知道图片怎么抓）")
    return errors
