"""WEBTOON 漫源（Naver 官方应用接口，纯 HTTP）。

参考 Breeze 的 `Breeze-plugin-webtoon`（它只实现了搜索 / 详情 / 章节 / 图片，
没有登录与收藏夹——所以这里也不假造账号功能，UI 会明确显示"不支持收藏夹"）。

接口要点（对着插件源码逐条实现，并实测过）：

1. **应用接口需要 HMAC-SHA1 签名**：`md = base64(HMAC-SHA1(secret, url[:255] + msgpad))`，
   然后把 `msgpad` 与 `md` 追加到查询串上；没有签名直接 403。
2. 需要固定设备头：`User-Agent: nApps (...)`、`wtu: <固定值>`、`Referer: http://m.webtoons.com/`。
3. 响应统一是 `{"message": {"code": 0, "result": {...}}}`。
4. **应用接口没有"浏览/榜单"列表**（搜空关键词返回 0 条），所以首页列表走网页版
   服务端渲染页面（`/{lang}/popular`、`/{lang}/originals`），其余全部走应用接口。
5. 图片地址是相对路径，要拼到 `https://webtoon-phinf.pstatic.net` 上。

语言（站点自带的维度，当成分类用）：zh-hant / en / th / id / es / fr / de。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import hmac
import ssl
import threading
import time
import urllib.parse
from typing import Any

from dumbcat.app_config import USER_AGENT
from dumbcat.errors import ApiError
from dumbcat.html_parser import HtmlNode, HtmlTreeParser
from dumbcat.html_query import query_all, query_one
from dumbcat.sources.api import ApiClient
from dumbcat.sources.base import Category, PageResult, SeriesProvider

API_BASE = "https://global.apis.naver.com/lineWebtoon/webtoon"
IMAGE_CDN = "https://webtoon-phinf.pstatic.net"
WEB_BASE = "https://www.webtoons.com"

#: 应用接口的固定凭据（来自站点自己的应用；插件源码里的原值）
HMAC_SECRET = "gUtPzJFZch4ZyAGviiyH94P99lQ3pFdRTwpJWDlSGFfwgpr6ses5ALOxWHOIT7R1"
WTU = "a1b2c3d4e5f6789012345678901234ab"
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
APP_UA = "nApps (Android 13; SM-G991B; en; 3.9.6)"

#: 站点支持的界面语言（也是内容语言）
LANGUAGES: tuple[tuple[str, str], ...] = (
    ("zh-hant", "繁體中文"),
    ("en", "English"),
    ("th", "ไทย"),
    ("id", "Bahasa Indonesia"),
    ("es", "Español"),
    ("fr", "Français"),
    ("de", "Deutsch"),
)
DEFAULT_LANG = "zh-hant"

#: 首页列表来源（网页版服务端渲染页）
LIST_KINDS: tuple[tuple[str, str], ...] = (
    ("popular", "熱門"),
    ("originals", "正式連載"),
)
DEFAULT_LIST = "popular"

PAGE_SIZE = 30


def _force_tls12(client: Any) -> None:
    """WEBTOON 的 Akamai 边缘对 TLS1.3 偶发直接断连（SSLEOFError），退回 1.2 更稳。"""
    try:
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        client.ssl_context = ctx
    except Exception:  # noqa: BLE001 - 老 OpenSSL 没有 TLSVersion 时保持默认
        pass


class WebtoonProvider(SeriesProvider):
    key = "webtoon"
    display_name = "WEBTOON"
    output_prefix = ("WEBTOON",)

    def __init__(self) -> None:
        client = ApiClient(
            API_BASE,
            headers={"Accept": "application/json", "Referer": "http://m.webtoons.com/"},
            min_request_interval=0.1,
            session_key=self.key,
        )
        _force_tls12(client)
        super().__init__(client)
        self._lang = str(self.session.data.get("lang") or DEFAULT_LANG)
        self._list_kind = str(self.session.data.get("list") or DEFAULT_LIST)

    def uses_browser(self) -> bool:
        return False

    def site_base_url(self) -> str:
        return WEB_BASE

    # ---------- 签名与请求 ----------
    @staticmethod
    def sign_url(url: str) -> str:
        """NHN 网关要求：md = base64(HMAC-SHA1(secret, url[:255] + msgpad))。"""
        msgpad = str(int(time.time() * 1000))
        payload = url[:255] + msgpad
        digest = hmac.new(
            HMAC_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1
        ).digest()
        md = base64.b64encode(digest).decode("ascii")
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}msgpad={msgpad}&md={urllib.parse.quote(md)}"

    def _common_qs(self) -> str:
        return urllib.parse.urlencode(
            {
                "platform": "APP_ANDROID",
                "language": self._lang,
                "locale": self._lang,
                "v": "1",
                "serviceZone": "GLOBAL",
            }
        )

    def _api(self, path: str, cancel_event: threading.Event | None = None) -> dict[str, Any]:
        url = self.sign_url(f"{API_BASE}/{path}")
        raw, _final, _charset = self.api.request_bytes(
            url,
            headers={"User-Agent": APP_UA, "wtu": WTU},
            cancel_event=cancel_event,
        )
        import json

        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise ApiError("WEBTOON 返回了无法解析的数据") from exc
        message = payload.get("message") if isinstance(payload, dict) else None
        if isinstance(message, dict):
            code = message.get("code")
            if code not in (0, 200, None):
                raise ApiError(
                    f"WEBTOON 接口错误：{message.get('message') or code}"
                )
            result = message.get("result")
            return result if isinstance(result, dict) else {}
        return payload if isinstance(payload, dict) else {}

    # ---------- 分类（语言 + 首页榜单） ----------
    def categories(self, cancel_event: threading.Event | None = None) -> list[Category]:
        rows: list[Category] = []
        rows.extend(Category(f"list={token}", label, "type") for token, label in LIST_KINDS)
        rows.extend(
            Category(f"lang={token}", label, "topic") for token, label in LANGUAGES
        )
        return rows

    def _apply_category(self, category: str) -> None:
        """分类 token 形如 `lang=en` / `list=originals`，只改对应的那一维。"""
        text = str(category or "").strip()
        if not text:
            return
        changed = False
        for part in text.replace("&", ",").split(","):
            if "=" not in part:
                continue
            key, value = (piece.strip() for piece in part.split("=", 1))
            if key == "lang" and value:
                self._lang = value
                changed = True
            elif key == "list" and value:
                self._list_kind = value
                changed = True
        if changed:
            self.session.data["lang"] = self._lang
            self.session.data["list"] = self._list_kind
            self.session.save()

    # ---------- 首页列表（网页版服务端渲染） ----------
    @staticmethod
    def _cards_from(root: HtmlNode) -> list[dict[str, Any]]:
        """解析 `ul.webtoon_list` 里的卡片。

        实测标记：
            <a data-title-no="4823" data-webtoon-type="WEBTOON"
               href=".../list?title_no=4823" data-genre="ROMANCE">
              <div class="image_wrap"><img src="https://webtoon-phinf.../x.jpg" alt="标题">
        标题优先取 img 的 alt（站点把作品名放在 alt 里）。
        """
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for anchor in query_all(root, ".webtoon_list li a[data-title-no]"):
            title_no = anchor.attr("data-title-no").strip()
            if not title_no or title_no in seen:
                continue
            seen.add(title_no)
            kind = anchor.attr("data-webtoon-type").strip().lower()
            if kind not in {"webtoon", "canvas"}:
                kind = "canvas" if "challenge" in anchor.attr("href").lower() else "webtoon"
            image = query_one(anchor, "img")
            title = ""
            cover = ""
            if image is not None:
                title = image.attr("alt").strip()
                cover = WebtoonProvider.abs_image_url(image.attr("src"))
            if not title:
                title = anchor.attr("data-title-no")
            items.append(
                {
                    "id": f"{kind}:{title_no}",
                    "slug": title_no,
                    "title": title,
                    "type": "manga",
                    "status": "-",
                    "views": 0,
                    "url": anchor.attr("href"),
                    "_url": anchor.attr("href"),
                    "poster": cover,
                    "genres": [anchor.attr("data-genre")] if anchor.attr("data-genre") else [],
                }
            )
        return items

    def browse_page(
        self,
        page: int = 1,
        category: str = "",
        cancel_event: threading.Event | None = None,
    ) -> PageResult:
        page = max(1, int(page or 1))
        self._apply_category(category)
        if page > 1:
            # 网页版榜单没有分页（带 ?page= 返回同一页），如实报告到底
            return PageResult(items=[], page=page, has_more=False, category=category)
        url = f"{WEB_BASE}/{self._lang}/{self._list_kind}"
        text, final_url = self.api.request_text(
            url,
            headers={
                # 网页版走 Akamai，认桌面 Chrome 的 UA；换成 app 的 UA 会只回 855 字节的拒绝页
                "User-Agent": WEB_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
                "Referer": f"{WEB_BASE}/",
            },
            cancel_event=cancel_event,
        )
        root = HtmlTreeParser.parse(text)
        items = [self.decorate_series(item) for item in self._cards_from(root)]
        logging.getLogger("dumbcat.webtoon").warning(
            "webtoon 列表 %s -> %s 字节, %s 张卡片 :: %s",
            final_url, len(text or ""), len(items), (text or "")[:300].replace(chr(10), " ")
        )
        return PageResult(
            items=items,
            page=page,
            has_more=False,
            total=len(items) or None,
            category=category,
            note=f"{dict(LANGUAGES).get(self._lang, self._lang)} · "
            f"{dict(LIST_KINDS).get(self._list_kind, self._list_kind)}",
        )

    def browse_series(
        self,
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        return self.browse_page(1, "", cancel_event).items

    # ---------- 搜索（应用接口） ----------
    @staticmethod
    def abs_image_url(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if text.startswith("//"):
            return "https:" + text
        if text.startswith("http://"):
            return "https://" + text[len("http://") :]
        if text.startswith("https://"):
            return text
        return IMAGE_CDN + (text if text.startswith("/") else f"/{text}")

    def _hit_to_series(self, item: dict[str, Any], kind: str) -> dict[str, Any] | None:
        try:
            title_no = int(item.get("titleNo") or 0)
        except (TypeError, ValueError):
            title_no = 0
        if not title_no:
            return None
        title = str(item.get("title") or item.get("titleName") or "").strip()
        if not title:
            return None
        authors = [
            str(item.get(key) or "").strip()
            for key in ("writingAuthorName", "pictureAuthorName")
        ]
        return {
            "id": f"{kind}:{title_no}",
            "slug": str(title_no),
            "title": title,
            "type": "manga",
            "status": "-",
            "views": int(item.get("readCount") or 0) or 0,
            "url": f"{WEB_BASE}/{self._lang}/list?title_no={title_no}",
            "_url": f"{WEB_BASE}/{self._lang}/list?title_no={title_no}",
            "poster": self.abs_image_url(
                str(item.get("thumbnail") or item.get("titleThumbnail") or item.get("iconImage") or "")
            ),
            "author": " / ".join(name for name in authors if name),
            "likes": int(item.get("likeitCount") or 0) or 0,
            "_kind": kind,
        }

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
        start = (page - 1) * PAGE_SIZE + 1
        result = self._api(
            "searchAll.json?"
            + urllib.parse.urlencode(
                {
                    "query": keyword,
                    "startIndex": start,
                    "pageSize": PAGE_SIZE,
                }
            )
            + f"&{self._common_qs()}",
            cancel_event,
        )
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        totals = 0
        for holder, kind in (
            (result.get("webtoonSearch"), "webtoon"),
            (result.get("challengeSearch"), "canvas"),
        ):
            if not isinstance(holder, dict):
                continue
            try:
                totals += int(holder.get("total") or 0)
            except (TypeError, ValueError):
                pass
            for raw in holder.get("titleList") or []:
                if not isinstance(raw, dict):
                    continue
                # 未满 18 岁内容默认不展示（站点自己的分级标记）
                if raw.get("ageGradeNotice") or raw.get("unsuitableForChildren"):
                    continue
                series = self._hit_to_series(raw, kind)
                if series is None or series["id"] in seen:
                    continue
                seen.add(series["id"])
                items.append(self.decorate_series(series))
        return PageResult(
            items=items,
            page=page,
            has_more=bool(items) and page * PAGE_SIZE < totals,
            total=totals or None,
        )

    def search_series(
        self,
        query: str,
        series_type: str = "manga",
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        return self.search_page(query, series_type, 1, cancel_event).items

    # ---------- 详情与章节 ----------
    @staticmethod
    def _split_id(value: str) -> tuple[str, int]:
        text = str(value or "")
        if ":" in text:
            kind, _, number = text.partition(":")
            try:
                return (kind or "webtoon"), int(number)
            except ValueError:
                return "webtoon", 0
        try:
            return "webtoon", int(text)
        except ValueError:
            return "webtoon", 0

    def get_chapters(
        self,
        series: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        kind, title_no = self._split_id(series.get("id") or series.get("slug") or "")
        if not title_no:
            return []
        path = (
            "challengeEpisodeList.json" if kind == "canvas" else "episodeList.json"
        )
        result = self._api(
            f"{path}?titleNo={title_no}&startIndex=0&pageSize=99999&{self._common_qs()}",
            cancel_event,
        )
        holder = result.get("episodeList")
        episodes = holder.get("episode") if isinstance(holder, dict) else []
        chapters: list[dict[str, Any]] = []
        for raw in episodes or []:
            if not isinstance(raw, dict):
                continue
            try:
                number = int(raw.get("episodeNo") or 0)
            except (TypeError, ValueError):
                continue
            if number <= 0:
                continue
            title = str(raw.get("episodeTitle") or raw.get("title") or "").strip()
            chapters.append(
                self.decorate_chapter(
                    series,
                    {
                        "id": str(number),
                        "slug": str(number),
                        "number": float(number),
                        "title": title or f"第 {number} 話",
                        "url": f"{WEB_BASE}/{self._lang}/list?title_no={title_no}&episode_no={number}",
                        "isFree": True,
                        "price": 0,
                        "publishStatus": "PUBLIC",
                        "_kind": kind,
                        "_title_no": title_no,
                    },
                )
            )
        # 应用接口给的是新→旧，阅读列表要旧→新
        chapters.sort(key=lambda item: float(item.get("number") or 0))
        return chapters

    def get_chapter_pages(
        self,
        series: dict[str, Any],
        chapter: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        kind, title_no = self._split_id(
            series.get("id") or chapter.get("_title_no") or series.get("slug") or ""
        )
        try:
            episode_no = int(chapter.get("id") or chapter.get("slug") or 0)
        except (TypeError, ValueError):
            return []
        if not title_no or not episode_no:
            return []
        if kind == "canvas":
            result = self._api(
                "challengeViewerEpisodeV1?"
                + urllib.parse.urlencode(
                    {
                        "readingRequestLanguage": self._lang,
                        "titleNo": title_no,
                        "episodeNo": episode_no,
                    }
                )
                + f"&{self._common_qs()}",
                cancel_event,
            )
            holder = result.get("challengeEpisode")
            images = holder.get("imageList") if isinstance(holder, dict) else []
        else:
            result = self._api(
                f"downloadImageList.json?titleNo={title_no}"
                f"&episodeNoList={episode_no}&{self._common_qs()}",
                cancel_event,
            )
            holder = result.get("downloadImageList")
            episodes = holder.get("episodeList") if isinstance(holder, dict) else []
            first = episodes[0] if isinstance(episodes, list) and episodes else {}
            images = first.get("imageInfo") if isinstance(first, dict) else []
        pages: list[dict[str, Any]] = []
        for index, raw in enumerate(images or []):
            if not isinstance(raw, dict):
                continue
            url = self.abs_image_url(str(raw.get("url") or ""))
            if not url:
                continue
            page: dict[str, Any] = {
                "page_number": int(raw.get("sortOrder") or index + 1) or index + 1,
                "image_url": url,
            }
            # 站点直接给了宽高，阅读器可以先用它排版
            for key in ("width", "height"):
                try:
                    value = int(raw.get(key) or 0)
                except (TypeError, ValueError):
                    value = 0
                if value:
                    page[key] = value
            pages.append(page)
        pages.sort(key=lambda item: int(item.get("page_number") or 0))
        for index, page in enumerate(pages, start=1):
            page["page_number"] = index
        return pages

    def chapter_referer(
        self,
        series: dict[str, Any],
        chapter: dict[str, Any],
    ) -> str:
        return f"{WEB_BASE}/"

    def fetch_image_bytes(self, url: str, referer: str = "") -> bytes:
        """WEBTOON 的图床对**没有 Referer 的请求直接 403**（实测），所以这里兜底。"""
        return super().fetch_image_bytes(
            self.abs_image_url(url), referer or f"{WEB_BASE}/"
        )
