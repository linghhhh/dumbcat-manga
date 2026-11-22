"""绅士漫画（wnacg）纯 HTTP 漫源。

以前这个站走浏览器（`BrowserSiteProvider`），单页列表要 1.5～3 秒。实测它其实
是服务端渲染站点，所有内容都能用普通 HTTP 拿到，改过来之后列表/搜索/章节都在
几百毫秒级，浏览器只在必要时兜底。

实测确认过的接口（2026-10 与站点核对）：

| 用途 | 地址 |
|---|---|
| 最新列表 | `/albums.html`，翻页 `/albums-index-page-{n}.html`（每页 20 部） |
| 分类列表 | `/albums-index-cate-{cid}.html`，翻页 `/albums-index-cate-{cid}-page-{n}.html` |
| 搜索 | `/search/?q={kw}&f=_all&s=create_time_DESC&syn=yes` |
| 作品详情 | `/photos-index-aid-{sid}.html` |
| 章节阅读页 | `/photos-slide-aid-{aid}-sid-{sid}.html` |
| 章节图片列表 | `/photos-item-aid-{aid}.html` → 内联 `mReader.initData({...})` 的 `page_url` |
| 登录页 | `/users-login.html` |
| 加入收藏 | `/users-save_fav-id-{sid}.html` |
| 取消收藏 | `/users-fav_del-aid-{sid}.html` |
| 我的收藏 | `/users-fav.html` |

两个必须注意的坑（都实测过）：

1. 图床对**纯 HTTP 直接 RST**，必须把图片地址从 `http://` 改成 `https://`
   才给图（封面还常写成协议相对的 `//t4.qy0.ru/...`）。
2. `/photos-item-aid-*.html` 里的数据是 **JS 字面量而不是严格 JSON**：有尾逗号，
   而且图片名里带方括号，用正则截数组会被提前截断。所以走
   `dumbcat.sources.jsdata` 的括号配平提取。
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.parse
import zipfile
from typing import Any

from dumbcat.app_config import IMAGE_EXTENSIONS, USER_AGENT
from dumbcat.errors import ApiError
from dumbcat.html_parser import HtmlNode, HtmlTreeParser
from dumbcat.html_query import query_all, query_one
from dumbcat.naming import sanitize_path_component
from dumbcat.sources.api import ApiClient
from dumbcat.sources.base import (
    NATIVE_COMPLETE_SUFFIX,
    Category,
    FavoriteFolder,
    LoginRequired,
    PageResult,
    SeriesProvider,
)
from dumbcat.sources.jsdata import extract_js_array, upgrade_to_https

SITE_BASE = "https://www.wnacg.com"

#: 每页条目数（站点固定 20）
PAGE_SIZE = 20

#: 收藏书架页（实测 /users-fav.html 是 404，正确的是 favclass）
FAVORITES_PATH = "/users-favclass.html"

#: 站点原生整本下载页（走 wcdn 的 worker 生成直链）
NATIVE_DOWNLOAD_PATH = "/download-index-aid-{series_id}.html"

#: 列表页里作品详情链接
_ITEM_LINK = 'a[href^="/photos-index-aid-"]'
#: 详情页里的章节链接
_CHAPTER_LINK = 'a[href^="/photos-slide-aid-"]'
#: 阅读入口按钮的文案（不是章节标题，实测会混进章节链接里）
_NAV_LABELS = {
    "開始閱讀",
    "开始阅读",
    "閱讀",
    "阅读",
    "下拉閱讀",
    "下拉阅读",
    "線上閱讀",
    "在线阅读",
    "continue reading",
}


class WnacgProvider(SeriesProvider):
    key = "wnacg"
    display_name = "绅士漫画"
    output_prefix = ("绅士漫画",)

    def __init__(self) -> None:
        super().__init__(
            ApiClient(
                SITE_BASE,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Referer": f"{SITE_BASE}/",
                },
                min_request_interval=0.12,
                session_key=self.key,
            )
        )

    def uses_browser(self) -> bool:
        """纯 HTTP 抓取；不需要浏览器。"""
        return False

    # ---------- 通用 ----------
    def site_base_url(self) -> str:
        return SITE_BASE

    def _get_html(
        self,
        path: str,
        cancel_event: threading.Event | None = None,
    ) -> tuple[HtmlNode, str]:
        text, final_url = self.api.request_text(path, cancel_event=cancel_event)
        if not text.strip():
            raise ApiError(f"{self.display_name} 返回了空页面", url=final_url)
        return HtmlTreeParser.parse(text), final_url

    def _items_from(self, root: HtmlNode, base_url: str = SITE_BASE) -> list[dict[str, Any]]:
        """解析列表页条目。

        标记结构（实测）：
            <div class="pic_box cate-1"><a href="/photos-index-aid-394359.html"
                 title="标题"><img src="//t4.qy0.ru/.../x.webp"></a></div>
            <div class="info"><div class="title"><a ...>标题</a></div>
                <div class="info_col">21張圖片， 創建於2026-10-09</div></div>
        标题和张数在兄弟节点里，所以用父节点回查。
        """
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for anchor in query_all(root, _ITEM_LINK):
            href = anchor.attr("href")
            match = href.rsplit("aid-", 1)[-1].split(".", 1)[0]
            comic_id = match.strip()
            if not comic_id or comic_id in seen:
                continue
            seen.add(comic_id)
            title = (
                anchor.attr("title").strip()
                or anchor.text_content().strip()
                or comic_id
            )
            cover = ""
            image = query_one(anchor, "img")
            if image is not None:
                cover = upgrade_to_https(
                    image.attr("data-original") or image.attr("src")
                )
            # 张数/创建时间在 .pic_box 的兄弟节点里
            info_text = ""
            container = anchor.parent
            if container is not None and container.parent is not None:
                info_node = query_one(container.parent, ".info_col")
                if info_node is not None:
                    info_text = info_node.text_content()
            url = urllib.parse.urljoin(f"{base_url}/", href.lstrip("/"))
            results.append(
                self.decorate_series(
                    {
                        "id": comic_id,
                        "slug": comic_id,
                        "title": title,
                        "type": "manga",
                        "status": "-",
                        "views": 0,
                        "url": url,
                        "_url": url,
                        "poster": cover,
                        "subtitle": info_text,
                    }
                )
            )
        return results

    # ---------- 详情补充 ----------
    def detail(self, series: dict[str, Any]) -> dict[str, Any]:
        """从相册页抓标签：.tagshow 里的一串关键词就是站点标签。"""
        url = str(series.get("url") or series.get("_url") or "").strip()
        if not url:
            return {}
        root, _ = self._get_html(url)
        tags = [
            anchor.text_content().strip()
            for anchor in query_all(root, ".tagshow")
        ]
        tags = [tag for tag in (item.strip() for item in tags) if tag]
        seen: set[str] = set()
        unique = [tag for tag in tags if not (tag in seen or seen.add(tag))]
        if not unique:
            return {}
        return {"genres": unique[:16]}

    # ---------- 分类 ----------
    def categories(self, cancel_event: threading.Event | None = None) -> list[Category]:
        root, _ = self._get_html("/albums.html", cancel_event)
        rows: list[Category] = []
        seen: set[str] = set()
        for anchor in query_all(root, 'a[href^="/albums-index-cate-"]'):
            href = anchor.attr("href")
            match = href.rsplit("cate-", 1)[-1].split(".", 1)[0]
            token = match.strip()
            label = anchor.text_content().strip() or anchor.attr("title").strip()
            if not token or not label or token in seen:
                continue
            seen.add(token)
            rows.append(Category(token, label, "genre"))
        return rows

    def _list_path(self, page: int, category: str) -> str:
        """列表页地址。

        站点用的是文件名分页而不是查询参数（实测）：
        - 最新：`/albums.html` → `/albums-index-page-{n}.html`
        - 分类：`/albums-index-cate-{cid}.html` → `/albums-index-cate-{cid}-page-{n}.html`
        """
        page = max(1, int(page or 1))
        token = str(category or "").strip()
        if token.startswith(("http://", "https://")):
            path = urllib.parse.urlparse(token).path
        elif token.startswith("/"):
            path = token
        elif token:
            path = f"/albums-index-cate-{token}.html"
        else:
            path = "/albums.html"
        path = re.sub(r"-page-\d+\.html$", ".html", path)
        if page <= 1:
            return path
        if path in {"/albums.html", "/"}:
            return f"/albums-index-page-{page}.html"
        if path.endswith(".html"):
            return f"{path[: -len('.html')]}-page-{page}.html"
        return f"{path}-page-{page}.html"

    def browse_page(
        self,
        page: int = 1,
        category: str = "",
        cancel_event: threading.Event | None = None,
    ) -> PageResult:
        page = max(1, int(page or 1))
        root, _ = self._get_html(self._list_path(page, category), cancel_event)
        items = self._items_from(root)
        # 站点每页固定 20 条：不足一页就说明到目录末尾了
        return PageResult(
            items=items,
            page=page,
            has_more=len(items) >= PAGE_SIZE,
            category=category,
        )

    def browse_series(
        self,
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        return self.browse_page(1, "", cancel_event).items

    # ---------- 搜索 ----------
    def _search_path(self, query: str, page: int) -> str:
        params = {
            "q": str(query or "").strip(),
            "f": "_all",
            "s": "create_time_DESC",
            "syn": "yes",
        }
        if page > 1:
            params["page"] = str(page)
        return f"/search/?{urllib.parse.urlencode(params)}"

    def search_page(
        self,
        query: str,
        series_type: str = "manga",
        page: int = 1,
        cancel_event: threading.Event | None = None,
    ) -> PageResult:
        page = max(1, int(page or 1))
        if series_type == "novel" or not str(query or "").strip():
            return PageResult(page=page)
        root, _ = self._get_html(self._search_path(query, page), cancel_event)
        items = self._items_from(root)
        return PageResult(items=items, page=page, has_more=len(items) >= PAGE_SIZE)

    def search_series(
        self,
        query: str,
        series_type: str = "manga",
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        return self.search_page(query, series_type, 1, cancel_event).items

    # ---------- 详情与章节 ----------
    def get_chapters(
        self,
        series: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        series_id = str(series.get("id") or series.get("slug") or "").strip()
        if not series_id:
            return []
        root, _ = self._get_html(f"/photos-index-aid-{series_id}.html", cancel_event)
        anchors = [
            anchor for anchor in query_all(root, _CHAPTER_LINK)
        ]
        chapters = self._chapters_from_anchors(series, anchors)
        if not chapters:
            # 单本画廊（很大一部分作品就是"一个画廊 N 张图"）：站点只有一个阅读
            # 入口，按钮文案是"開始閱讀/下拉閱讀"这种导航词，不能当章节标题。
            # 这种情况合成一个章节，用作品标题命名。
            fallback = self._single_gallery_chapter(series, anchors)
            if fallback is not None:
                chapters = [fallback]
        return sort_chapters_by_label(chapters)

    def _chapters_from_anchors(
        self,
        series: dict[str, Any],
        anchors: list[HtmlNode],
    ) -> list[dict[str, Any]]:
        chapters: list[dict[str, Any]] = []
        seen: set[str] = set()
        for anchor in anchors:
            href = anchor.attr("href")
            chapter_id = _chapter_id_from_href(href)
            if not chapter_id or chapter_id in seen:
                continue
            label = anchor.text_content().strip()
            if not label or label.casefold() in {name.casefold() for name in _NAV_LABELS}:
                continue
            seen.add(chapter_id)
            chapters.append(
                self.decorate_chapter(
                    series,
                    {
                        "id": chapter_id,
                        "slug": chapter_id,
                        "number": float(len(chapters) + 1),
                        "title": label,
                        "url": urllib.parse.urljoin(SITE_BASE + "/", href.lstrip("/")),
                        "isFree": True,
                        "price": 0,
                        "publishStatus": "PUBLIC",
                    },
                )
            )
        return chapters

    def _single_gallery_chapter(
        self,
        series: dict[str, Any],
        anchors: list[HtmlNode],
    ) -> dict[str, Any] | None:
        for anchor in anchors:
            href = anchor.attr("href")
            chapter_id = _chapter_id_from_href(href)
            if not chapter_id:
                continue
            title = str(series.get("title") or "全一話").strip()
            return self.decorate_chapter(
                series,
                {
                    "id": chapter_id,
                    "slug": chapter_id,
                    "number": 1.0,
                    "title": f"{title}（全一話）" if title else "全一話",
                    "url": urllib.parse.urljoin(SITE_BASE + "/", href.lstrip("/")),
                    "isFree": True,
                    "price": 0,
                    "publishStatus": "PUBLIC",
                },
            )
        return None

    def get_chapter_pages(
        self,
        series: dict[str, Any],
        chapter: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        chapter_id = str(chapter.get("id") or chapter.get("slug") or "").strip()
        if not chapter_id:
            return []
        referer = self.chapter_referer(series, chapter)
        text, _ = self.api.request_text(
            f"/photos-item-aid-{chapter_id}.html",
            headers={"Referer": referer} if referer else None,
            cancel_event=cancel_event,
        )
        urls = extract_js_array(text, "page_url")
        sizes = extract_js_array(text, "page_size")
        pages: list[dict[str, Any]] = []
        for index, value in enumerate(urls):
            url = upgrade_to_https(str(value or "").strip())
            if not url:
                continue
            page: dict[str, Any] = {"page_number": index + 1, "image_url": url}
            # 站点直接给了宽高，阅读器可以先用它排版，避免长条漫跳动
            if index < len(sizes) and isinstance(sizes[index], (list, tuple)):
                pair = list(sizes[index])
                if len(pair) >= 2:
                    try:
                        page["width"] = int(pair[0])
                        page["height"] = int(pair[1])
                    except (TypeError, ValueError):
                        pass
            pages.append(page)
        return pages

    def chapter_referer(
        self,
        series: dict[str, Any],
        chapter: dict[str, Any],
    ) -> str:
        return str(chapter.get("url") or series.get("_url") or SITE_BASE)

    def fetch_image_bytes(self, url: str, referer: str = "") -> bytes:
        return super().fetch_image_bytes(upgrade_to_https(url), referer)

    def download_file(
        self,
        url: str,
        destination: Any,
        referer: str = "",
        cancel_event: threading.Event | None = None,
    ) -> str:
        return super().download_file(
            upgrade_to_https(url), destination, referer, cancel_event
        )

    # ---------- 登录 ----------
    def login_url(self) -> str:
        return f"{SITE_BASE}/users-login.html"

    def open_login(self) -> None:
        from dumbcat.browser.hub import hub

        hub().show_window(self.login_url())

    def login_hint(self) -> str:
        return (
            "点「登录官方账号」在本程序的浏览器窗口里登录绅士漫画，"
            "登录完成后点「完成登录」；之后就能读取/写入站点的收藏书架。"
        )

    # ---------- 收藏（站点的「書架」，本身没有多收藏夹） ----------
    def favorite_folders(self) -> list[FavoriteFolder]:
        return [FavoriteFolder("", "書架")]

    def favorite_series(self, folder: str = "") -> list[dict[str, Any]]:
        if not self.is_logged_in():
            raise LoginRequired(
                "绅士漫画的收藏需要先登录官方账号", source=self.key
            )
        root, final_url = self._get_html(FAVORITES_PATH)
        if "users-login" in final_url or query_one(
            root, 'input[name="login_name"]'
        ) is not None:
            raise LoginRequired("登录态已失效，请重新登录绅士漫画", source=self.key)
        return self._items_from(root)

    def add_favorite(
        self,
        series: dict[str, Any],
        folder: FavoriteFolder | None = None,
    ) -> bool:
        if not self.is_logged_in():
            raise LoginRequired("收藏到绅士漫画书架需要先登录", source=self.key)
        series_id = str(series.get("id") or series.get("slug") or "").strip()
        if not series_id:
            return False
        text, _ = self.api.request_text(
            f"/users-save_fav-id-{series_id}.html",
            headers={"Referer": str(series.get("_url") or SITE_BASE)},
        )
        lowered = text.lower()
        if "users-login" in lowered:
            raise LoginRequired("登录态已失效，请重新登录绅士漫画", source=self.key)
        # 站点成功时不一定会给明确文案：只要不是报错页就按成功处理
        return "error" not in lowered[:200] or "success" in lowered

    def remove_favorite(self, series: dict[str, Any]) -> bool:
        if not self.is_logged_in():
            raise LoginRequired("取消绅士漫画收藏需要先登录", source=self.key)
        series_id = str(series.get("id") or series.get("slug") or "").strip()
        if not series_id:
            return False
        text, _ = self.api.request_text(
            f"/users-fav_del-aid-{series_id}.html",
            headers={"Referer": str(series.get("_url") or SITE_BASE)},
        )
        return "刪除收藏成功" in text or "删除收藏成功" in text or "success" in text.lower()

    # ---------- 浏览器兜底（整本下载线路被 Cloudflare 挡住时用） ----------
    def browser_post_json(
        self,
        url: str,
        payload: dict[str, Any],
        referer: str,
    ) -> dict[str, Any]:
        """在共享 Chromium 的**真实页面里**发这个请求。

        下载线路的 worker 域名有 Cloudflare 防护：直接请求（甚至用浏览器网络层
        直接请求）都会拿到 "Just a moment..." 挑战页。站点的下载页自己也是用
        页面里的 fetch 调它，所以这里先打开下载页，再用 page.evaluate 里的
        fetch 发出同样的请求——带着 JS 执行环境与 Cloudflare 放行 Cookie。
        """
        from dumbcat.browser.hub import hub

        script = """
        async ([url, payload, referer]) => {
            try {
                const response = await fetch(url, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Accept': 'application/json',
                        'Referer': referer,
                    },
                    body: JSON.stringify(payload),
                    credentials: 'include',
                });
                return { status: response.status, text: await response.text() };
            } catch (error) {
                return { status: 0, text: String(error) };
            }
        }
        """

        def work() -> dict[str, Any]:
            page = hub().page_for(f"site:{self.key}")
            try:
                page.goto(referer, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(1200)
            except Exception:  # noqa: BLE001 - 页面没开成也继续尝试
                pass
            result = page.evaluate(script, [url, payload, referer])
            if not isinstance(result, dict):
                return {}
            text = str(result.get("text") or "")
            if _looks_blocked(text):
                return {}
            try:
                data = json.loads(text)
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}

        try:
            return dict(hub().run(work, timeout=180.0))
        except Exception:  # noqa: BLE001 - 兜底失败就当作没有直链
            return {}

    def browser_get_bytes(self, url: str, referer: str) -> bytes:
        """用共享 Chromium 的网络层取二进制（ZIP 直链）。

        直链本身是 CDN 的普通 GET，Cloudflare 放行 Cookie 已经在同一个浏览器
        会话里，所以这里用浏览器的 request API 取就行。
        """
        from dumbcat.browser.hub import hub

        def work() -> bytes:
            page = hub().page_for(f"site:{self.key}")
            try:
                if not page.url or "wnacg" not in str(page.url):
                    page.goto(f"{SITE_BASE}/", wait_until="domcontentloaded", timeout=60000)
            except Exception:  # noqa: BLE001
                pass
            request = hub().request_context(f"site:{self.key}")
            response = request.get(url, headers={"Referer": referer}, timeout=180000)
            return bytes(response.body())

        try:
            return bytes(hub().run(work, timeout=300.0))
        except Exception:  # noqa: BLE001
            return b""

    # ---------- 站点原生整本下载 ----------
    def download_native(
        self,
        series: dict[str, Any],
        chapters: list[dict[str, Any]],
        output_root: Any,
        emit: Any,
        cancel_event: threading.Event,
    ) -> bool | None:
        """走站点自己的"下载漫画"线路整本拿 ZIP。

        以前这条线路靠浏览器点页面拿直链；实测下载页把直链参数写在内联脚本里
        （`WORKER_API` / `FILE_KEY` / `FILE_NAME`），用普通 HTTP 就能拿到，所以
        换成纯 HTTP 实现，不再为下载启动浏览器。
        """
        from pathlib import Path

        series_id = str(series.get("id") or series.get("slug") or "").strip()
        if not series_id or cancel_event.is_set():
            return None
        series_name = sanitize_path_component(
            series.get("title") or series.get("slug"), "Unknown Series", 90
        )
        series_root = self.output_root(Path(output_root), series) / series_name
        chapter_root = series_root / "Chapter 1 - 官方下载"
        archive_path = series_root / ".native-download.zip"
        if chapter_root.is_dir() and any(
            child.is_file() and child.name.endswith(NATIVE_COMPLETE_SUFFIX)
            for child in chapter_root.iterdir()
        ):
            emit("log", message="绅士漫画已存在完整的官方下载，已跳过。")
            return True

        try:
            emit("log", message="正在通过绅士漫画官方下载线路获取整本 …")
            page, _ = self.api.request_text(
                NATIVE_DOWNLOAD_PATH.format(series_id=series_id),
                headers={"Referer": f"{SITE_BASE}/photos-index-aid-{series_id}.html"},
            )
            routes = _native_routes(self, page, series_id, cancel_event)
            if not routes:
                return None
            body = _fetch_native_zip(self, routes, cancel_event)
            if not body or not body.startswith(b"PK"):
                return None
            series_root.mkdir(parents=True, exist_ok=True)
            archive_path.write_bytes(body)
            extracted = _extract_archive(archive_path, chapter_root, cancel_event)
            if extracted is None:
                return False
            if extracted <= 0:
                return None
            try:
                (chapter_root / f"{series_name}{NATIVE_COMPLETE_SUFFIX}").write_text(
                    f"pages={extracted}\n", encoding="utf-8"
                )
            except OSError:
                pass
            emit(
                "log",
                message=f"绅士漫画官方下载完成，共 {extracted} 页。",
                level="success",
            )
            return True
        except ApiError as exc:
            emit("log", message=f"官方下载线路不可用（{exc}），改用逐页下载。")
            return None
        finally:
            try:
                archive_path.unlink(missing_ok=True)
            except OSError:
                pass


def _chapter_id_from_href(href: str) -> str:
    """从 `/photos-slide-aid-368785-sid-390062.html` 里取出 aid。"""
    text = str(href or "")
    if "aid-" not in text:
        return ""
    tail = text.split("aid-", 1)[1]
    return tail.split("-", 1)[0].split(".", 1)[0].strip()


def _js_string(text: str, key: str) -> str:
    """从内联脚本里取 `KEY: 'value'` / `KEY = "value"` 形式的字符串常量。"""
    match = re.search(
        rf"""{re.escape(key)}\s*[:=]\s*['"]([^'"]+)['"]""", str(text or "")
    )
    return match.group(1).strip() if match else ""


def _looks_blocked(text: str) -> bool:
    """识别 Cloudflare 的挑战页（"Just a moment..."）。"""
    lowered = str(text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "just a moment",
            "cf_chl",
            "cf-browser-verification",
            "enable javascript and cookies",
            "checking your browser",
        )
    )


def _native_routes(
    provider: "WnacgProvider",
    page: str,
    series_id: str,
    cancel_event: threading.Event,
) -> list[str]:
    """取得官方下载线路的候选直链。

    下载线路的 worker 接口（`d1.wcdn.date/api/generate-link`）有 Cloudflare
    防护，普通 HTTP 会拿到挑战页；这时用共享 Chromium 的网络层再请求一次
    （只在这一步用浏览器，浏览/搜索仍然是纯 HTTP）。
    """
    routes: list[str] = []
    worker = _js_string(page, "WORKER_API")
    file_key = _js_string(page, "FILE_KEY")
    file_name = _js_string(page, "FILE_NAME")
    referer = NATIVE_DOWNLOAD_PATH.format(series_id=series_id)
    payload = {"file_key": file_key, "file_name": file_name}

    def absorb(data: Any) -> None:
        if not isinstance(data, dict):
            return
        for key in ("url", "link", "download_url"):
            value = data.get(key)
            if isinstance(value, str) and value.startswith("http"):
                routes.append(value)
                return
        nested = data.get("data")
        if isinstance(nested, str) and nested.startswith("http"):
            routes.append(nested)
        elif isinstance(nested, dict):
            absorb(nested)

    if worker and file_key and not cancel_event.is_set():
        blocked = False
        try:
            raw, _final, _charset = provider.api.post_json(
                worker, payload, headers={"Referer": referer}, cancel_event=cancel_event
            )
            text = raw.decode("utf-8", "replace")
            blocked = _looks_blocked(text)
            if not blocked:
                absorb(json.loads(text))
        except (ApiError, ValueError) as exc:
            blocked = _looks_blocked(str(exc))
        if blocked or not routes:
            absorb(provider.browser_post_json(worker, payload, referer))

    routes.extend(
        url
        for url in dict.fromkeys(
            re.findall(r"https?://[^\"'\s]+\.zip[^\"'\s]*", str(page or ""))
        )
        if url not in routes
    )
    return routes


def _fetch_native_zip(
    provider: "WnacgProvider",
    routes: list[str],
    cancel_event: threading.Event,
) -> bytes:
    """按候选直链依次尝试下载 ZIP，返回第一个真的是 ZIP 的响应体。"""
    for route in routes:
        if cancel_event.is_set():
            return b""
        try:
            body, _final, _charset = provider.api.request_bytes(
                route,
                headers={"Referer": f"{SITE_BASE}/"},
                cancel_event=cancel_event,
            )
            if body.startswith(b"PK"):
                return body
            if not _looks_blocked(body[:4096].decode("utf-8", "replace")):
                continue
        except ApiError:
            pass
        # 被 Cloudflare 挡了：换浏览器网络层再试
        body = provider.browser_get_bytes(route, f"{SITE_BASE}/")
        if body.startswith(b"PK"):
            return body
    return b""


def _extract_archive(
    archive_path: Any,
    chapter_root: Any,
    cancel_event: threading.Event,
) -> int | None:
    """把 ZIP 里的图片按页号解出来；取消时返回 None 并清掉半成品。"""
    from pathlib import Path

    folder = Path(chapter_root)
    if folder.is_dir():
        for child in folder.iterdir():
            if child.is_file() and (
                child.name.endswith(".part")
                or child.suffix.lower() in IMAGE_EXTENSIONS
            ):
                try:
                    child.unlink()
                except OSError:
                    pass
    folder.mkdir(parents=True, exist_ok=True)
    extracted = 0
    try:
        with zipfile.ZipFile(archive_path) as archive:
            names = sorted(
                name
                for name in archive.namelist()
                if not name.endswith("/")
                and name.lower().endswith(tuple(IMAGE_EXTENSIONS))
            )
            for index, name in enumerate(names, start=1):
                if cancel_event.is_set():
                    return None
                extension = Path(name).suffix.lower() or ".jpg"
                target = folder / f"{index:04d}{extension}"
                partial = target.with_name(f"{target.name}.part")
                try:
                    with archive.open(name) as source, partial.open("wb") as output:
                        while True:
                            if cancel_event.is_set():
                                raise ApiError("操作已停止")
                            chunk = source.read(256 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                    os.replace(partial, target)
                except BaseException:
                    try:
                        partial.unlink()
                    except OSError:
                        pass
                    raise
                extracted += 1
    except (OSError, zipfile.BadZipFile):
        return 0
    return extracted


def sort_chapters_by_label(chapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按标题里的「第N話」排序；标题里没有序号就保持站点给的顺序。"""
    if len(chapters) < 2:
        return chapters

    def number_of(item: dict[str, Any]) -> float | None:
        match = re.search(
            r"第\s*(\d+(?:[.\-]\d+)?)\s*[話话回卷]", str(item.get("title") or "")
        )
        if not match:
            return None
        try:
            return float(match.group(1).replace("-", "."))
        except ValueError:
            return None

    numbers = [number_of(item) for item in chapters]
    if any(number is None for number in numbers):
        return chapters
    order = sorted(range(len(chapters)), key=lambda index: numbers[index])
    if order == list(range(len(chapters))):
        return chapters
    return [chapters[index] for index in order]
