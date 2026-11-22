"""JJ漫画（jjmhw.cc / 漫小肆韓漫）纯 HTTP 漫源。

实测确认过的标记（2026-10 与站点核对）：

| 用途 | 地址 / 选择器 |
|---|---|
| 列表 | `/booklist?page={n}`，每页 28 部；分类页 `/booklist?tag={标签}`、`/booklist?end={0或1}`（0 连载 / 1 完结） |
| 搜索 | `/search?keyword={关键词}`（站点对搜不到的词返回 0 条，属正常） |
| 作品条目 | `.mh-item`：标题 `h2.title a`，链接 `a[href^="/book/"]`，封面在 `.mh-cover` 的 **style 背景图**里（不是 `<img>`） |
| 作品详情 | `/book/{id}`：标题 `h1`；封面 `.banner_detail img`；`p.tip span.block`（状态/地区/更新时间/点击/标签）；简介 `.detail-list` 附近文本 |
| 章节 | `#detail-list-select a[href*="/chapter/"]`（另有"开始阅读"重复项，要跳过） |
| 阅读页 | `/chapter/{id}`：`.comiclist img` 的 `data-original`（实测 228 张） |
| 上一章/下一章 | `.fanye a` |

两个注意点：

1. **封面是 CSS 背景图**：`<p class="mh-cover" style="background-image: url(https://…/cover.jpg)">`，
   所以不能找 `<img>`，要从 style 里抠 URL。
2. 该站的图床是另一个域名（`jjmhw9.top`），下载图片要带 Referer，否则容易被拒。

这个站没有登录/收藏夹接口（Breeze 的插件也没有），所以不假造账号能力。
"""
from __future__ import annotations

import re
import threading
import urllib.parse
from typing import Any

from dumbcat.app_config import USER_AGENT
from dumbcat.errors import ApiError
from dumbcat.html_parser import HtmlNode, HtmlTreeParser
from dumbcat.html_query import (
    query_all,
    query_one,
    texts_of,
)
from dumbcat.sources.api import ApiClient
from dumbcat.sources.base import Category, PageResult, SeriesProvider

SITE_BASE = "https://www.jjmhw.cc"

#: 每页条目数（实测 .mh-item 28 个）
PAGE_SIZE = 28

#: 状态筛选（站点自带 end 参数）
STATUS_FILTERS: tuple[tuple[str, str], ...] = (
    ("end=0", "连载中"),
    ("end=1", "已完结"),
)

#: 题材标签：站点支持 ?tag= 过滤，但页面**不给标签清单**。这里只列出逐个实测过、
#: 确实能出结果的标签（插件里另外 11 个标签在本站返回 0 条，已剔除）。
TAGS: tuple[str, ...] = (
    "都市", "校园", "性感", "剧情", "恋爱", "后宫", "搞笑", "职场", "玄幻", "动作", "青春",
)

_BACKGROUND_URL_RE = re.compile(
    r"""background-image\s*:\s*url\(\s*['"]?([^'")]+)['"]?\s*\)""", re.IGNORECASE
)
_AID_RE = re.compile(r"/book/(\d+)")


class JjmhwProvider(SeriesProvider):
    key = "jjmhw"
    display_name = "JJ漫画"
    output_prefix = ("JJ漫画",)

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
                min_request_interval=0.15,
                session_key=self.key,
            )
        )

    def uses_browser(self) -> bool:
        return False

    def site_base_url(self) -> str:
        return SITE_BASE

    # ---------- 通用 ----------
    def _get_root(
        self,
        path: str,
        cancel_event: threading.Event | None = None,
    ) -> HtmlNode:
        text, final_url = self.api.request_text(path, cancel_event=cancel_event)
        if not text.strip():
            raise ApiError(f"{self.display_name} 返回了空页面", url=final_url)
        return HtmlTreeParser.parse(text)

    @staticmethod
    def _absolute(base: str, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if text.startswith("//"):
            return "https:" + text
        if text.startswith(("http://", "https://")):
            return text
        return urllib.parse.urljoin(f"{base}/", text.lstrip("/"))

    # ---------- 列表 ----------
    # ---------- 详情补充 ----------
    def detail(self, series: dict[str, Any]) -> dict[str, Any]:
        """详情页里的题材标签（站点用 a[href*=tag] 标注，通常一两个）。"""
        url = str(series.get("url") or series.get("_url") or "").strip()
        if not url:
            return {}
        root = self._get_root(url)
        tags: list[str] = []
        for anchor in query_all(root, 'a[href*="tag"]'):
            text = anchor.text_content().strip()
            if text and text not in tags:
                tags.append(text)
        if not tags:
            return {}
        return {"genres": tags[:12]}

    def categories(self, cancel_event: threading.Event | None = None) -> list[Category]:
        rows: list[Category] = [
            Category(token, label, "status") for token, label in STATUS_FILTERS
        ]
        rows.extend(Category(f"tag={tag}", tag, "genre") for tag in TAGS)
        return rows

    def _list_path(self, page: int, category: str) -> str:
        token = str(category or "").strip()
        params: dict[str, str] = {}
        if token.startswith("http://") or token.startswith("https://"):
            parsed = urllib.parse.urlparse(token)
            params.update(
                {key: value[0] for key, value in urllib.parse.parse_qs(parsed.query).items()}
            )
        elif token:
            for part in token.replace("&", ",").split(","):
                if "=" in part:
                    key, value = part.split("=", 1)
                    params[key.strip()] = value.strip()
        page = max(1, int(page or 1))
        params["page"] = str(page)
        return f"/booklist?{urllib.parse.urlencode(params)}"

    def _items_from(self, root: HtmlNode) -> list[dict[str, Any]]:
        """解析 `.mh-item` 列表条目。

        结构（实测）：
            <li><div class="mh-item">
                  <a href="/book/1246"><p class="mh-cover" style="background-image:url(封面)"></p></a>
                  <div class="mh-item-detali"><h2 class="title"><a href="/book/1246">标题</a></h2>
                      <div class="zl">…</div></div>
            </div></li>
        封面是 **背景图**，不是 <img>，所以要解 style。
        """
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for block in query_all(root, ".mh-item"):
            anchor = query_one(block, "h2.title a") or query_one(block, "a[href^='/book/']")
            if anchor is None:
                continue
            match = _AID_RE.search(anchor.attr("href") or "")
            if not match:
                continue
            comic_id = match.group(1)
            if comic_id in seen:
                continue
            seen.add(comic_id)
            title = anchor.text_content().strip() or query_one(block, "h2.title").text_content().strip() if query_one(block, "h2.title") else comic_id
            cover = ""
            cover_node = query_one(block, ".mh-cover")
            if cover_node is not None:
                matched = _BACKGROUND_URL_RE.search(cover_node.attr("style") or "")
                if matched:
                    cover = self._absolute(SITE_BASE, matched.group(1))
            subtitle = ""
            detail = query_one(block, ".mh-item-detali")
            if detail is not None:
                text = detail.text_content()
                # 去掉标题与"评分:"前缀，留简介
                text = text.replace(title, "", 1).strip()
                subtitle = re.sub(r"^评分[:：]\s*", "", text).strip()[:80]
            url = self._absolute(SITE_BASE, anchor.attr("href"))
            items.append(
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
                        "subtitle": subtitle,
                    }
                )
            )
        return items

    def browse_page(
        self,
        page: int = 1,
        category: str = "",
        cancel_event: threading.Event | None = None,
    ) -> PageResult:
        page = max(1, int(page or 1))
        root = self._get_root(self._list_path(page, category), cancel_event)
        items = self._items_from(root)
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
    def search_page(
        self,
        query: str,
        series_type: str = "manga",
        page: int = 1,
        cancel_event: threading.Event | None = None,
    ) -> PageResult:
        page = max(1, int(page or 1))
        keyword = str(query or "").strip()
        if series_type == "novel" or not keyword:
            return PageResult(page=page)
        params = {"keyword": keyword}
        if page > 1:
            params["page"] = str(page)
        root = self._get_root(
            f"/search?{urllib.parse.urlencode(params)}", cancel_event
        )
        # 站点会明确给出"共找 N 个相关内容"。搜不到时它仍然渲染一批无关的推荐条目，
        # 直接取 .mh-item 会把推荐当成搜索结果（实测：乱码关键词返回 19 条），
        # 所以先用站点自己的计数把关。
        total = _search_total(root)
        if total == 0:
            return PageResult(items=[], page=page, has_more=False, total=0)
        items = self._items_from(root)
        return PageResult(
            items=items,
            page=page,
            has_more=len(items) >= PAGE_SIZE,
            total=total,
        )

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
        comic_id = str(series.get("id") or series.get("slug") or "").strip()
        if not comic_id:
            return []
        root = self._get_root(f"/book/{comic_id}", cancel_event)
        chapters: list[dict[str, Any]] = []
        seen: set[str] = set()
        for anchor in query_all(root, "#detail-list-select a[href*='/chapter/']") or query_all(
            root, "a[href*='/chapter/']"
        ):
            href = anchor.attr("href")
            match = re.search(r"/chapter/(\d+)", href or "")
            if not match:
                continue
            chapter_id = match.group(1)
            label = anchor.text_content().strip()
            if not label or label in {"开始阅读", "開始閱讀", "开始观看"} or chapter_id in seen:
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
                        "url": self._absolute(SITE_BASE, href),
                        "isFree": True,
                        "price": 0,
                        "publishStatus": "PUBLIC",
                    },
                )
            )
        # 站点默认新→旧，阅读顺序要旧→新
        if len(chapters) > 1:
            chapters.sort(key=lambda item: _chapter_order(str(item.get("title") or "")))
            for index, chapter in enumerate(chapters, start=1):
                chapter["number"] = float(index)
        return chapters

    def get_chapter_pages(
        self,
        series: dict[str, Any],
        chapter: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        chapter_id = str(chapter.get("id") or chapter.get("slug") or "").strip()
        if not chapter_id:
            return []
        root = self._get_root(f"/chapter/{chapter_id}", cancel_event)
        pages: list[dict[str, Any]] = []
        for node in query_all(root, ".comiclist img") or query_all(root, "img[data-original]"):
            url = (
                node.attr("data-original")
                or node.attr("data-src")
                or node.attr("src")
            )
            url = self._absolute(SITE_BASE, url)
            if not url:
                continue
            pages.append(
                {"page_number": len(pages) + 1, "image_url": url}
            )
        return pages

    def chapter_referer(
        self,
        series: dict[str, Any],
        chapter: dict[str, Any],
    ) -> str:
        # 图床在另一个域名上，带站点 Referer 更稳
        return str(chapter.get("url") or f"{SITE_BASE}/")

    def fetch_image_bytes(self, url: str, referer: str = "") -> bytes:
        return super().fetch_image_bytes(url, referer or f"{SITE_BASE}/")

    # ---------- 详情字段（界面右侧信息） ----------
    def get_series_detail(
        self,
        series: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """补充详情页里的状态/地区/标签（拿不到就返回原样）。"""
        comic_id = str(series.get("id") or series.get("slug") or "").strip()
        if not comic_id:
            return series
        root = self._get_root(f"/book/{comic_id}", cancel_event)
        detail = dict(series)
        title = query_one(root, "h1")
        if title is not None and title.text_content().strip():
            detail["title"] = title.text_content().strip()
        cover = query_one(root, ".banner_detail img") or query_one(root, ".cover img")
        if cover is not None:
            detail["poster"] = self._absolute(SITE_BASE, cover.attr("src"))
        for block in query_all(root, "p.tip span.block"):
            text = block.text_content().strip()
            if "：" not in text and ":" not in text:
                continue
            key, _, value = text.replace("：", ":").partition(":")
            key, value = key.strip(), value.strip()
            if key in {"状态", "狀態"}:
                detail["status"] = value
            elif key in {"地区", "地區"}:
                detail["region"] = value
            elif key == "标签":
                detail["genres"] = [tag.strip() for tag in value.split() if tag.strip()]
            elif key == "点击":
                try:
                    detail["views"] = int(value.replace(",", ""))
                except ValueError:
                    pass
        description = query_one(root, ".detail-list") or query_one(root, ".desc")
        if description is not None:
            detail["description"] = description.text_content().strip()[:600]
        return detail


def _search_total(root: HtmlNode) -> int | None:
    """读站点自己的"共找 N 个相关内容"；读不到返回 None（不干预）。"""
    text = root.text_content()
    match = re.search(r"共\s*找\s*([\d,]+)\s*个相关", text)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _chapter_order(title: str) -> float:
    """按标题里的「第N话」给出排序键；没有序号就排到最后。"""
    match = re.search(r"第\s*(\d+(?:[.\-]\d+)?)\s*[話话回]", str(title or ""))
    if not match:
        return 1e9
    try:
        return float(match.group(1).replace("-", "."))
    except ValueError:
        return 1e9
