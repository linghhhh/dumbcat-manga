"""呆猫漫画 · 图源插件 API（v1）

这是给**插件作者**看的稳定接口层。一个图源插件就是一个 Python 文件（或一个
带 ``plugin.json`` 的文件夹），里面写一个继承 :class:`SourcePlugin` 的类：

```python
from dumbcat.plugins.api import Page, SourcePlugin, series, chapter

class MySource(SourcePlugin):
    key = "my_source"           # 小写字母/数字/下划线，全局唯一
    name = "我的图源"
    base_url = "https://example.com"
    version = "1.0.0"
    author = "你"

    def search(self, query, page=1):
        doc = self.ctx.parse(self.ctx.http.get_text(
            f"{self.base_url}/search", params={"q": query, "page": page}
        ))
        items = [
            series(
                url=self.abs(a.attr("href")),
                title=a.text(),
                cover=self.abs(a.one("img").attr("src")) if a.one("img") else "",
            )
            for a in doc.select(".book-list .item a")
        ]
        return Page(items=items, page=page, has_more=bool(items))

    def chapters(self, s):
        root = self.ctx.parse(self.ctx.http.get_text(s["url"]))
        return [
            chapter(url=self.abs(a.attr("href")), title=a.text())
            for a in root.select(".chapter-list a")
        ]

    def pages(self, s, c):
        root = self.ctx.parse(self.ctx.http.get_text(c["url"]))
        return [self.abs(img.attr("src")) for img in root.select("#images img")]

PLUGIN = MySource
```

设计原则：

- **零第三方依赖**：插件只依赖标准库与这里的工具，不会因为装插件而破坏主程序。
- **只读站点公开页面**：不做登录绕过、付费解锁、验证码或访问控制规避。
- **失败要说话**：抓不到就抛异常（或返回空 ``Page``），界面会把错误显示成
  「图源名 + 原因」，不会静默吞掉。
- **字段能少写就少写**：只要提供 ``title`` / ``url``，其余（封面、作者、标签）
  可以是常见别名，主程序会自动归一化。

字段别名见 :func:`series` 与 README 的「字段速查」。
"""
from __future__ import annotations

import gzip
import io
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from dumbcat.app_config import USER_AGENT
from dumbcat.html_parser import HtmlNode, HtmlTreeParser
from dumbcat.html_query import query_all, query_one

#: 插件 API 版本。主程序只加载声明兼容的插件。
API_VERSION = 1

#: 单次 HTTP 请求的默认超时（秒）与重试次数
DEFAULT_TIMEOUT = 20.0
DEFAULT_RETRIES = 2


# ---------------------------------------------------------------------------
# 数据容器
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Category:
    """图源自带的一个分类（题材 / 状态 / 排序 / 专题）。"""

    token: str
    label: str
    kind: str = "genre"


@dataclass
class Page:
    """一页浏览或搜索结果。

    ``has_more`` 决定界面还能不能继续往下翻。插件如果不确定，可以返回
    ``has_more=len(items) >= page_size``，最差也只是多点一次「加载更多」。
    """

    items: list[dict[str, Any]] = field(default_factory=list)
    page: int = 1
    has_more: bool = False
    total: int | None = None
    note: str = ""


@dataclass
class ChatMessage:
    """图源给用户的一句提示（会显示成呆猫的气泡）。"""

    text: str
    level: str = "info"


# ---------------------------------------------------------------------------
# 字段构造器：允许常见别名，主程序归一化时都能认出来
# ---------------------------------------------------------------------------
def series(**fields: Any) -> dict[str, Any]:
    """构造一条作品数据。

    最少需要 ``title`` 与 ``url``（或 ``slug``）。常用字段：

    ==========  ==========================================================
    字段         说明
    ==========  ==========================================================
    title       标题
    url         详情页绝对地址（推荐）；也接受 slug / id
    cover       封面地址（别名：poster / thumbnail / image）
    author      作者（别名：artist / authors）
    status      连载状态
    tags        标签列表或逗号分隔字符串
    summary     简介
    latest      最新话
    ==========  ==========================================================
    """
    return dict(fields)


def chapter(**fields: Any) -> dict[str, Any]:
    """构造一条章节数据。

    最少需要 ``url``（或 id）。``title`` / ``name`` / ``number`` 用来显示；
    ``locked=True`` 表示付费/锁定章节（界面会标灰并跳过下载）。
    """
    return dict(fields)


# ---------------------------------------------------------------------------
# HTML 文档包装
# ---------------------------------------------------------------------------
class Doc:
    """对内置 HTML 解析器的轻量包装，提供 cheerio 风格的 ``select``。"""

    __slots__ = ("node",)

    def __init__(self, node: HtmlNode) -> None:
        self.node = node

    @classmethod
    def parse(cls, source: str) -> "Doc":
        return cls(HtmlTreeParser.parse(source or ""))

    def select(self, css: str) -> list["Doc"]:
        return [Doc(node) for node in query_all(self.node, css)]

    def one(self, css: str) -> "Doc | None":
        node = query_one(self.node, css)
        return Doc(node) if node is not None else None

    def text(self, css: str = "", default: str = "") -> str:
        if not css:
            return self.node.text_content().strip()
        node = query_one(self.node, css)
        return node.text_content().strip() if node is not None else default

    def attr(self, name: str, default: str = "") -> str:
        return self.node.attr(name) or default

    def get(self, name: str, default: str = "") -> str:
        return self.attr(name, default)

    def parent(self) -> "Doc | None":
        if self.node.parent is None:
            return None
        return Doc(self.node.parent)

    def __len__(self) -> int:
        return 1

    def __bool__(self) -> bool:
        return True

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Doc {self.node.tag} .{(self.node.attr('class') or '')[:40]}>"


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------
@dataclass
class Response:
    status: int
    headers: dict[str, str]
    content: bytes
    url: str = ""

    @property
    def text(self) -> str:
        return _decode(self.content, self.headers.get("content-type", ""))

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> "Response":
        if self.status >= 400:
            raise PluginError(f"HTTP {self.status}：{self.url}")
        return self


def _decode(raw: bytes, content_type: str = "") -> str:
    """按 header / meta / BOM 猜编码；中文站常见的 GBK 也能读。"""
    candidates: list[str] = []
    match = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if match:
        candidates.append(match.group(1))
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    head = raw[:4096].decode("ascii", "ignore")
    match = re.search(r"<meta[^>]+charset=[\"']?([\w-]+)", head, re.I)
    if match:
        candidates.append(match.group(1))
    candidates.extend(["utf-8", "gb18030", "big5", "latin-1"])
    for encoding in candidates:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


class Http:
    """插件用的同步 HTTP 客户端（带重试、gzip、中文编码兜底）。

    用法::

        resp = ctx.http.get("https://example.com/search", params={"q": "猫"})
        data = ctx.http.get_json("https://api.example.com/list")
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.log = logger or logging.getLogger("dumbcat.plugin.http")

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: bytes | str | None = None,
        json_body: Any = None,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        follow_redirects: bool = True,
    ) -> Response:
        if params:
            query = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None},
                doseq=True,
            )
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        body: bytes | None
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers = {**(headers or {}), "Content-Type": "application/json"}
        elif isinstance(data, str):
            body = data.encode("utf-8")
        else:
            body = data
        merged = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        }
        merged.update(headers or {})
        last_error: Exception | None = None
        for attempt in range(max(1, retries + 1)):
            try:
                request = urllib.request.Request(
                    url, data=body, headers=merged, method=method.upper()
                )
                # 自己处理重定向，才能保留同一套 headers
                opener = urllib.request.build_opener(
                    urllib.request.HTTPRedirectHandler() if follow_redirects
                    else _NoRedirect()
                )
                with opener.open(request, timeout=timeout) as response:
                    raw = response.read()
                    encoding = (response.headers.get("Content-Encoding") or "").lower()
                    if encoding == "gzip":
                        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                    elif encoding == "deflate":
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                    return Response(
                        status=int(response.status),
                        headers={k.lower(): v for k, v in response.headers.items()},
                        content=raw,
                        url=response.geturl(),
                    )
            except urllib.error.HTTPError as exc:
                # 4xx 重试没有意义，直接抛出可读错误
                if 400 <= exc.code < 500 and exc.code not in {408, 425, 429}:
                    raise PluginError(f"HTTP {exc.code}：{url}") from exc
                last_error = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
            if attempt < retries:
                time.sleep(min(2.0, 0.4 * (2**attempt)))
        raise PluginError(f"网络请求失败：{url}（{last_error}）")

    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        return self.request(url, method="POST", **kwargs)

    def get_bytes(self, url: str, **kwargs: Any) -> bytes:
        return self.request(url, **kwargs).content

    def get_text(self, url: str, **kwargs: Any) -> str:
        return self.request(url, **kwargs).text

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.request(url, **kwargs).json()

    def post_json(self, url: str, payload: Any, **kwargs: Any) -> Any:
        return self.request(url, method="POST", json_body=payload, **kwargs).json()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


# ---------------------------------------------------------------------------
# 简单磁盘缓存（插件可以给自己的抓取结果做短缓存）
# ---------------------------------------------------------------------------
class PluginCache:
    """插件私有缓存：``ctx.cache.get("key")`` / ``ctx.cache.set("key", value)``。

    值以 JSON 存盘，位于运行时目录，不会被主程序清理，适合缓存分类、签名等。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, key: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:120]
        return self.root / f"{safe}.json"

    def get(self, key: str, default: Any = None, ttl: float | None = None) -> Any:
        path = self._path(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
        if ttl is not None and time.time() - float(payload.get("t", 0)) > ttl:
            return default
        return payload.get("v", default)

    def set(self, key: str, value: Any) -> None:
        path = self._path(key)
        tmp = path.with_suffix(".tmp")
        try:
            with self._lock:
                tmp.write_text(
                    json.dumps({"t": time.time(), "v": value}, ensure_ascii=False),
                    encoding="utf-8",
                )
                tmp.replace(path)
        except OSError:
            pass

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 插件上下文与基类
# ---------------------------------------------------------------------------
class PluginContext:
    """传给插件的运行时上下文：HTTP、缓存、日志、路径。"""

    def __init__(
        self,
        key: str,
        plugin_dir: Path,
        cache_dir: Path,
        logger: logging.Logger | None = None,
    ) -> None:
        self.key = key
        self.plugin_dir = Path(plugin_dir)
        self.log = logger or logging.getLogger(f"dumbcat.plugin.{key}")
        self.http = Http(self.log)
        self.cache = PluginCache(cache_dir)

    @staticmethod
    def parse(html: str) -> Doc:
        return Doc.parse(html)

    @staticmethod
    def abs_url(base: str, url: str) -> str:
        return urllib.parse.urljoin(base, url)

    def sleep(self, seconds: float) -> None:
        """故意保留的友好限速入口；请遵守站点 robots / 访问频率。"""
        time.sleep(max(0.0, float(seconds)))


class SourcePlugin:
    """所有图源插件的基类。

    只有 :meth:`chapters` 与 :meth:`pages` 是必须实现的；浏览/搜索/分类
    按需覆写。方法都在**后台线程**里被调用，可以安全地阻塞。
    """

    #: 唯一 key：小写字母开头，仅 a-z0-9_，2-32 位
    key = ""
    #: 界面显示名
    name = ""
    #: 站点根地址（用于补全相对链接、打开官网）
    base_url = ""
    version = "1.0.0"
    author = ""
    description = ""
    #: 可选：一个 emoji 或短字符串，显示在界面上
    icon = "🐾"
    #: 内容语言，仅用于展示
    language = "zh"
    #: 是否 18+（界面显示小角标；不改变任何抓取逻辑）
    nsfw = False
    #: 一页条数提示（界面按这个估算是否还有下一页）
    page_size = 30

    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx

    # ---------- 浏览 ----------
    def categories(self) -> list[Category]:
        """站点自己的分类；默认没有。"""
        return []

    def browse(self, page: int = 1, category: str = "") -> Page:
        """不带关键词的浏览/推荐列表。"""
        return Page(items=[], page=page, has_more=False)

    def search(self, query: str, page: int = 1) -> Page:
        """关键词搜索。"""
        return Page(items=[], page=page, has_more=False)

    # ---------- 详情 ----------
    def detail(self, s: dict[str, Any]) -> dict[str, Any]:
        """可选：补充作者、简介、标签等详情字段（返回值会与 s 合并）。"""
        return {}

    def chapters(self, s: dict[str, Any]) -> list[dict[str, Any]]:
        """章节列表；每项至少要有 ``url``。"""
        raise NotImplementedError("插件没有实现 chapters()")

    def pages(self, s: dict[str, Any], c: dict[str, Any]) -> list[Any]:
        """某一章的图片地址列表；字符串或 ``{"url": ...}`` 都可以。"""
        raise NotImplementedError("插件没有实现 pages()")

    def referer(self, s: dict[str, Any], c: dict[str, Any]) -> str:
        """取图时带的 Referer（很多图床防盗链）。默认用章节页地址。"""
        return str(c.get("url") or self.base_url or "")

    # ---------- 工具 ----------
    def abs(self, url: str) -> str:
        """把相对地址补全成绝对地址。"""
        if not url:
            return ""
        return urllib.parse.urljoin(self.base_url, url)

    def fetch_image(self, url: str, referer: str = "") -> bytes:
        """走插件自己的 HTTP 客户端取图（需要 Cookie 时在这里加 headers）。"""
        headers = {"Referer": referer} if referer else None
        return self.ctx.http.get_bytes(self.abs(url), headers=headers)

    def message(self, text: str, level: str = "info") -> ChatMessage:
        """返回一条给用户看的提示（目前用于日志）。"""
        return ChatMessage(text=text, level=level)

    def uses_browser(self) -> bool:
        """插件是否自行驱动浏览器（内置 API 不提供，保留字段兼容）。"""
        return False

    def on_unload(self) -> None:
        """卸载/热重载时的清理钩子（可选）。"""
        return None


#: 更短的装饰器别名：``@plugin`` 等价于 ``@register``
def register(cls: type[SourcePlugin]) -> type[SourcePlugin]:
    """标记插件类（加载器也会自动发现模块里的 SourcePlugin 子类）。"""
    if not (isinstance(cls, type) and issubclass(cls, SourcePlugin)):
        raise TypeError("@register 只能用于 SourcePlugin 子类")
    cls.__dumbcat_plugin__ = True  # type: ignore[attr-defined]
    return cls


plugin = register


class PluginError(RuntimeError):
    """插件里可以直接抛这个异常，界面会原样展示 message。"""


__all__ = [
    "API_VERSION",
    "Category",
    "ChatMessage",
    "Doc",
    "Http",
    "Page",
    "PluginCache",
    "PluginContext",
    "PluginError",
    "Response",
    "SourcePlugin",
    "chapter",
    "plugin",
    "register",
    "series",
]
