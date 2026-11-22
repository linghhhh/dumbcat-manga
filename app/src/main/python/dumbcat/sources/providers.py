"""各漫源 provider 实现与漫源注册表。

由原 manga_core.py 拆分而来（行为不变）：纯接口漫源（QiManga / Comix /
Manga18fx / 禁漫天堂 / 拷贝漫画）与浏览器型漫源（绅士漫画 / 漫画柜 /
包子漫画 / 哔哩哔哩 / 快看）都在这里，浏览器部分统一走 dumbcat.browser 的
共享 Chromium 会话。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

# 进程临时目录必须在使用浏览器的模块被导入之前指向运行时目录；该调用是幂等的，
# dumbcat.app_config 导入时也会做同样一件事。
from dumbcat.runtime_cleanup import configure_process_temp

configure_process_temp()

from dumbcat.app_config import (
    BAOZIMH_SITE_BASE,
    COMIC18_SITE_BASE,
    COMIX_IMAGE_HEADERS,
    COMIX_SITE_BASE,
    COPYMANGA_API_BASE,
    COPYMANGA_SITE_BASE,
    IMAGE_EXTENSIONS,
    JM_API_BASE,
    JM_IMAGE_BASE,
    MANGA18FX_HTML_HEADERS,
    MANGA18FX_SITE_BASE,
    QIMANGA_API_BASE,
    QIMANGA_API_HEADERS,
    QIMANGA_API_V2_BASE,
    QIMANGA_SITE_BASE,
    USER_AGENT,
    WNACG_SITE_BASE,
)
from dumbcat.sources.jm_scramble import unscramble as _jm_unscramble

log = logging.getLogger("dumbcat.sources.providers")

from dumbcat.naming import (
    chapter_number_text,
    chapter_sort_key,
    sanitize_path_component,
)

from dumbcat.errors import ApiError, normalize_image_url
from dumbcat.html_parser import HtmlNode, HtmlTreeParser
from dumbcat.browser.comix import BrowserError, ComixBrowserClient
from dumbcat.browser.site import SiteBrowserClient, SiteBrowserError
from dumbcat.sources.api import ApiClient, JmApiClient
from dumbcat.sources.base import (
    NATIVE_COMPLETE_SUFFIX,
    Category,
    FavoriteFolder,
    LoginRequired,
    PageResult,
    SeriesProvider,
)
from dumbcat.sources.sites.jjmhw import JjmhwProvider
from dumbcat.sources.sites.wnacg import WnacgProvider
from dumbcat.sources.sites.webtoon import WebtoonProvider


class QiMangaProvider(SeriesProvider):
    key = "qimanga"
    display_name = "QiManga"
    output_prefix = ("QiManga",)

    def __init__(self) -> None:
        super().__init__(ApiClient(QIMANGA_API_BASE, headers=QIMANGA_API_HEADERS))
        self.api_v2 = ApiClient(
            QIMANGA_API_V2_BASE,
            headers=QIMANGA_API_HEADERS,
            min_request_interval=0.16,
        )

    def search_series(
        self,
        query: str,
        series_type: str = "manga",
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        if series_type == "novel":
            return []
        payload = self.api.request_json(
            "/series/search",
            {"q": query.strip(), "page": 1, "perPage": 50},
            cancel_event,
        )
        if isinstance(payload, dict):
            data = payload.get("data", [])
        else:
            data = payload
        if not isinstance(data, list):
            raise ApiError("QiManga 搜索接口返回格式异常")
        results = [item for item in data if isinstance(item, dict)]
        if series_type == "manga":
            comic_types = {"MANGA", "MANHWA", "MANHUA", "COMIC"}
            results = [
                item
                for item in results
                if str(item.get("type") or "").upper() in comic_types
            ]
        return [self.decorate_series(item) for item in results]

    def browse_series(
        self,
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        # 注意：/series/search 要求 q 至少 2 个字符，用空 q 浏览会直接报错
        # （实测 "q must be longer than or equal to 2 characters"）。
        # 浏览应改用专门的列表接口 /series。
        payload = self.api.request_json(
            "/series",
            {"page": 1, "perPage": 60},
            cancel_event,
        )
        if isinstance(payload, dict):
            data = payload.get("data", [])
        else:
            data = payload
        if not isinstance(data, list):
            raise ApiError("QiManga 浏览接口返回格式异常")
        comic_types = {"MANGA", "MANHWA", "MANHUA", "COMIC"}
        results = [
            item for item in data
            if isinstance(item, dict)
            and str(item.get("type") or "").upper() in comic_types
        ]
        return [self.decorate_series(item) for item in results]

    # ---------- 分类与分页 ----------
    #: QiManga 的 /series 接口支持按类型与连载状态筛选，官方站点左侧栏用的就是它们
    COMIC_TYPES = ("MANGA", "MANHWA", "MANHUA", "COMIC")
    TYPE_LABELS = {
        "MANGA": "日漫",
        "MANHWA": "韩漫",
        "MANHUA": "国漫",
        "COMIC": "美漫",
    }

    def categories(self, cancel_event: threading.Event | None = None) -> list[Category]:
        rows = [Category("", "全部", "type")]
        rows.extend(
            Category(f"type={name}", self.TYPE_LABELS.get(name, name), "type")
            for name in self.COMIC_TYPES
        )
        rows.extend(
            (
                Category("status=ONGOING", "连载中", "status"),
                Category("status=COMPLETED", "已完结", "status"),
                Category("status=HIATUS", "休刊", "status"),
            )
        )
        return rows

    def _page_payload(self, page: int, per_page: int, cancel_event: threading.Event | None) -> Any:
        return self.api.request_json(
            "/series",
            {"page": max(1, int(page or 1)), "perPage": per_page},
            cancel_event,
        )

    def browse_page(
        self,
        page: int = 1,
        category: str = "",
        cancel_event: threading.Event | None = None,
    ) -> PageResult:
        page = max(1, int(page or 1))
        per_page = 60
        filters: dict[str, str] = {}
        for part in str(category or "").replace("&", ",").split(","):
            if "=" in part:
                key, value = part.split("=", 1)
                filters[key.strip()] = value.strip()
        params: dict[str, Any] = {"page": page, "perPage": per_page}
        params.update(filters)
        payload = self.api.request_json("/series", params, cancel_event)
        data = payload.get("data", []) if isinstance(payload, dict) else payload
        if not isinstance(data, list):
            raise ApiError("QiManga 浏览接口返回格式异常")
        wanted_type = filters.get("type", "")
        wanted_status = filters.get("status", "")
        items: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").upper()
            if wanted_type:
                if item_type != wanted_type.upper():
                    continue
            elif item_type not in self.COMIC_TYPES:
                continue
            if wanted_status and str(item.get("status") or "").upper() != wanted_status.upper():
                continue
            items.append(self.decorate_series(item))
        total: int | None = None
        total_pages = 0
        if isinstance(payload, dict):
            for key in ("totalItems", "total"):
                value = payload.get(key)
                if isinstance(value, (int, float)) and value > 0:
                    total = int(value)
                    break
            value = payload.get("totalPages")
            if isinstance(value, (int, float)):
                total_pages = int(value)
        has_more = (page < total_pages) if total_pages else (len(data) >= per_page)
        return PageResult(
            items=items,
            page=page,
            has_more=has_more,
            total=total,
            category=category,
        )

    def search_page(
        self,
        query: str,
        series_type: str = "manga",
        page: int = 1,
        cancel_event: threading.Event | None = None,
    ) -> PageResult:
        page = max(1, int(page or 1))
        if series_type == "novel":
            return PageResult(page=page)
        payload = self.api.request_json(
            "/series/search",
            {"q": query.strip(), "page": page, "perPage": 50},
            cancel_event,
        )
        data = payload.get("data", []) if isinstance(payload, dict) else payload
        if not isinstance(data, list):
            raise ApiError("QiManga 搜索接口返回格式异常")
        items = [item for item in data if isinstance(item, dict)]
        if series_type == "manga":
            items = [
                item for item in items
                if str(item.get("type") or "").upper() in self.COMIC_TYPES
            ]
        total_pages = 0
        if isinstance(payload, dict) and isinstance(payload.get("totalPages"), (int, float)):
            total_pages = int(payload["totalPages"])
        return PageResult(
            items=[self.decorate_series(item) for item in items],
            page=page,
            has_more=page < total_pages if total_pages else len(items) >= 50,
        )

    def detail(self, series: dict[str, Any]) -> dict[str, Any]:
        """补详情字段：题材标签、作者、简介。

        列表接口只给标题/封面/状态，题材要再打一次 ``/series/{slug}``；
        拿不到就静默返回空字典，页面照常用列表字段渲染。
        """
        slug = str(series.get("slug") or "").strip()
        if not slug:
            return {}
        payload = self.api.request_json(f"/series/{urllib.parse.quote(slug, safe='')}")
        data = payload.get("data") if isinstance(payload, dict) else None
        item = data if isinstance(data, dict) else (payload if isinstance(payload, dict) else {})
        if not item:
            return {}
        extra: dict[str, Any] = {}
        genres = item.get("genres")
        if isinstance(genres, list) and genres:
            extra["genres"] = genres
        for key in ("author", "description"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                extra[key] = value.strip()
        return extra

    def get_chapters(
        self,
        series: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        slug = urllib.parse.quote(str(series.get("slug") or ""), safe="")
        if not slug:
            raise ApiError("作品缺少 slug，无法加载 QiManga 章节。")
        chapters: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(100):
            query = {"cursor": cursor} if cursor else None
            payload = self.api_v2.request_json(
                f"/series/{slug}/chapters",
                query,
                cancel_event,
            )
            if not isinstance(payload, dict):
                raise ApiError("QiManga 章节列表接口返回格式异常")
            data = payload.get("data", [])
            if isinstance(data, list):
                chapters.extend(item for item in data if isinstance(item, dict))
            if not payload.get("hasMore"):
                break
            next_cursor = payload.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            if next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise ApiError("QiManga 章节分页过多，已停止加载")
        return [self.decorate_chapter(series, item) for item in chapters]

    def get_chapter_pages(
        self,
        series: dict[str, Any],
        chapter: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        if self.is_locked(chapter):
            return []
        series_slug = urllib.parse.quote(str(series.get("slug") or ""), safe="")
        chapter_slug = urllib.parse.quote(str(chapter.get("slug") or ""), safe="")
        if not series_slug or not chapter_slug:
            return []
        payload = self.api.request_json(
            f"/series/{series_slug}/chapters/{chapter_slug}",
            cancel_event=cancel_event,
        )
        if not isinstance(payload, dict) or self.is_locked(payload):
            return []
        images = payload.get("images")
        if not isinstance(images, list):
            return []
        return [item for item in images if isinstance(item, dict)]

    def chapter_referer(
        self,
        series: dict[str, Any],
        chapter: dict[str, Any],
    ) -> str:
        series_slug = urllib.parse.quote(str(series.get("slug") or ""), safe="")
        chapter_slug = urllib.parse.quote(str(chapter.get("slug") or ""), safe="")
        return f"{QIMANGA_SITE_BASE}/series/{series_slug}/{chapter_slug}"


COMIX_CHAPTER_RE = re.compile(
    r"(?:ch(?:apter)?[.\s-]*)(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


class ComixProvider(SeriesProvider):
    key = "comix"
    display_name = "Comix"
    output_prefix = ("Comix",)

    def __init__(self) -> None:
        super().__init__(
            ApiClient(
                COMIX_SITE_BASE,
                headers=COMIX_IMAGE_HEADERS,
                min_request_interval=0.12,
            )
        )
        self.browser = ComixBrowserClient()

    @staticmethod
    def _chapter_number(*values: Any) -> float | None:
        for value in values:
            match = COMIX_CHAPTER_RE.search(str(value or ""))
            if not match:
                continue
            try:
                return float(match.group(1))
            except ValueError:
                continue
        return None

    def search_series(
        self,
        query: str,
        series_type: str = "manga",
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        if series_type == "novel":
            return []
        try:
            rows = self.browser.search(query, cancel_event)
        except BrowserError as exc:
            raise ApiError(str(exc)) from exc
        results: list[dict[str, Any]] = []
        for row in rows:
            href = str(row.get("href") or "")
            if not href.startswith("/title/"):
                continue
            slug = href.split("/title/", 1)[1].split("/", 1)[0]
            item = self.decorate_series(
                {
                    "id": slug,
                    "hid": slug,
                    "slug": slug,
                    "title": row.get("title") or slug,
                    "type": str(row.get("type") or "manga").lower(),
                    "status": row.get("status") or "-",
                    "views": 0,
                    "url": href,
                    "_url": COMIX_SITE_BASE + href,
                }
            )
            results.append(item)
        return results

    @staticmethod
    def _rows_to_series(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把浏览器读回的卡片行转成统一的作品结构。"""
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            href = str(row.get("href") or "")
            if not href.startswith("/title/"):
                continue
            slug = href.split("/title/", 1)[1].split("/", 1)[0]
            if not slug or slug in seen:
                continue
            seen.add(slug)
            results.append({
                "id": slug, "hid": slug, "slug": slug,
                "title": row.get("title") or slug,
                "type": str(row.get("type") or "manga").lower(),
                "status": row.get("status") or "-", "views": 0,
                "url": href, "_url": COMIX_SITE_BASE + href,
                "poster": row.get("poster") or "",
            })
        return results

    def browse_series(
        self,
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        try:
            rows = self.browser.browse(cancel_event)
        except BrowserError as exc:
            raise ApiError(str(exc)) from exc
        return [self.decorate_series(item) for item in self._rows_to_series(rows)]

    # ---------- 分类与分页 ----------
    def uses_browser(self) -> bool:
        return True

    def categories(self, cancel_event: threading.Event | None = None) -> list[Category]:
        """读取 Comix 自己的分类筛选（类型 / 题材 / 状态 / 主题）。"""
        try:
            rows = self.browser.categories(cancel_event)
        except BrowserError as exc:
            raise ApiError(str(exc)) from exc
        result: list[Category] = [Category("", "全部", "type")]
        seen: set[str] = set()
        for row in rows:
            token = str(row.get("token") or "").strip()
            label = str(row.get("label") or "").strip()
            kind = str(row.get("kind") or "genre")
            if not token or not label or token in seen:
                continue
            seen.add(token)
            result.append(Category(token, label, kind))
        return result

    def browse_page(
        self,
        page: int = 1,
        category: str = "",
        cancel_event: threading.Event | None = None,
    ) -> PageResult:
        page = max(1, int(page or 1))
        try:
            rows = self.browser.browse_page(page, category, cancel_event)
        except BrowserError as exc:
            raise ApiError(str(exc)) from exc
        items = [self.decorate_series(item) for item in self._rows_to_series(rows)]
        meta = self.browser.browse_meta(page, category, cancel_event)
        max_page = int(meta.get("max_page") or 0)
        return PageResult(
            items=items,
            page=page,
            has_more=bool(items) and bool(meta.get("has_next")),
            total=max_page or None,
            category=category,
        )

    def get_chapters(
        self,
        series: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        try:
            rows = self.browser.get_chapters(series, cancel_event)
        except BrowserError as exc:
            raise ApiError(str(exc)) from exc
        finally:
            self.browser.clear_cache()
        chapters: list[dict[str, Any]] = []
        seen_numbers: set[float] = set()
        for row in rows:
            href = str(row.get("href") or "")
            number = self._chapter_number(row.get("label"), href)
            if number is None or number in seen_numbers:
                continue
            seen_numbers.add(number)
            chapter_id = href.rstrip("/").split("/")[-1].split("-", 1)[0]
            locked = bool(row.get("locked"))
            chapter = {
                "id": chapter_id,
                "number": number,
                "title": "",
                "slug": chapter_id,
                "url": COMIX_SITE_BASE + href,
                "href": href,
                "isFree": not locked,
                "price": row.get("price") or "",
            }
            chapters.append(self.decorate_chapter(series, chapter))
        return chapters

    def get_chapter_pages(
        self,
        series: dict[str, Any],
        chapter: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        if self.is_locked(chapter):
            return []
        chapter_url = self.chapter_referer(series, chapter)
        try:
            return self.browser.get_pages(chapter_url, cancel_event)
        except BrowserError as exc:
            raise ApiError(str(exc)) from exc

    def chapter_referer(
        self,
        series: dict[str, Any],
        chapter: dict[str, Any],
    ) -> str:
        return str(chapter.get("url") or "") or (
            f"{COMIX_SITE_BASE}{chapter.get('href') or ''}"
        )

    def cleanup_chapter_cache(self) -> int:
        return self.browser.clear_cache()

    def cleanup_runtime_cache(self) -> int:
        return self.browser.clear_cache()

    def close(self) -> None:
        self.browser.close()


MANGA18FX_CHAPTER_RE = re.compile(
    r"chapter[\s-]*(\d+(?:[.-]\d+)?)",
    re.IGNORECASE,
)


class Manga18fxProvider(SeriesProvider):
    key = "manga18fx"
    display_name = "Manga18fx"
    output_prefix = ("Manga18fx",)

    def __init__(self) -> None:
        super().__init__(
            ApiClient(
                MANGA18FX_SITE_BASE,
                headers=MANGA18FX_HTML_HEADERS,
                min_request_interval=0.12,
            )
        )

    @staticmethod
    def _chapter_number(*values: Any) -> float | None:
        for value in values:
            match = MANGA18FX_CHAPTER_RE.search(str(value or ""))
            if not match:
                continue
            try:
                return float(match.group(1).replace("-", "."))
            except ValueError:
                continue
        return None

    @staticmethod
    def _series_slug(url: str) -> str:
        return urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1]
        
    @classmethod
    def _parse_search_document(
        cls,
        source: str,
        base_url: str = MANGA18FX_SITE_BASE,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        root = HtmlTreeParser.parse(source)
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for card in root.find_all("div", "page-item"):
            anchor = next(
                (
                    item
                    for item in card.find_all("a")
                    if "/manga/" in item.attr("href")
                    and "/chapter-" not in item.attr("href")
                ),
                None,
            )
            if anchor is None:
                continue
            url = urllib.parse.urljoin(base_url, anchor.attr("href"))
            if url in seen:
                continue
            seen.add(url)
            image = next(iter(card.find_all("img")), None)
            latest = next(
                (
                    item
                    for item in card.find_all("a")
                    if "/chapter-" in item.attr("href")
                ),
                None,
            )
            slug = cls._series_slug(url)
            results.append(
                {
                    "id": slug,
                    "slug": slug,
                    "title": (
                        anchor.attr("title")
                        or anchor.text_content()
                        or slug
                    ),
                    "type": "manga",
                    "status": "-",
                    "views": 0,
                    "url": url,
                    "_url": url,
                    "poster": (
                        urllib.parse.urljoin(
                            base_url,
                            image.attr("data-src") or image.attr("src") if image else "",
                        )
                    ),
                    "latestChapter": latest.text_content() if latest else "",
                }
            )
            if len(results) >= limit:
                break
        return results

    @staticmethod
    def _detail_title(root: HtmlNode) -> str:
        title = ""
        h1 = next(iter(root.find_all("h1")), None)
        if h1 is not None:
            title = h1.text_content()
        if not title:
            for meta in root.find_all("meta"):
                if meta.attr("property") == "og:title":
                    title = meta.attr("content")
                    break
        title = re.sub(r"^Read\s+", "", title, flags=re.I)
        title = re.sub(r"\s+-\s+Manga18fx$", "", title, flags=re.I)
        title = re.sub(r"\s+Manhwa$", "", title, flags=re.I)
        return title.strip()

    @classmethod
    def _parse_detail_document(
        cls,
        source: str,
        detail_url: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        root = HtmlTreeParser.parse(source)
        slug = cls._series_slug(detail_url)
        poster = ""
        for meta in root.find_all("meta"):
            if meta.attr("property") == "og:image":
                poster = meta.attr("content")
                break
        series = {
            "id": slug,
            "slug": slug,
            "title": cls._detail_title(root) or slug,
            "type": "manga",
            "status": "-",
            "views": 0,
            "url": detail_url,
            "_url": detail_url,
            "poster": poster,
        }

        chapter_prefix = f"/manga/{slug}/chapter-"
        chapters: list[dict[str, Any]] = []
        seen_numbers: set[float] = set()
        anchors = root.find_all("a", "chapter-name")
        if not anchors:
            anchors = [
                anchor
                for anchor in root.find_all("a")
                if "/chapter-" in anchor.attr("href")
            ]
        for anchor in anchors:
            href = anchor.attr("href")
            if chapter_prefix not in href:
                continue
            label = anchor.text_content() or anchor.attr("title")
            number = cls._chapter_number(label, href)
            if number is None or number in seen_numbers:
                continue
            seen_numbers.add(number)
            chapter_url = urllib.parse.urljoin(detail_url, href)
            chapter_id = chapter_url.rstrip("/").split("/")[-1]
            chapters.append(
                {
                    "id": chapter_id,
                    "number": number,
                    "title": "",
                    "slug": chapter_id,
                    "url": chapter_url,
                    "isFree": True,
                    "price": 0,
                    "publishStatus": "PUBLIC",
                }
            )
        chapters.sort(key=chapter_sort_key)
        return series, chapters

    @staticmethod
    def _parse_page_document(source: str, chapter_url: str) -> list[dict[str, Any]]:
        root = HtmlTreeParser.parse(source)
        images: list[dict[str, Any]] = []
        seen: set[str] = set()
        for container in root.find_all("div", "page-break"):
            image = next(iter(container.find_all("img")), None)
            if image is None:
                continue
            image_url = image.attr("data-src") or image.attr("src")
            if not image_url:
                continue
            image_url = urllib.parse.urljoin(chapter_url, image_url)
            if image_url in seen:
                continue
            seen.add(image_url)
            images.append(
                {
                    "page_number": len(images) + 1,
                    "image_url": image_url,
                }
            )
        return images

    def _fetch_document(
        self,
        path_or_url: str,
        cancel_event: threading.Event | None = None,
    ) -> tuple[str, str]:
        return self.api.request_text(
            path_or_url,
            headers=MANGA18FX_HTML_HEADERS,
            cancel_event=cancel_event,
        )

    @staticmethod
    def _is_detail_url(value: str) -> bool:
        parsed = urllib.parse.urlparse(value)
        return parsed.scheme in {"http", "https"} and "/manga/" in parsed.path

    def search_series(
        self,
        query: str,
        series_type: str = "manga",
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        if series_type == "novel":
            return []
        if self._is_detail_url(query):
            source, final_url = self._fetch_document(query, cancel_event)
            series, _chapters = self._parse_detail_document(source, final_url)
            return [self.decorate_series(series)]
        query_url = (
            f"/search?{urllib.parse.urlencode({'q': query.strip()})}"
        )
        source, final_url = self._fetch_document(query_url, cancel_event)
        return [
            self.decorate_series(item)
            for item in self._parse_search_document(source, final_url)
        ]

    def browse_series(
        self,
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        source, final_url = self._fetch_document("/", cancel_event)
        return [
            self.decorate_series(item)
            for item in self._parse_search_document(source, final_url)
        ]

    # ---------- 分类与分页 ----------
    @staticmethod
    def _parse_pagination(source: str, page: int) -> bool:
        """从列表页 HTML 里判断是否还有下一页。"""
        root = HtmlTreeParser.parse(source)
        for anchor in root.find_all("a"):
            rel = (anchor.attr("rel") or "").lower()
            text = (anchor.text_content() or "").strip()
            if rel == "next" or text in {"Next", "next", "下一页", "下页", "›", "»"}:
                return True
        # 数字分页：出现比当前页更大的页码即视为还有下一页
        for anchor in root.find_all("a"):
            text = (anchor.text_content() or "").strip()
            if text.isdigit() and int(text) > page:
                return True
        return False

    def categories(self, cancel_event: threading.Event | None = None) -> list[Category]:
        source, final_url = self._fetch_document("/", cancel_event)
        root = HtmlTreeParser.parse(source)
        rows: list[Category] = []
        seen: set[str] = set()
        for anchor in root.find_all("a"):
            href = anchor.attr("href") or ""
            if "/genre/" not in href and "/manga-genre/" not in href:
                continue
            path = urllib.parse.urlparse(href).path.rstrip("/")
            token = path.split("/")[-1]
            label = (anchor.text_content() or "").strip()
            if not token or not label or token in seen or len(label) > 12:
                continue
            seen.add(token)
            rows.append(Category(token, label, "genre"))
        return rows

    def browse_page(
        self,
        page: int = 1,
        category: str = "",
        cancel_event: threading.Event | None = None,
    ) -> PageResult:
        page = max(1, int(page or 1))
        category = str(category or "").strip()
        if category.startswith(("http://", "https://")):
            path = category
        elif category:
            path = f"/genre/{category}"
        else:
            path = "/"
        query = f"?page={page}" if page > 1 else ""
        source, final_url = self._fetch_document(f"{path}{query}", cancel_event)
        items = [
            self.decorate_series(item)
            for item in self._parse_search_document(source, final_url)
        ]
        return PageResult(
            items=items,
            page=page,
            has_more=bool(items) and self._parse_pagination(source, page),
            category=category,
        )

    def get_chapters(
        self,
        series: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        detail_url = str(series.get("_url") or series.get("url") or "")
        if not detail_url:
            raise ApiError("作品缺少 Manga18fx 详情页地址")
        source, final_url = self._fetch_document(detail_url, cancel_event)
        _series, chapters = self._parse_detail_document(source, final_url)
        return [self.decorate_chapter(series, item) for item in chapters]

    def get_chapter_pages(
        self,
        series: dict[str, Any],
        chapter: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        chapter_url = self.chapter_referer(series, chapter)
        if not chapter_url:
            return []
        source, final_url = self._fetch_document(chapter_url, cancel_event)
        return self._parse_page_document(source, final_url)

    def chapter_referer(
        self,
        series: dict[str, Any],
        chapter: dict[str, Any],
    ) -> str:
        return str(chapter.get("url") or "")

    def close(self) -> None:
        return None


GENERIC_CHAPTER_RE = re.compile(
    r"(?:chapter|chap|ch|第|vol(?:ume)?)[\s._-]*(\d+(?:[._-]\d+)?)",
    re.IGNORECASE,
)


class BrowserSiteProvider(SeriesProvider):
    """Provider for sites whose public data is rendered in a logged-in page."""

    def __init__(self, key: str, display_name: str, base_url: str) -> None:
        self.key = key
        self.display_name = display_name
        self.output_prefix = (display_name,)
        self.base_url = base_url.rstrip("/")
        favorite_path = "/users-favclass.html" if key == "wnacg" else ""
        search_path = "/albums.html" if key == "wnacg" else ""
        self.browser = SiteBrowserClient(
            key,
            self.base_url,
            favorite_path,
            search_path,
            # 列表页排序：不显式给排序时部分站点会退化成默认杂序
            sort="create_time_DESC" if key == "wnacg" else "",
        )
        super().__init__(
            ApiClient(
                self.base_url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Referer": f"{self.base_url}/",
                },
                min_request_interval=0.2,
            )
        )

    @staticmethod
    def _chapter_number(*values: Any) -> float | None:
        for value in values:
            match = GENERIC_CHAPTER_RE.search(str(value or ""))
            if not match:
                match = re.search(r"(?<!\d)(\d+(?:[._-]\d+)?)(?!\d)", str(value or ""))
            if not match:
                continue
            try:
                return float(match.group(1).replace("_", ".").replace("-", "."))
            except ValueError:
                continue
        return None

    @staticmethod
    def _slug(url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        value = parsed.path.rstrip("/").split("/")[-1]
        return value or parsed.netloc

    def search_series(
        self,
        query: str,
        series_type: str = "manga",
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        if series_type == "novel":
            return []
        query = query.strip()
        if query.startswith(("http://", "https://")):
            rows = [{"href": query, "title": self._slug(query), "poster": ""}]
        else:
            if cancel_event and cancel_event.is_set():
                return []
            try:
                rows = self.browser.search(query)
            except SiteBrowserError as exc:
                raise ApiError(str(exc)) from exc
        return self._rows_to_series(rows)

    def browse_series(
        self,
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        if cancel_event and cancel_event.is_set():
            return []
        try:
            return self._rows_to_series(self.browser.browse())
        except SiteBrowserError as exc:
            raise ApiError(str(exc)) from exc

    # ---------- 分类与分页 ----------
    def uses_browser(self) -> bool:
        return True

    def categories(self, cancel_event: threading.Event | None = None) -> list[Category]:
        """读取站点自己的分类导航（绅士漫画是 albums-index-cate-N.html）。"""
        if cancel_event and cancel_event.is_set():
            return []
        try:
            rows = self.browser.categories()
        except SiteBrowserError as exc:
            raise ApiError(str(exc)) from exc
        result: list[Category] = [Category("", "全部", "genre")]
        seen: set[str] = set()
        for row in rows:
            href = str(row.get("href") or "").strip()
            label = str(row.get("label") or "").strip()
            if not href or not label:
                continue
            # 只保留站内链接，token 直接交给浏览器打开
            parsed = urllib.parse.urlparse(href)
            if parsed.netloc and parsed.netloc != urllib.parse.urlparse(self.base_url).netloc:
                continue
            token = f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path
            if not token or token in seen:
                continue
            seen.add(token)
            result.append(Category(token, label, str(row.get("kind") or "genre")))
        return result

    def browse_page(
        self,
        page: int = 1,
        category: str = "",
        cancel_event: threading.Event | None = None,
    ) -> PageResult:
        page = max(1, int(page or 1))
        if cancel_event and cancel_event.is_set():
            return PageResult(page=page, category=category)
        try:
            rows = self.browser.list_rows(page, str(category or ""))
        except SiteBrowserError as exc:
            raise ApiError(str(exc)) from exc
        items = self._rows_to_series(rows)
        has_more = False
        if items:
            # 站点自带分页控件优先；没有控件时按"本页是否有内容"判断（保守不翻页）
            has_more = self.browser.has_next_page(page, str(category or ""))
        return PageResult(
            items=items,
            page=page,
            has_more=has_more,
            category=category,
        )

    def _rows_to_series(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            url = urllib.parse.urljoin(f"{self.base_url}/", str(row.get("href") or ""))
            if not url.startswith(self.base_url) or url in seen:
                continue
            seen.add(url)
            slug = self._slug(url)
            results.append(
                self.decorate_series(
                    {
                        "id": slug,
                        "slug": slug,
                        "title": str(row.get("title") or slug),
                        "type": "manga",
                        "status": "-",
                        "views": 0,
                        "url": url,
                        "_url": url,
                        "poster": normalize_image_url(
                            row.get("poster") or "",
                            self.base_url,
                        ),
                    }
                )
            )
        return results

    def favorite_series(
        self, folder: str = "", max_pages: int | None = None
    ) -> list[dict[str, Any]]:
        try:
            return self._rows_to_series(self.browser.favorites())
        except SiteBrowserError as exc:
            raise ApiError(str(exc)) from exc

    def login_url(self) -> str:
        """站点自己的登录页（浏览器型漫源实际路径各异，先用首页最稳）。"""
        for path in ("/users-login.html", "/login", "/user/login"):
            candidate = urllib.parse.urljoin(f"{self.base_url}/", path.lstrip("/"))
            if candidate:
                return candidate
        return self.base_url

    def get_chapters(
        self,
        series: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        url = str(series.get("_url") or series.get("url") or "")
        if not url:
            return []
        if cancel_event and cancel_event.is_set():
            return []
        try:
            rows = self.browser.chapters(url)
        except SiteBrowserError as exc:
            raise ApiError(str(exc)) from exc
        chapters: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, row in enumerate(rows, start=1):
            chapter_url = urllib.parse.urljoin(f"{self.base_url}/", str(row.get("href") or ""))
            if not chapter_url.startswith(self.base_url) or chapter_url in seen:
                continue
            seen.add(chapter_url)
            number = self._chapter_number(row.get("label"), chapter_url)
            chapters.append(
                self.decorate_chapter(
                    series,
                    {
                        "id": self._slug(chapter_url),
                        "slug": self._slug(chapter_url),
                        "number": number if number is not None else float(index),
                        "title": str(row.get("label") or ""),
                        "url": chapter_url,
                        "isFree": True,
                        "price": 0,
                        "publishStatus": "PUBLIC",
                    },
                )
            )
        return sorted(chapters, key=chapter_sort_key)

    def get_chapter_pages(
        self,
        series: dict[str, Any],
        chapter: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        url = self.chapter_referer(series, chapter)
        if not url:
            return []
        if cancel_event and cancel_event.is_set():
            return []
        try:
            urls = self.browser.pages(url)
        except SiteBrowserError as exc:
            raise ApiError(str(exc)) from exc
        return [
            {"page_number": index, "image_url": image_url}
            for index, image_url in enumerate(urls, start=1)
        ]

    def chapter_referer(
        self,
        series: dict[str, Any],
        chapter: dict[str, Any],
    ) -> str:
        return str(chapter.get("url") or "")

    def download_file(
        self,
        url: str,
        destination: Path,
        referer: str = "",
        cancel_event: threading.Event | None = None,
    ) -> str:
        if cancel_event and cancel_event.is_set():
            return "cancelled"
        try:
            return self.browser.download_file(url, referer, destination)
        except SiteBrowserError as exc:
            raise ApiError(str(exc), url=url) from exc

    def fetch_image_bytes(self, url: str, referer: str = "") -> bytes:
        try:
            return self.browser.fetch_image(url, referer or self.base_url)
        except SiteBrowserError as exc:
            raise ApiError(str(exc), url=url) from exc

    def download_native(
        self,
        series: dict[str, Any],
        chapters: list[dict[str, Any]],
        output_root: Path,
        emit: Callable[..., None],
        cancel_event: threading.Event,
    ) -> bool | None:
        if self.key != "wnacg":
            return None
        if cancel_event.is_set():
            return False
        series_name = sanitize_path_component(
            series.get("title") or series.get("slug"),
            "Unknown Series",
            90,
        )
        series_root = self.output_root(output_root, series) / series_name
        chapter_root = series_root / "Chapter 1 - Native Download"
        archive_path = series_root / ".native-download.zip"
        source_url = str(series.get("_url") or series.get("url") or "")
        if not source_url:
            return None
        if self._chapter_files_complete_for_native(chapter_root):
            emit("log", message="WNACG 已存在完整的站内下载文件，已跳过。")
            return True
        # 上一轮可能被取消或崩溃，残留的半成品会让本轮混入旧数据，先清掉。
        self._clear_partial_extraction(chapter_root)
        try:
            emit("download_status", message="正在使用 WNACG 站内下载功能获取整本漫画...")
            status = self.browser.download_archive(source_url, archive_path)
            if status == "skipped" and not archive_path.exists():
                return None
            chapter_root.mkdir(parents=True, exist_ok=True)
            extracted = 0
            with zipfile.ZipFile(archive_path) as archive:
                names = sorted(
                    name for name in archive.namelist()
                    if not name.endswith("/")
                    and name.lower().endswith(tuple(IMAGE_EXTENSIONS))
                )
                for index, name in enumerate(names, start=1):
                    if cancel_event.is_set():
                        self._clear_partial_extraction(chapter_root)
                        return False
                    extension = Path(name).suffix.lower() or ".jpg"
                    target = chapter_root / f"{index:04d}{extension}"
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
                        # 解压中断时不要留下半成品；返回码由上层根据 cancel_event 判定。
                        try:
                            partial.unlink()
                        except OSError:
                            pass
                        raise
                    extracted += 1
            if extracted <= 0:
                return None
            # 只有全部解压成功才写入完成标记；缺标记一律视为未完成并重新解压。
            try:
                (chapter_root / f"{series_name}{NATIVE_COMPLETE_SUFFIX}").write_text(
                    f"pages={extracted}\n", encoding="utf-8"
                )
            except OSError:
                pass
            emit("log", message=f"WNACG 站内下载完成，共解压 {extracted} 页。", level="success")
            return True
        except (OSError, zipfile.BadZipFile, SiteBrowserError) as exc:
            self._clear_partial_extraction(chapter_root)
            raise ApiError(f"WNACG 站内下载失败：{exc}") from exc
        finally:
            try:
                archive_path.unlink()
            except OSError:
                pass

    @staticmethod
    def _clear_partial_extraction(folder: Path) -> None:
        """删除站点原生下载留下的半成品（.part）与残留图片。

        在“重新解压”之前调用：若上一轮解压到一半被取消，残留图片会使本轮
        重解压时混入旧的页序，因此必须连旧图片一并清除。
        """
        try:
            children = list(folder.iterdir())
        except OSError:
            return
        for item in children:
            if item.name.endswith(".part") or item.suffix.lower() in IMAGE_EXTENSIONS:
                try:
                    if item.is_file() or item.is_symlink():
                        item.unlink()
                except OSError:
                    pass

    @staticmethod
    def _chapter_files_complete_for_native(folder: Path) -> bool:
        """判断站点原生整本下载是否真的已完成。

        必须同时满足：存在完成标记（说明上一轮全部解压成功）且目录内有非空图片。
        仅凭“存在任意非空图片”会把解压到一半就被取消的目录误判为完成，
        并在下次运行时静默跳过，导致永久缺页。
        """
        try:
            children = list(folder.iterdir())
        except OSError:
            return False
        if not any(item.name.endswith(NATIVE_COMPLETE_SUFFIX) for item in children):
            return False
        files = [
            item for item in children
            if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS
        ]
        return bool(files) and all(item.stat().st_size > 0 for item in files)

    def open_site(self) -> None:
        try:
            self.browser.open()
        except SiteBrowserError as exc:
            raise RuntimeError(str(exc)) from exc

    def open_login(self) -> None:
        """用共享浏览器打开站点自己的登录页（绅士漫画是 users-login.html）。"""
        try:
            self.browser.open(self.login_url())
        except SiteBrowserError as exc:
            raise RuntimeError(str(exc)) from exc

    def open_favorites(self) -> None:
        try:
            self.browser.open_favorites()
        except SiteBrowserError as exc:
            raise RuntimeError(str(exc)) from exc

    def open_reader(self, series: dict[str, Any], chapter: dict[str, Any]) -> None:
        try:
            self.browser.open_reader(self.chapter_referer(series, chapter))
        except SiteBrowserError as exc:
            raise RuntimeError(str(exc)) from exc

    def site_base_url(self) -> str:
        return self.base_url

    def close(self) -> None:
        self.browser.close()


class JmProvider(SeriesProvider):
    """禁漫天堂 provider based on the same API contract as Breeze 3.0.33."""

    key = "18comic"
    display_name = "禁漫天堂"
    output_prefix = ("禁漫天堂",)

    #: 站点自带的分类（/categories 接口返回），首次读取后缓存
    _category_cache: list[Category] | None = None

    def __init__(self) -> None:
        self.api = JmApiClient()
        self.browser = SiteBrowserClient(
            "18comic",
            COMIC18_SITE_BASE,
        )
        #: 登录状态探测结果缓存（界面刷新很频繁，不能每次都打站点）
        self._login_checked_at = 0.0
        self._login_ok = False

    def open_login(self) -> None:
        try:
            self.browser.open(f"{COMIC18_SITE_BASE}/login")
        except SiteBrowserError as exc:
            raise RuntimeError(str(exc)) from exc

    def login_url(self) -> str:
        return f"{COMIC18_SITE_BASE}/login"

    # ---------- 官方账号登录（应用接口，账号密码直登） ----------
    def supports_password_login(self) -> bool:
        return True

    def login_hint(self) -> str:
        return (
            "禁漫天堂支持在程序里直接用账号密码登录（不需要浏览器），"
            "登录后可以读取收藏夹、把作品收藏进收藏夹。"
        )

    def login(self, account: str, password: str) -> bool:
        """POST /login（不能带 Authorization），成功后保存 jwttoken 与 AVS Cookie。"""
        account = str(account or "").strip()
        if not account or not password:
            raise ApiError("账号和密码都要填。")
        payload = self.api.request_json(
            "/login",
            method="POST",
            form={"username": account, "password": password},
            auth=False,
        )
        if not isinstance(payload, dict):
            raise ApiError("禁漫登录返回格式异常。")
        token = str(payload.get("jwttoken") or "").strip()
        if not token:
            raise ApiError("禁漫登录没有返回登录令牌，请确认账号密码。")
        self.session.set_headers({"Authorization": f"Bearer {token}"})
        secret = str(payload.get("s") or "").strip()
        if secret:
            self.session.update_cookies([("AVS", secret)])
        self.session.data["username"] = str(payload.get("username") or account)
        self.session.mark_login_confirmed(True)
        self.session.save()
        self._login_checked_at = 0.0
        return True

    def is_logged_in(self) -> bool:
        """问一次站点确认真实登录状态（结果缓存 60 秒，界面刷新不会反复打站点）。"""
        if not self.session.confirmed:
            return False
        now = time.monotonic()
        if self._login_checked_at and now - self._login_checked_at < 60:
            return self._login_ok
        try:
            self.api.request_json("/favorite", {"page": 1, "folder_id": "", "o": "mr"})
            self._login_ok = True
        except ApiError as exc:
            self._login_ok = False
            if exc.status not in (401, 403):
                # 网络问题不该把用户判成"未登录"
                self._login_ok = self._login_ok or False
        self._login_checked_at = now
        return self._login_ok

    # ---------- 官方收藏夹 ----------
    def favorite_folders(self) -> list[FavoriteFolder]:
        payload = self._favorite_page(1, "")
        rows: list[FavoriteFolder] = [FavoriteFolder("", "全部收藏")]
        for item in payload.get("folder_list") or []:
            if not isinstance(item, dict):
                continue
            folder_id = str(item.get("FID") or "").strip()
            name = str(item.get("name") or "").strip()
            if folder_id and name:
                rows.append(FavoriteFolder(folder_id, name))
        return rows

    def _favorite_page(self, page: int, folder: str) -> dict[str, Any]:
        payload = self.api.request_json(
            "/favorite",
            {"page": max(1, int(page or 1)), "folder_id": str(folder or ""), "o": "mr"},
        )
        return payload if isinstance(payload, dict) else {}

    #: 收藏夹单页条数（站点固定 20 条/页）
    _FAVORITE_PAGE_SIZE = 20
    #: 并发翻页数：站点没有限流，但 3 路已经能把「73 页」压到几分钟内
    _FAVORITE_WORKERS = 3

    def favorite_series(
        self, folder: str = "", max_pages: int | None = None
    ) -> list[dict[str, Any]]:
        """读收藏夹。

        禁漫账号里收藏上千部是常态（一页 20 条、单页要 6~8 秒），逐页串行会把
        接口拖到十分钟以上，因此这里**并发翻页**并按 `max_pages` 收紧上限。
        """
        if not self.is_logged_in():
            raise LoginRequired("禁漫天堂的收藏夹需要先登录账号", source=self.key)
        limit = 50 if not max_pages else max(1, min(int(max_pages), 50))
        pages: dict[int, list[dict[str, Any]]] = {}
        total = 0
        stopped = False
        try:
            from concurrent.futures import ThreadPoolExecutor
        except Exception:  # noqa: BLE001 - 极端环境下退回串行
            ThreadPoolExecutor = None  # type: ignore[assignment]
        if ThreadPoolExecutor is None:
            for page in range(1, limit + 1):
                payload = self._favorite_page(page, folder)
                rows = payload.get("list") or []
                if page == 1:
                    total = self._safe_int(payload.get("total"))
                if not rows:
                    break
                pages[page] = rows
                if len(rows) < self._FAVORITE_PAGE_SIZE or (total and len(pages) * self._FAVORITE_PAGE_SIZE >= total):
                    break
        else:
            with ThreadPoolExecutor(max_workers=self._FAVORITE_WORKERS) as pool:
                page = 1
                while not stopped and page <= limit:
                    batch = list(range(page, min(page + self._FAVORITE_WORKERS, limit + 1)))
                    futures = {p: pool.submit(self._favorite_page, p, folder) for p in batch}
                    for p in batch:
                        try:
                            payload = futures[p].result()
                        except Exception as exc:  # noqa: BLE001 - 单页失败不影响其它页
                            log.warning("禁漫收藏第 %s 页读取失败：%s", p, exc)
                            stopped = True
                            break
                        rows = payload.get("list") or []
                        if p == 1:
                            total = self._safe_int(payload.get("total"))
                        if not rows:
                            stopped = True
                            break
                        pages[p] = rows
                        if len(rows) < self._FAVORITE_PAGE_SIZE:
                            stopped = True
                            break
                    page = batch[-1] + 1

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in sorted(pages):
            for item in pages[page]:
                if not isinstance(item, dict):
                    continue
                comic_id = str(item.get("id") or "").strip()
                if not comic_id or comic_id in seen:
                    continue
                seen.add(comic_id)
                items.append(self.decorate_series(self._series_from_item(item)))
            if total and len(items) >= total:
                break
        #: 站点上的总数与本次实际取到的条数，供上层提示「还有多少没同步」
        self._favorite_total = total
        self._favorite_pages = len(pages)
        return items

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def favorite_total_hint(self) -> int:
        """上一次 `favorite_series()` 时站点报的收藏总数（未知为 0）。"""
        return int(getattr(self, "_favorite_total", 0) or 0)

    def _is_favorite(self, comic_id: str) -> bool:
        """查这部作品当前是否已在收藏夹（/favorite 是切换接口，必须先查）。"""
        try:
            payload = self.api.request_json("/album", {"id": comic_id})
        except ApiError:
            return False
        return bool(isinstance(payload, dict) and payload.get("is_favorite"))

    def _toggle_favorite(self, comic_id: str) -> str:
        """POST /favorite 是**切换**接口，返回本次实际动作：add / remove。"""
        payload = self.api.request_json("/favorite", method="POST", form={"aid": comic_id})
        if isinstance(payload, dict):
            return str(payload.get("type") or "")
        return ""

    def _move_to_folder(self, comic_id: str, folder_id: str) -> bool:
        if not folder_id:
            return True
        self.api.request_json(
            "/favorite_folder",
            method="POST",
            form={
                "type": "move",
                "folder_id": folder_id,
                "folder_name": "",
                "aid": comic_id,
            },
        )
        return True

    def add_favorite(
        self,
        series: dict[str, Any],
        folder: FavoriteFolder | None = None,
    ) -> bool:
        if not self.is_logged_in():
            raise LoginRequired("收藏到禁漫收藏夹需要先登录账号", source=self.key)
        comic_id = str(series.get("id") or series.get("slug") or "").strip()
        if not comic_id:
            return False
        folder_id = str(getattr(folder, "token", "") or "")
        if self._is_favorite(comic_id):
            # 已经在收藏夹里：只做"移动到目标收藏夹"，绝不重复点（会取消收藏）
            self._move_to_folder(comic_id, folder_id)
            return True
        action = self._toggle_favorite(comic_id)
        if action == "remove":
            # 说明本地判断与站点不一致，再点一次切回"已收藏"
            action = self._toggle_favorite(comic_id)
        self._login_checked_at = 0.0
        if action not in {"add", ""}:
            return False
        self._move_to_folder(comic_id, folder_id)
        return True

    def remove_favorite(self, series: dict[str, Any]) -> bool:
        if not self.is_logged_in():
            raise LoginRequired("取消禁漫收藏需要先登录账号", source=self.key)
        comic_id = str(series.get("id") or series.get("slug") or "").strip()
        if not comic_id:
            return False
        if not self._is_favorite(comic_id):
            return True
        action = self._toggle_favorite(comic_id)
        if action == "add":
            action = self._toggle_favorite(comic_id)
        self._login_checked_at = 0.0
        return action in {"remove", ""}

    def uses_browser(self) -> bool:
        """章节/封面优先走公开 API，但取图与下载会用到共享 Chromium 会话。"""
        return True

    @staticmethod
    def _cover_url(item: dict[str, Any]) -> str:
        image = str(item.get("image") or "").strip()
        comic_id = str(item.get("id") or "").strip()
        if image:
            normalized = normalize_image_url(image, JM_IMAGE_BASE)
            if normalized.startswith(("http://", "https://")):
                return normalized
        return f"{JM_IMAGE_BASE}/media/albums/{comic_id}_3x4.jpg" if comic_id else ""

    @staticmethod
    def _series_from_item(item: dict[str, Any]) -> dict[str, Any]:
        comic_id = str(item.get("id") or "").strip()
        return {
            "id": comic_id,
            "hid": comic_id,
            "slug": comic_id,
            "title": str(item.get("name") or comic_id),
            "type": "manga",
            "status": "-",
            "views": item.get("total_views") or item.get("totalViews") or 0,
            "url": f"{COMIC18_SITE_BASE}/album/{comic_id}",
            "_url": f"{COMIC18_SITE_BASE}/album/{comic_id}",
            "poster": JmProvider._cover_url(item),
            "_jm_raw": item,
        }

    def search_series(
        self,
        query: str,
        series_type: str = "manga",
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        if series_type == "novel":
            return []
        query = query.strip()
        if not query:
            return self.browse_series(cancel_event)
        if query.casefold().startswith("jm"):
            query = query[2:].strip()
        if query.isdigit() and int(query) >= 100:
            try:
                response = self.api.request_json("/album", {"id": query}, cancel_event)
                item = response if isinstance(response, dict) else {}
                return [self.decorate_series(self._series_from_item(item))] if item.get("id") else []
            except ApiError:
                pass
        response = self.api.request_json(
            "/search",
            {"search_query": query, "page": 1, "o": ""},
            cancel_event,
        )
        content = response.get("content", []) if isinstance(response, dict) else []
        return [
            self.decorate_series(self._series_from_item(item))
            for item in content
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        ]

    def browse_series(
        self,
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        # 实测 /search 传空 search_query 返回的 content 为空（不是报错），
        # 所以浏览不能靠它。改用 /latest；个别镜像没有该端点时再退回 /search。
        items: list[dict[str, Any]] = []
        for path, params in (
            ("/latest", {"page": 1}),
            ("/promote", {"page": 1}),
            ("/search", {"search_query": "", "page": 1, "o": ""}),
        ):
            if cancel_event is not None and cancel_event.is_set():
                break
            try:
                response = self.api.request_json(path, params, cancel_event)
            except ApiError:
                continue
            if isinstance(response, list):
                found = [item for item in response if isinstance(item, dict)]
            elif isinstance(response, dict):
                found = [
                    item
                    for item in (response.get("content") or [])
                    if isinstance(item, dict)
                ]
            else:
                found = []
            if found:
                items = found
                break
        return [
            self.decorate_series(self._series_from_item(item))
            for item in items
            if str(item.get("id") or "").strip()
        ]

    # ---------- 分类与分页 ----------
    def categories(self, cancel_event: threading.Event | None = None) -> list[Category]:
        if self._category_cache is not None:
            return list(self._category_cache)
        try:
            payload = self.api.request_json("/categories", None, cancel_event)
        except ApiError:
            return []
        rows: list[Category] = [Category("", "全部", "genre")]
        raw = payload.get("categories", []) if isinstance(payload, dict) else []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            slug = str(item.get("slug") or "").strip()
            cid = str(item.get("id") or "").strip()
            # "最新A漫" 是首页聚合，没有独立分类参数，跳过（默认浏览就是它）
            if not name or not cid or cid == "0":
                continue
            token = f"c={cid}"
            rows.append(Category(token, name, "genre"))
            for sub in item.get("sub_categories") or []:
                if not isinstance(sub, dict):
                    continue
                sub_name = str(sub.get("name") or "").strip()
                sub_cid = str(sub.get("CID") or "").strip()
                if not sub_name or not sub_cid:
                    continue
                rows.append(Category(f"c={cid}&t={sub_cid}", f"{name}·{sub_name}", "genre"))
        self._category_cache = rows
        return list(rows)

    def browse_page(
        self,
        page: int = 1,
        category: str = "",
        cancel_event: threading.Event | None = None,
    ) -> PageResult:
        page = max(1, int(page or 1))
        query: dict[str, Any] = {"page": page}
        for part in str(category or "").replace("&", ",").split(","):
            if "=" in part:
                key, value = part.split("=", 1)
                if key.strip() in {"c", "t", "o", "a", "f", "s"}:
                    query[key.strip()] = value.strip()
        response = self.api.request_json("/categories/filter", query, cancel_event)
        content = response.get("content", []) if isinstance(response, dict) else []
        items = [
            self.decorate_series(self._series_from_item(item))
            for item in (content if isinstance(content, list) else [])
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        ]
        total = None
        if isinstance(response, dict):
            value = response.get("total")
            if isinstance(value, (int, float)) and value > 0:
                total = int(value)
        per_page = max(1, len(items))
        has_more = bool(items) and (
            (total is not None and page * per_page < total) or len(items) >= 20
        )
        return PageResult(
            items=items,
            page=page,
            has_more=has_more,
            total=total,
            category=category,
        )

    def search_page(
        self,
        query: str,
        series_type: str = "manga",
        page: int = 1,
        cancel_event: threading.Event | None = None,
    ) -> PageResult:
        page = max(1, int(page or 1))
        if series_type == "novel":
            return PageResult(page=page)
        keyword = query.strip()
        if keyword.casefold().startswith("jm"):
            keyword = keyword[2:].strip()
        if not keyword:
            return self.browse_page(page, "", cancel_event)
        response = self.api.request_json(
            "/search",
            {"search_query": keyword, "page": page, "o": ""},
            cancel_event,
        )
        content = response.get("content", []) if isinstance(response, dict) else []
        items = [
            self.decorate_series(self._series_from_item(item))
            for item in (content if isinstance(content, list) else [])
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        ]
        total = 0
        if isinstance(response, dict) and isinstance(response.get("total"), (int, float)):
            total = int(response["total"])
        per_page = max(1, len(items))
        return PageResult(
            items=items,
            page=page,
            has_more=bool(items) and (page * per_page < total if total else len(items) >= 20),
            total=total or None,
        )

    def detail(self, series: dict[str, Any]) -> dict[str, Any]:
        """补详情字段：tags / works / author 都在 /album 里。"""
        comic_id = str(series.get("id") or series.get("slug") or "").strip()
        if not comic_id:
            return {}
        payload = self.api.request_json("/album", {"id": comic_id})
        if not isinstance(payload, dict) or not payload:
            return {}
        extra: dict[str, Any] = {}
        tags = payload.get("tags")
        if isinstance(tags, list) and tags:
            extra["genres"] = [str(tag) for tag in tags if str(tag).strip()]
        author = payload.get("author")
        if isinstance(author, list):
            names = [str(row).strip() for row in author if str(row).strip() and str(row).strip().upper() != "N/A"]
            if names:
                extra["author"] = "、".join(names)
        elif isinstance(author, str) and author.strip() and author.strip().upper() != "N/A":
            extra["author"] = author.strip()
        description = payload.get("description")
        if isinstance(description, str) and description.strip():
            extra["description"] = description.strip()
        return extra

    def get_chapters(
        self,
        series: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        comic_id = str(series.get("id") or series.get("slug") or "").strip()
        if not comic_id:
            return []
        response = self.api.request_json("/album", {"id": comic_id}, cancel_event)
        raw_series = response.get("series", []) if isinstance(response, dict) else []
        chapters: list[dict[str, Any]] = []
        ordered = [item for item in raw_series if isinstance(item, dict) and str(item.get("sort") or "") != "0"]
        ordered.sort(key=lambda item: float(item.get("sort") or 0))
        for index, item in enumerate(ordered, start=1):
            chapter_id = str(item.get("id") or "").strip()
            if not chapter_id:
                continue
            chapters.append(
                self.decorate_chapter(
                    series,
                    {
                        "id": chapter_id,
                        "slug": chapter_id,
                        "number": index,
                        "title": f"第{index}话 {str(item.get('name') or '').strip()}".strip(),
                        "url": f"{JM_API_BASE}/chapter?id={urllib.parse.quote(chapter_id)}",
                        "isFree": True,
                        "price": 0,
                        "publishStatus": "PUBLIC",
                    },
                )
            )
        if chapters:
            return chapters
        return [
            self.decorate_chapter(
                series,
                {
                    "id": comic_id,
                    "slug": comic_id,
                    "number": 1,
                    "title": "第1话",
                    "url": f"{JM_API_BASE}/chapter?id={urllib.parse.quote(comic_id)}",
                    "isFree": True,
                    "price": 0,
                    "publishStatus": "PUBLIC",
                },
            )
        ]

    def get_chapter_pages(
        self,
        series: dict[str, Any],
        chapter: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        chapter_id = str(chapter.get("id") or chapter.get("slug") or "").strip()
        if not chapter_id:
            return []
        response = self.api.request_json("/chapter", {"skip": "", "id": chapter_id}, cancel_event)
        images = response.get("images", []) if isinstance(response, dict) else []
        if not isinstance(images, list) and isinstance(response, dict):
            nested = response.get("data")
            if isinstance(nested, dict):
                images = nested.get("images", [])
        return [
            {
                "page_number": index,
                "image_url": f"{JM_IMAGE_BASE}/media/photos/{urllib.parse.quote(chapter_id)}/{urllib.parse.quote(str(name))}",
            }
            for index, name in enumerate(images, start=1)
            if str(name or "").strip()
        ]

    def chapter_referer(
        self,
        series: dict[str, Any],
        chapter: dict[str, Any],
    ) -> str:
        return f"{COMIC18_SITE_BASE}/"

    def download_file(
        self,
        url: str,
        destination: Path,
        referer: str = "",
        cancel_event: threading.Event | None = None,
    ) -> str:
        if cancel_event and cancel_event.is_set():
            return "cancelled"
        # Use the persistent Chromium context first so CDN cookies, referer,
        # and anti-hotlink headers match the user's logged-in site session.
        try:
            status = self.browser.download_file(
                url, referer or f"{COMIC18_SITE_BASE}/", destination
            )
        except Exception:
            status = self.api.download_file(url, destination, referer, cancel_event)
        if status == "downloaded" and "/media/photos/" in url:
            self._fix_scramble_file(destination, url)
        return status

    @staticmethod
    def _fix_scramble_file(destination: Path, url: str = "") -> None:
        """禁漫的图是横向切条倒序的，落盘后还原一下（检测不到就原样保留）。"""
        try:
            raw = destination.read_bytes()
        except OSError:
            log.warning(
                "禁漫切条还原：读取已下载文件失败，跳过 file=%s url=%s",
                destination,
                url,
                exc_info=True,
            )
            return
        if not raw:
            return
        try:
            fixed = _jm_unscramble(raw)
        except Exception:  # noqa: BLE001 - 还原失败不能影响下载结果，但要留日志
            log.warning(
                "禁漫切条还原：unscramble 抛异常，保留原图 file=%s url=%s",
                destination,
                url,
                exc_info=True,
            )
            return
        if fixed is raw or fixed == raw:
            return
        try:
            destination.write_bytes(fixed)
        except OSError:
            log.warning(
                "禁漫切条还原：写回还原结果失败，保留原文件 file=%s url=%s",
                destination,
                url,
                exc_info=True,
            )

    def fetch_image_bytes(self, url: str, referer: str = "") -> bytes:
        data = self._fetch_image_raw(url, referer)
        if data and "/media/photos/" in url:
            data = _jm_unscramble(data)
        return data

    def _fetch_image_raw(self, url: str, referer: str = "") -> bytes:
        try:
            return self.browser.fetch_image(url, referer or f"{COMIC18_SITE_BASE}/")
        except Exception:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    "Referer": referer or f"{COMIC18_SITE_BASE}/",
                    "Host": urllib.parse.urlparse(url).netloc,
                },
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()

    def open_site(self) -> None:
        try:
            self.browser.open()
        except SiteBrowserError as exc:
            raise RuntimeError(str(exc)) from exc

    def open_favorites(self) -> None:
        try:
            self.browser.open()
        except SiteBrowserError as exc:
            raise RuntimeError(str(exc)) from exc

    def open_reader(self, series: dict[str, Any], chapter: dict[str, Any]) -> None:
        try:
            self.browser.open()
        except SiteBrowserError as exc:
            raise RuntimeError(str(exc)) from exc

    def site_base_url(self) -> str:
        return COMIC18_SITE_BASE

    def close(self) -> None:
        self.browser.close()


class CopyComicProvider(SeriesProvider):
    """拷贝漫画 Breeze v3 API adapter."""

    key = "copycomic"
    display_name = "拷贝漫画"
    output_prefix = ("拷贝漫画",)

    def __init__(self) -> None:
        super().__init__(
            ApiClient(
                COPYMANGA_API_BASE,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "version": "2025.05.09",
                    # 实测（2026-10）：站点要求带 platform / region 头，否则接口
                    # 一律返回 code=200 但 results.total=0（列表与搜索都会"空"，
                    # 表现为漫源加载不出任何作品）。platform 只接受 1，给 3 会 code=210。
                    "platform": "1",
                    "region": "1",
                    "Referer": f"{COPYMANGA_SITE_BASE}/",
                },
                min_request_interval=0.15,
            )
        )

    @staticmethod
    def _data(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        value = payload.get("results")
        return value if isinstance(value, dict) else payload

    @staticmethod
    def _series(item: dict[str, Any]) -> dict[str, Any] | None:
        comic_id = str(item.get("path_word") or item.get("id") or "").strip()
        if not comic_id:
            return None
        status = item.get("status")
        if isinstance(status, dict):
            status = status.get("display") or status.get("value")
        return {
            "id": comic_id,
            "slug": comic_id,
            "title": str(item.get("name") or item.get("title") or comic_id).strip(),
            "type": "manga",
            "status": str(status or "-"),
            "views": item.get("popular") or item.get("views") or 0,
            "poster": normalize_image_url(str(item.get("cover") or ""), COPYMANGA_SITE_BASE),
            "url": f"{COPYMANGA_SITE_BASE}/comic/{comic_id}/",
            "_url": f"{COPYMANGA_SITE_BASE}/comic/{comic_id}/",
        }

    def _rows(self, payload: Any) -> list[dict[str, Any]]:
        rows = self._data(payload).get("list", [])
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def search_series(self, query: str, series_type: str = "manga", cancel_event: threading.Event | None = None) -> list[dict[str, Any]]:
        if series_type == "novel" or not query.strip():
            return []
        payload = self.api.request_json(
            "/search/comic",
            {"limit": 30, "offset": 0, "q": query.strip(), "q_type": "", "platform": 1},
            cancel_event,
        )
        return [self.decorate_series(item) for row in self._rows(payload) if (item := self._series(row))]

    def browse_series(self, cancel_event: threading.Event | None = None) -> list[dict[str, Any]]:
        payload = self.api.request_json(
            "/comics",
            {"limit": 30, "offset": 0, "ordering": "-datetime_updated", "free_type": 1, "platform": 1},
            cancel_event,
        )
        return [self.decorate_series(item) for row in self._rows(payload) if (item := self._series(row))]

    # ---------- 分类与分页 ----------
    #: 站点自带的排序 / 免费筛选，与网页版筛选栏一致
    _CATEGORIES = (
        Category("ordering=-datetime_updated", "最近更新", "sort"),
        Category("ordering=-popular", "人气最高", "sort"),
        Category("ordering=-datetime_created", "最新上架", "sort"),
        Category("free_type=1", "免费", "type"),
        Category("free_type=0", "付费", "type"),
    )

    def categories(self, cancel_event: threading.Event | None = None) -> list[Category]:
        return [Category("", "全部", "sort"), *self._CATEGORIES]

    def _offset(self, page: int, per_page: int) -> int:
        return max(0, (max(1, int(page or 1)) - 1) * per_page)

    def browse_page(
        self,
        page: int = 1,
        category: str = "",
        cancel_event: threading.Event | None = None,
    ) -> PageResult:
        page = max(1, int(page or 1))
        per_page = 30
        query: dict[str, Any] = {
            "limit": per_page,
            "offset": self._offset(page, per_page),
            "ordering": "-datetime_updated",
            "free_type": 1,
            "platform": 1,
        }
        for part in str(category or "").replace("&", ",").split(","):
            if "=" in part:
                key, value = part.split("=", 1)
                if key.strip() in {"ordering", "free_type"}:
                    query[key.strip()] = value.strip()
        payload = self.api.request_json("/comics", query, cancel_event)
        items = [self.decorate_series(item) for row in self._rows(payload) if (item := self._series(row))]
        total = None
        if isinstance(payload, dict):
            data = self._data(payload)
            for key in ("total", "count"):
                value = data.get(key)
                if isinstance(value, (int, float)) and value > 0:
                    total = int(value)
                    break
        return PageResult(
            items=items,
            page=page,
            has_more=len(items) >= per_page,
            total=total,
            category=category,
        )

    def search_page(
        self,
        query: str,
        series_type: str = "manga",
        page: int = 1,
        cancel_event: threading.Event | None = None,
    ) -> PageResult:
        page = max(1, int(page or 1))
        if series_type == "novel" or not query.strip():
            return PageResult(page=page)
        per_page = 30
        payload = self.api.request_json(
            "/search/comic",
            {
                "limit": per_page,
                "offset": self._offset(page, per_page),
                "q": query.strip(),
                "q_type": "",
                "platform": 1,
            },
            cancel_event,
        )
        items = [self.decorate_series(item) for row in self._rows(payload) if (item := self._series(row))]
        return PageResult(items=items, page=page, has_more=len(items) >= per_page)

    def _detail(self, comic_id: str, cancel_event: threading.Event | None) -> dict[str, Any]:
        payload = self.api.request_json(
            f"/comic2/{urllib.parse.quote(comic_id, safe='')}",
            {"platform": 1},
            cancel_event,
        )
        data = self._data(payload)
        value = data.get("comic")
        return value if isinstance(value, dict) else data

    def detail(self, series: dict[str, Any]) -> dict[str, Any]:
        """补详情字段：``theme``（题材）就是站点上的标签。"""
        comic_id = str(series.get("id") or series.get("slug") or "").strip()
        if not comic_id:
            return {}
        item = self._detail(comic_id, None)
        if not isinstance(item, dict) or not item:
            return {}
        extra: dict[str, Any] = {}
        theme = item.get("theme")
        if isinstance(theme, list) and theme:
            extra["genres"] = theme
        author = item.get("author")
        if isinstance(author, list):
            names = [
                str(row.get("name") or "").strip()
                for row in author
                if isinstance(row, dict)
            ]
            names = [name for name in names if name]
            if names:
                extra["author"] = "、".join(names)
        elif isinstance(author, str) and author.strip():
            extra["author"] = author.strip()
        brief = item.get("brief")
        if isinstance(brief, str) and brief.strip():
            extra["description"] = brief.strip()
        status = item.get("status")
        if isinstance(status, dict):
            status = status.get("display") or status.get("value")
        if isinstance(status, str) and status.strip():
            extra["status"] = status.strip()
        return extra

    def get_chapters(self, series: dict[str, Any], cancel_event: threading.Event | None = None) -> list[dict[str, Any]]:
        comic_id = str(series.get("id") or series.get("slug") or "").strip()
        if not comic_id:
            return []
        detail = self._detail(comic_id, cancel_event)
        groups = detail.get("groups") if isinstance(detail.get("groups"), list) else []
        chapters: list[dict[str, Any]] = []
        order = 0
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_id = str(group.get("path_word") or group.get("name") or "").strip()
            if not group_id:
                continue
            payload = self.api.request_json(
                f"/comic/{urllib.parse.quote(comic_id, safe='')}/group/{urllib.parse.quote(group_id, safe='')}/chapters",
                {"limit": 200, "offset": 0, "platform": 1},
                cancel_event,
            )
            for row in self._rows(payload):
                chapter_id = str(row.get("uuid") or row.get("id") or "").strip()
                if not chapter_id:
                    continue
                order += 1
                chapters.append(self.decorate_chapter(series, {
                    "id": chapter_id,
                    "slug": chapter_id,
                    "number": row.get("order") or order,
                    "title": str(row.get("name") or f"第{order}话"),
                    "url": f"{COPYMANGA_SITE_BASE}/comic/{comic_id}/{chapter_id}",
                    "isFree": True,
                    "price": 0,
                    "publishStatus": "PUBLIC",
                }))
        return chapters

    def get_chapter_pages(self, series: dict[str, Any], chapter: dict[str, Any], cancel_event: threading.Event | None = None) -> list[dict[str, Any]]:
        comic_id = str(series.get("id") or series.get("slug") or "").strip()
        chapter_id = str(chapter.get("id") or chapter.get("slug") or "").strip()
        if not comic_id or not chapter_id:
            return []
        payload = self.api.request_json(
            f"/comic/{urllib.parse.quote(comic_id, safe='')}/chapter2/{urllib.parse.quote(chapter_id, safe='')}",
            {"platform": 1},
            cancel_event,
        )
        data = self._data(payload)
        chapter_data = data.get("chapter") if isinstance(data.get("chapter"), dict) else data
        contents = chapter_data.get("contents", []) if isinstance(chapter_data, dict) else []
        pages: list[dict[str, Any]] = []
        for index, value in enumerate(contents if isinstance(contents, list) else [], start=1):
            url = str(value.get("url") or value.get("image") or value.get("content") or "") if isinstance(value, dict) else str(value or "")
            url = normalize_image_url(url.strip(), COPYMANGA_SITE_BASE)
            if url:
                pages.append({"page_number": index, "image_url": url})
        return pages

    def chapter_referer(self, series: dict[str, Any], chapter: dict[str, Any]) -> str:
        return str(chapter.get("url") or series.get("_url") or COPYMANGA_SITE_BASE)

    def site_base_url(self) -> str:
        return COPYMANGA_SITE_BASE


def build_providers(include_breeze: bool = False) -> dict[str, SeriesProvider]:
    """构建漫源注册表：key → provider。

    图源现在由 ``dumbcat/sources/json/*.json`` 描述（一个图源一个文件），
    这里只是把清单里声明 ``engine: python`` 的那些实例化出来。加/删/停用
    图源 = 改 JSON，不用动这个文件。

    include_breeze 为 True 时额外接入「需要梯子 / 需要浏览器」的站点
    （拷贝漫画、WEBTOON、JJ漫画、漫画柜、包子漫画）。这些站点在部分网络
    环境下不可达，但加载 provider 本身不发请求，失败只在真正浏览时提示。
    """
    from dumbcat.sources.manifest import build_providers as _from_json

    return _from_json(include_breeze=include_breeze)



