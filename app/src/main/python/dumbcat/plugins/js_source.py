"""JS 图源：一个 ``.js`` 文件就是一个图源，跑在内嵌 QuickJS 里。

为什么要有它：JSON 规则只能发 GET 抓 HTML；禁漫的 AES、WEBTOON 的 HMAC 签名、
wnacg 的 ``mReader.initData`` 这种内联 JS 数组，规则都表达不了，必须让图源
自己写代码。内嵌 QuickJS 让这点变成可能，同时保持「装一个文件就是一个源」。

文件放在 ``用户插件/`` 目录下。示例：

.. code-block:: js

    registerSource({
      key: 'example',
      name: '示例图源',
      baseUrl: 'https://example.com',
      icon: '🧪',

      browse: ({ page, baseUrl }) => {
        const html = Network.get(`${baseUrl}/list/${page}`);
        return { items: Html.parse(html).findAll('.book-item').map(node => ({
          title: node.get('a.title'),
          url: Tools.absUrl(baseUrl, node.get('a@href')),
          poster: node.get('img', 'data-src'),
        })), hasMore: true };
      },

      detail: ({ url }) => ({ author: '…', tags: ['…'] }),

      chapters: ({ url }) => Html.parse(Network.get(url))
        .findAll('.chapter-list a')
        .map((node, index) => ({ url: node.attr('href'), title: node.text(), number: index + 1 })),

      pages: ({ url }) => {
        const html = Network.get(url);
        const urls = Text.matchAll(html, /data-src="([^"]+)"/g);
        return { urls, referer: url };
      },

      // 可选：需要 Referer / Cookie / 解密时自己取图，返回 {base64} 或 {url, headers}
      fetchImage: ({ url, referer }) => ({ base64: Network.getBase64(url, { Referer: referer }) }),
    });

函数**必须是同步的**（宿主跨语言调用无法 await）。可用的全局能力见
``app/src/main/assets/appdata/jsruntime/prelude.js``: ``Network``、``Html``、
``Text``、``Json``、``Crypto``、``Tools``、``console``。
"""

from __future__ import annotations

import base64
import re
from typing import Any

from dumbcat.plugins.api import Category, Page, PluginError, SourcePlugin
from dumbcat.plugins import js_bridge

__all__ = ["JsSourcePlugin", "KEY_RE"]

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


def _join(base: str, value: str) -> str:
    """补全绝对地址（``//x`` 也算），和 JSON 图源保持一致。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        return "https:" + text
    if text.startswith(("http://", "https://", "data:")):
        return text
    if not base:
        return text
    return str(base).rstrip("/") + "/" + text.lstrip("/")


def _as_list(value: Any, key: str = "items") -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for name in (key, "items", "list", "chapters", "urls"):
            item = value.get(name)
            if isinstance(item, list):
                return item
    return []


class JsSourcePlugin(SourcePlugin):
    """把一个注册进 QuickJS 的 JS 图源包装成 ``SourcePlugin``。"""

    #: 由注册表在加载时塞进来
    SCRIPT = ""
    SCRIPT_PATH = ""
    META: dict[str, Any] = {}

    icon = "🧩"
    version = "1.0.0"

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        meta = dict(self.META or {})
        self.meta = meta
        key = str(meta.get("key") or self.key)
        #: 脚本里 registerSource 用的 key（与注册表里的 key 可能因重名而不同）
        self.js_key = key
        self.key = str(self.key or key)
        self.name = str(meta.get("name") or self.name or self.key)
        self.version = str(meta.get("version") or self.version or "1.0.0")
        self.base_url = str(meta.get("baseUrl") or self.base_url or "")
        self.icon = str(meta.get("icon") or self.icon)
        self.nsfw = bool(meta.get("nsfw"))
        self.page_size = int(meta.get("pageSize") or self.page_size or 30)
        self._referers: dict[str, str] = {}

    # ------------------------------------------------------------------ 内部
    def _call(self, fn: str, payload: dict[str, Any]) -> Any:
        try:
            return js_bridge.call(self.js_key, fn, payload)
        except js_bridge.JsError as exc:
            raise PluginError(str(exc)) from exc

    def _series_args(self, series: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(series.get("id") or series.get("uid") or ""),
            "url": str(series.get("url") or ""),
            "title": str(series.get("title") or ""),
            "baseUrl": self.base_url,
        }

    def _chapter_args(
        self, series: dict[str, Any], chapter: dict[str, Any]
    ) -> dict[str, Any]:
        args = self._series_args(series)
        args.update(
            {
                "chapterUrl": str(chapter.get("url") or ""),
                "chapterId": str(chapter.get("id") or chapter.get("uid") or ""),
                "chapterTitle": str(chapter.get("title") or ""),
                "number": chapter.get("number"),
            }
        )
        return args

    def _page(self, fn: str, payload: dict[str, Any], page: int) -> Page:
        raw = self._call(fn, payload)
        items: list[dict[str, Any]] = []
        for item in _as_list(raw):
            if not isinstance(item, dict):
                continue
            url = _join(self.base_url, str(item.get("url") or item.get("link") or ""))
            if not url:
                continue
            entry: dict[str, Any] = {
                "title": str(item.get("title") or "未命名作品"),
                "url": url,
            }
            poster = _join(
                self.base_url, str(item.get("poster") or item.get("cover") or "")
            )
            if poster:
                entry["poster"] = poster
            for name in ("author", "status", "latest", "summary"):
                value = str(item.get(name) or "").strip()
                if value:
                    entry[name] = value
            tags = item.get("tags")
            if isinstance(tags, str):
                tags = [part for part in re.split(r"[,，/\s]+", tags) if part]
            if isinstance(tags, list) and tags:
                entry["tags"] = [str(tag) for tag in tags if str(tag).strip()]
            series_id = str(item.get("id") or "").strip()
            if series_id:
                entry["id"] = series_id
            items.append(entry)
        has_more = True
        if isinstance(raw, dict) and "hasMore" in raw:
            has_more = bool(raw.get("hasMore"))
        elif len(items) < self.page_size:
            has_more = False
        return Page(items=items, page=page, has_more=has_more)

    # ---------------------------------------------------------------- 对外接口
    def categories(self) -> list[Category]:
        out: list[Category] = []
        try:
            raw = self._call("categories", {})
        except PluginError:
            return out
        for item in _as_list(raw, "categories"):
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
        return self._page(
            "browse",
            {
                "page": int(page),
                "category": str(category or ""),
                "baseUrl": self.base_url,
                "pageSize": self.page_size,
            },
            page,
        )

    def search(self, query: str, page: int = 1) -> Page:
        return self._page(
            "search",
            {
                "query": str(query or ""),
                "page": int(page),
                "baseUrl": self.base_url,
                "pageSize": self.page_size,
            },
            page,
        )

    def detail(self, series: dict[str, Any]) -> dict[str, Any]:
        try:
            raw = self._call("detail", self._series_args(series))
        except PluginError:
            # detail 是可选的，没实现就当没有补充字段
            return {}
        extra: dict[str, Any] = {}
        if not isinstance(raw, dict):
            return extra
        for name in ("author", "status", "summary", "latest"):
            value = str(raw.get(name) or "").strip()
            if value:
                extra[name] = value
        poster = _join(self.base_url, str(raw.get("poster") or raw.get("cover") or ""))
        if poster:
            extra["poster"] = poster
        tags = raw.get("tags")
        if isinstance(tags, str):
            tags = [part for part in re.split(r"[,，/\s]+", tags) if part]
        if isinstance(tags, list) and tags:
            extra["tags"] = [str(tag) for tag in tags if str(tag).strip()]
        return extra

    def chapters(self, series: dict[str, Any]) -> list[dict[str, Any]]:
        raw = self._call("chapters", self._series_args(series))
        chapters: list[dict[str, Any]] = []
        for item in _as_list(raw, "chapters"):
            if isinstance(item, str):
                url = _join(self.base_url, item)
                if url:
                    chapters.append({"url": url})
                continue
            if not isinstance(item, dict):
                continue
            url = _join(self.base_url, str(item.get("url") or item.get("link") or ""))
            if not url:
                continue
            entry: dict[str, Any] = {"url": url}
            title = str(item.get("title") or item.get("name") or "").strip()
            if title:
                entry["title"] = title
            number = item.get("number")
            if number not in (None, ""):
                entry["number"] = number
            chapter_id = str(item.get("id") or "").strip()
            if chapter_id:
                entry["id"] = chapter_id
            chapters.append(entry)
        return chapters

    def pages(self, series: dict[str, Any], chapter: dict[str, Any]) -> list[dict[str, Any]]:
        raw = self._call("pages", self._chapter_args(series, chapter))
        referer = ""
        if isinstance(raw, dict):
            referer = str(raw.get("referer") or raw.get("referrer") or "")
        urls = _as_list(raw, "urls") if isinstance(raw, dict) else _as_list(raw)
        chapter_url = str(chapter.get("url") or "")
        if referer and chapter_url:
            self._referers[chapter_url] = referer
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in urls:
            value = item.get("url") if isinstance(item, dict) else item
            image = _join(self.base_url, str(value or ""))
            if not image or image in seen:
                continue
            seen.add(image)
            out.append({"index": len(out) + 1, "image_url": image, "url": image})
        return out

    def referer(self, series: dict[str, Any], chapter: dict[str, Any]) -> str:
        chapter_url = str(chapter.get("url") or "")
        return self._referers.get(chapter_url) or chapter_url or self.base_url

    # 老接口名，JSON 图源与内置源都用这个
    chapter_referer = referer

    def fetch_image(self, url: str, referer: str = "") -> bytes:
        """图源自己取图（Referer / Cookie / 切条还原都靠它）。"""
        try:
            raw = self._call("fetchImage", {
                "url": str(url),
                "referer": str(referer),
                "baseUrl": self.base_url,
            })
        except PluginError as exc:
            if "不支持" not in str(exc):
                raise
            raw = None
        if isinstance(raw, dict):
            encoded = raw.get("base64")
            if encoded:
                try:
                    return base64.b64decode(str(encoded))
                except Exception as exc:  # noqa: BLE001
                    raise PluginError(f"JS 图源返回的 base64 不合法：{exc}") from exc
            target = str(raw.get("url") or "")
            if target:
                headers = {
                    str(name): str(value)
                    for name, value in (raw.get("headers") or {}).items()
                }
                if referer and "Referer" not in headers:
                    headers["Referer"] = str(referer)
                return self.ctx.http.get_bytes(self.abs(target), headers=headers or None)
        return super().fetch_image(url, referer)

    def uses_browser(self) -> bool:
        return False

    def on_unload(self) -> None:
        js_bridge.unload(self.js_key)
