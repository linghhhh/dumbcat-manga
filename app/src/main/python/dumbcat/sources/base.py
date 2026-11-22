"""漫源抽象层：provider 基类与站点基址映射。

放在叶子位置，既被各漫源实现（dumbcat.sources.providers）继承，也被下载管线
（dumbcat.loader）引用，避免二者互相依赖形成循环。
"""
from __future__ import annotations

import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from dumbcat.app_config import (
    BAOZIMH_SITE_BASE,
    COMIC18_SITE_BASE,
    COMIX_SITE_BASE,
    COPYMANGA_SITE_BASE,
    MANGA18FX_SITE_BASE,
    QIMANGA_SITE_BASE,
    WNACG_SITE_BASE,
)

#: 站点原生整本下载完成标记：只有完整解压全部图片后才写入。
NATIVE_COMPLETE_SUFFIX = ".manga-native-complete"

#: 各漫源站点基址（浏览器型漫源自己在 __init__ 里传入，不走这张表）
SITE_BASE_URLS: dict[str, str] = {
    "qimanga": QIMANGA_SITE_BASE,
    "comix": COMIX_SITE_BASE,
    "manga18fx": MANGA18FX_SITE_BASE,
    "wnacg": WNACG_SITE_BASE,
    "18comic": COMIC18_SITE_BASE,
    "copycomic": COPYMANGA_SITE_BASE,
    "baozimh": BAOZIMH_SITE_BASE,
    "manhuagui": "https://tw.manhuagui.com",
    "bilibili": "https://manga.bilibili.com",
    "kuaikan": "https://www.kuaikanmanhua.com",
}


@dataclass(frozen=True)
class Category:
    """漫源自带的一个分类条目。

    token 回传给 `browse_page(category=...)`；label 用于界面显示；
    kind 用于分组（type 类型 / status 连载状态 / sort 排序 / topic 专题 / genre 题材）。
    """

    token: str
    label: str
    kind: str = "genre"


@dataclass(frozen=True)
class FavoriteFolder:
    """官方站点上的一个收藏夹。

    `token` 原样回传给 `add_favorite()` / `favorite_series()`；`count` 只有站点
    给了才填（没有就给 None，界面不要编数字）。
    """

    token: str
    name: str
    count: int | None = None


class LoginRequired(RuntimeError):
    """需要登录才能完成的操作（界面据此提示用户去登录）。"""

    def __init__(self, message: str, *, source: str = "") -> None:
        super().__init__(message)
        self.source = source


class FavoritesUnsupported(RuntimeError):
    """该漫源没有官方收藏夹（或还没实现）。"""


@dataclass
class PageResult:
    """一页浏览 / 搜索结果。

    has_more 为 True 时界面才显示「加载更多」；total 只在接口给了总数时有值。
    """

    items: list[dict[str, Any]] = field(default_factory=list)
    page: int = 1
    has_more: bool = False
    total: int | None = None
    category: str = ""
    note: str = ""

    def __bool__(self) -> bool:
        return bool(self.items)


class SeriesProvider:
    key = "base"
    display_name = "Unknown"
    output_prefix: tuple[str, ...] = ()

    #: 单页浏览 / 搜索的理想条数；各漫源按自己接口的限制覆写
    browse_page_size = 30

    def __init__(self, api: ApiClient) -> None:
        self.api = api

    # ---------- 分类与分页（界面「分类」下拉与「加载更多」用） ----------
    def uses_browser(self) -> bool:
        """该漫源是否需要浏览器（界面据此提示"已隐藏浏览器"等信息）。"""
        return False

    def categories(self, cancel_event: threading.Event | None = None) -> list[Category]:
        """该漫源自带的分类。

        默认返回空列表，表示不支持分类浏览；支持分类的漫源覆写本方法，返回
        站点自己的分类（类型 / 题材 / 排序 / 专题），界面直接按它分组显示。
        """
        return []

    def detail(self, series: dict[str, Any]) -> dict[str, Any]:
        """补充详情字段（题材标签、作者、简介…）。

        列表接口通常不给标签，需要再打一次详情页 / 详情接口。默认什么也不补，
        支持的图源覆写本方法；返回的字典会合并进作品的原始字段。
        """
        return {}

    def browse_page(
        self,
        page: int = 1,
        category: str = "",
        cancel_event: threading.Event | None = None,
    ) -> PageResult:
        """读取浏览列表的一页。

        默认实现只支持第 1 页（兼容那些没有分页接口的漫源）；有分页接口的
        漫源覆写本方法，界面就能通过「加载更多」把整个目录翻完。
        """
        if page and page > 1:
            return PageResult(items=[], page=page, has_more=False, category=category)
        items = self.browse_series(cancel_event)
        if category:
            items = self._filter_by_category(items, category)
        return PageResult(
            items=items,
            page=1,
            has_more=False,
            category=category,
        )

    def search_page(
        self,
        query: str,
        series_type: str = "manga",
        page: int = 1,
        cancel_event: threading.Event | None = None,
    ) -> PageResult:
        """搜索一页（同样支持「加载更多」）。"""
        if page and page > 1:
            return PageResult(items=[], page=page, has_more=False)
        return PageResult(
            items=self.search_series(query, series_type, cancel_event),
            page=1,
            has_more=False,
        )

    def _filter_by_category(
        self,
        items: list[dict[str, Any]],
        category: str,
    ) -> list[dict[str, Any]]:
        """默认分类过滤：把分类当成关键词在标题 / 类型 / 标签里匹配。

        只有不支持服务端分类的漫源才会走到这里，属于兜底而不是主路径。
        """
        token = str(category or "").strip().casefold()
        if not token:
            return items
        result: list[dict[str, Any]] = []
        for item in items:
            haystack = " ".join(
                str(item.get(name) or "")
                for name in ("title", "type", "status", "genres", "tags", "category")
            ).casefold()
            if token in haystack:
                result.append(item)
        return result

    def decorate_series(self, series: dict[str, Any]) -> dict[str, Any]:
        item = dict(series)
        if not item.get("poster"):
            for key in (
                "cover", "coverUrl", "cover_url", "coverImage", "cover_image",
                "poster_url", "thumbnail", "thumbnailUrl", "thumbnail_url",
                "image", "image_url", "thumb",
            ):
                value = item.get(key)
                if isinstance(value, dict):
                    value = value.get("url") or value.get("src") or value.get("image")
                if isinstance(value, str) and value.strip():
                    item["poster"] = value.strip()
                    break
        item["_provider"] = self.key
        item["_source_label"] = self.display_name
        return item

    def decorate_chapter(
        self,
        series: dict[str, Any],
        chapter: dict[str, Any],
    ) -> dict[str, Any]:
        item = dict(chapter)
        item["_provider"] = self.key
        item["_source_label"] = self.display_name
        item["_series_slug"] = series.get("slug")
        return item

    @staticmethod
    def is_locked(chapter: dict[str, Any]) -> bool:
        if bool(chapter.get("is_premium")):
            return True
        if chapter.get("requiresPurchase") is True:
            return True
        if chapter.get("isFree") is False:
            return True
        publish_status = str(chapter.get("publishStatus") or "").upper()
        if publish_status and publish_status != "PUBLIC":
            return True
        try:
            return float(chapter.get("price") or 0) > 0
        except (TypeError, ValueError):
            return True

    def output_root(self, base: Path, series: dict[str, Any]) -> Path:
        path = Path(base)
        for part in self.output_prefix:
            path /= part
        return path

    def search_series(
        self,
        query: str,
        series_type: str = "manga",
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def browse_series(
        self,
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        """Return a site's public/recommended manga list without a keyword."""
        return self.search_series("", "manga", cancel_event)

    def get_chapters(
        self,
        series: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_chapter_pages(
        self,
        series: dict[str, Any],
        chapter: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def chapter_referer(
        self,
        series: dict[str, Any],
        chapter: dict[str, Any],
    ) -> str:
        raise NotImplementedError

    def cleanup_chapter_cache(self) -> int:
        return 0

    def cleanup_runtime_cache(self) -> int:
        return 0

    def download_file(
        self,
        url: str,
        destination: Path,
        referer: str = "",
        cancel_event: threading.Event | None = None,
    ) -> str:
        return self.api.download_file(
            url,
            destination,
            cancel_event=cancel_event,
            referer=referer,
        )

    def fetch_image_bytes(self, url: str, referer: str = "") -> bytes:
        raw, _final_url, _charset = self.api.request_bytes(
            url,
            headers={"Referer": referer} if referer else None,
        )
        return raw

    def download_native(
        self,
        series: dict[str, Any],
        chapters: list[dict[str, Any]],
        output_root: Path,
        emit: Callable[..., None],
        cancel_event: threading.Event,
    ) -> bool | None:
        return None

    def open_site(self) -> None:
        webbrowser.open(self.site_base_url())

    # ---------- 登录态（官方账号） ----------
    @property
    def session(self) -> Any:
        """本漫源的登录态（Cookie / 令牌）。延迟导入避免循环依赖。"""
        if getattr(self, "_session", None) is None:
            from dumbcat.sources.session import session_for

            self._session = session_for(self.key)
        return self._session

    def is_logged_in(self) -> bool:
        """是否已登录官方账号。

        默认实现要求"用户确认过登录"。支持校验接口的漫源应该覆写本方法，
        真的去问一次服务器（那才是可信的登录状态）。
        """
        return bool(self.session.confirmed)

    def login_url(self) -> str:
        """官方登录页地址（用共享浏览器打开这里让用户登录）。"""
        return self.site_base_url()

    def login_hint(self) -> str:
        """给界面用的一句话说明（怎么登录、登录后能做什么）。"""
        return "在本程序的浏览器窗口里登录官方账号，登录态会被保存并用于读取/写入收藏夹。"

    def logout(self) -> None:
        """清除本漫源的登录态。"""
        self.session.clear()
        hub_close = getattr(self, "_forget_browser_state", None)
        if callable(hub_close):
            hub_close()

    def supports_password_login(self) -> bool:
        """是否支持在程序里直接用账号密码登录（而不是走浏览器）。"""
        return False

    def login(self, account: str, password: str) -> bool:
        """用账号密码登录官方站点；不支持时抛异常。"""
        raise FavoritesUnsupported(
            f"{self.display_name} 不支持在程序内用账号密码登录，请用「登录官方账号」在浏览器里登录。"
        )

    def capture_login_from_browser(self) -> int:
        """把共享浏览器里该站点的 Cookie 采集进本漫源的会话。

        这是所有漫源通用的登录方式：用户在自己熟悉的官方登录页登录（包括
        验证码、二次验证），程序只负责把结果 Cookie 收下来复用，不绕任何
        访问控制。
        """
        from dumbcat.browser.hub import hub
        from dumbcat.sources.session import session_for

        base = self.site_base_url()
        if not base:
            return 0
        cookies = hub().cookies_for(base)
        session = session_for(self.key)
        count = session.absorb_browser_cookies(cookies)
        header_hook = getattr(self, "browser_session_headers", None)
        if count and callable(header_hook):
            headers = header_hook()
            if isinstance(headers, dict) and headers:
                session.set_headers(headers)
        if count:
            # 用户点「完成登录」即视为确认登录；能校验的漫源会覆写 is_logged_in()
            session.mark_login_confirmed(True)
            session.save()
        return count

    # ---------- 官方收藏夹 ----------
    def favorite_series(
        self, folder: str = "", max_pages: int | None = None
    ) -> list[dict[str, Any]]:
        """读取当前登录态下的收藏列表（folder 为空表示"全部收藏"）。

        默认不支持；支持的漫源覆写本方法，`folder` 用 `favorite_folders()` 给的
        token。`max_pages` 是「最多翻多少页」的软上限，收藏上千条时调用方靠它
        控制等待时间；为 None 表示由漫源自己决定。
        """
        return []

    def favorite_folders(self) -> list[FavoriteFolder]:
        """列出官方站点上的收藏夹。默认不支持。"""
        raise FavoritesUnsupported(f"{self.display_name} 暂不支持读取收藏夹")

    def add_favorite(
        self,
        series: dict[str, Any],
        folder: FavoriteFolder | None = None,
    ) -> bool:
        """把作品收藏到官方收藏夹（folder 为 None 时用站点默认夹）。"""
        raise FavoritesUnsupported(f"{self.display_name} 暂不支持收藏到官方收藏夹")

    def remove_favorite(self, series: dict[str, Any]) -> bool:
        """从官方收藏夹移除作品。"""
        raise FavoritesUnsupported(f"{self.display_name} 暂不支持取消收藏")

    def supports_favorites(self) -> bool:
        """本漫源是否提供官方收藏夹能力（界面据此给出准确提示）。"""
        for name in ("favorite_folders", "add_favorite"):
            own = type(self).__dict__.get(name)
            if own is not None and own is not getattr(SeriesProvider, name):
                return True
        return False

    def open_favorites(self) -> None:
        webbrowser.open(self.site_base_url())

    def open_reader(self, series: dict[str, Any], chapter: dict[str, Any]) -> None:
        url = self.chapter_referer(series, chapter)
        if url:
            webbrowser.open(url)

    def close(self) -> None:
        """默认无资源可释放。

        纯 HTTP 的漫源不需要清理；浏览器型漫源会覆写此方法关闭 Chromium。
        统一提供这个方法，调用方就不必用 getattr 判空（此前 QiManga 等纯
        接口漫源没有 close()，收尾时会抛 AttributeError）。
        """
        return None

    def site_base_url(self) -> str:
        return SITE_BASE_URLS.get(self.key, "")

