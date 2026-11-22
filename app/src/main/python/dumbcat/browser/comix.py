"""Comix 站点专用页面控制器。

Comix 的搜索、章节分页和阅读页都需要真实 DOM（阅读页有 scramble/canvas 特殊页），
因此走 `dumbcat.browser.hub` 的共享 Chromium：Comix 独占自己的 page，与其它漫源
的 Cookie 互相隔离，同时不会额外多开一个浏览器进程或窗口。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
from typing import Any, Callable

from dumbcat.browser.hub import BrowserHubError, hub


class BrowserError(RuntimeError):
    pass


class ComixBrowserClient:
    SITE_BASE = "https://comix.to"
    MAX_CHAPTER_PAGE_TURNS = 1000
    MAX_BROWSE_PAGES = 200
    SEARCH_CONTENT_RATINGS = ("safe", "suggestive", "erotica", "pornographic")
    SEARCH_CONTENT_RATING = ",".join(SEARCH_CONTENT_RATINGS)
    SEARCH_ORDER = "relevance:desc"

    # 结果网格就绪判据。站点改版过：旧版容器是 .list-grid，新版改成 BEM 命名的
    # a.card + .card__title（实测 .list-grid 已完全不存在，导致等待 30 秒后超时、
    # browse 直接失败）。两种标记都兼容，避免下次改版又挂。
    GRID_SELECTOR = ".list-grid, a.card[href*='/title/'], .card__title"
    GRID_READY = f"Boolean(document.querySelector(\"{GRID_SELECTOR}\"))"
    SEARCH_WAIT = (
        f"Boolean(document.querySelector(\"{GRID_SELECTOR}\") "
        "|| document.body.innerText.includes('No comics match'))"
    )
    ADVANCED_FILTERS_OPEN = r"""(() => {
        const label = [...document.querySelectorAll('.fdrop__label')].find(
            (node) => node.textContent.trim().toUpperCase() === 'SORT BY'
        );
        const dropdown = label && label.closest('.fdrop');
        const sortButton = dropdown && dropdown.querySelector('button.fdrop__btn');
        if (sortButton && sortButton.getClientRects().length > 0) return true;
        const toggle = document.querySelector('button.filter-adv-toggle');
        if (!toggle) return false;
        toggle.click();
        return true;
    })()"""
    SORT_CONTROL_WAIT = r"""(() => {
        const label = [...document.querySelectorAll('.fdrop__label')].find(
            (node) => node.textContent.trim().toUpperCase() === 'SORT BY'
        );
        const dropdown = label && label.closest('.fdrop');
        const button = dropdown && dropdown.querySelector('button.fdrop__btn');
        return !!button && button.getClientRects().length > 0;
    })()"""
    SORT_DROPDOWN_OPEN = r"""(() => {
        const label = [...document.querySelectorAll('.fdrop__label')].find(
            (node) => node.textContent.trim().toUpperCase() === 'SORT BY'
        );
        const dropdown = label && label.closest('.fdrop');
        const button = dropdown && dropdown.querySelector('button.fdrop__btn');
        if (!button) return false;
        if (button.getAttribute('aria-expanded') !== 'true') button.click();
        return true;
    })()"""
    SORT_OPTION_WAIT = r"""(() => {
        const label = [...document.querySelectorAll('.fdrop__label')].find(
            (node) => node.textContent.trim().toUpperCase() === 'SORT BY'
        );
        const dropdown = label && label.closest('.fdrop');
        const option = dropdown && [...dropdown.querySelectorAll('[role="option"]')].find(
            (node) => node.textContent.trim() === 'Best match'
        );
        return !!option && option.getClientRects().length > 0;
    })()"""
    SORT_BEST_MATCH_CLICK = r"""(() => {
        const label = [...document.querySelectorAll('.fdrop__label')].find(
            (node) => node.textContent.trim().toUpperCase() === 'SORT BY'
        );
        const dropdown = label && label.closest('.fdrop');
        const option = dropdown && [...dropdown.querySelectorAll('[role="option"]')].find(
            (node) => node.textContent.trim() === 'Best match'
        );
        if (!option) return false;
        const events = ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'];
        for (const type of events) {
            const EventType = type.startsWith('pointer') && window.PointerEvent ? PointerEvent : MouseEvent;
            option.dispatchEvent(new EventType(type, {
                bubbles: true,
                cancelable: true,
                view: window,
                button: 0,
                buttons: type.endsWith('down') ? 1 : 0,
            }));
        }
        return true;
    })()"""
    SORT_BEST_MATCH_WAIT = r"""(() => {
        const label = [...document.querySelectorAll('.fdrop__label')].find(
            (node) => node.textContent.trim().toUpperCase() === 'SORT BY'
        );
        const dropdown = label && label.closest('.fdrop');
        const value = dropdown && dropdown.querySelector('.fdrop__value');
        return !!value && value.textContent.trim() === 'Best match';
    })()"""
    SEARCH_RESULT_SIGNATURE = r"""(() => [
        ...document.querySelectorAll(".list-grid a[href*='/title/']")
    ].map((anchor) => anchor.getAttribute('href') || '').join('\u001f'))()"""
    SEARCH_EXTRACT = r"""(() => {
        // 兼容两代标记：旧版 .list-grid/.lrow，新版 BEM 的 a.card + .card__title
        // + .card__poster-wrap（实测新版已无 .list-grid）。
        const anchors = [...document.querySelectorAll("a[href*='/title/']")];
        const seen = new Set();
        const rows = [];
        for (const anchor of anchors) {
            const href = (anchor.getAttribute('href') || '').split('?')[0];
            if (!href.startsWith('/title/') || seen.has(href)) continue;
            seen.add(href);
            // 优先取包含该链接的最小卡片容器；找不到就用链接自身
            const row = (anchor.closest && anchor.closest('.card, .lrow')) || anchor;
            const titleNode = row.querySelector
                ? row.querySelector('.card__title, .lrow__title, h3')
                : null;
            const title = (
                titleNode ? titleNode.textContent
                    : anchor.getAttribute('aria-label') || anchor.textContent || ''
            ).trim();
            if (!title) continue;
            const typeNode = row.querySelector('.card__type, .lrow__type');
            const statusNode = row.querySelector('.card__status, .lrow__status');
            const image = row.querySelector('img');
            rows.push({
                href,
                title,
                type: typeNode ? typeNode.textContent.trim() : '',
                status: statusNode ? statusNode.textContent.trim() : '',
                poster: image
                    ? (image.currentSrc || image.getAttribute('src')
                       || image.getAttribute('data-src') || '')
                    : ''
            });
        }
        return rows;
    })()"""
    CHAPTER_WAIT = "Boolean(document.querySelector('.mpage__chapters'))"
    CHAPTER_EXTRACT = r"""(() => [...document.querySelectorAll('.mchap-row')].map((row) => {
        const anchor = row.querySelector('a.mchap-row__primary');
        const labelNode = row.querySelector('.mchap-row__ch');
        const label = (labelNode ? labelNode.textContent : '').trim();
        const priceNode = row.querySelector('.mchap-row__price, [class*="coin"], [class*="price"]');
        const text = (row.textContent || '').trim();
        return {
            href: anchor ? (anchor.getAttribute('href') || '').split('?')[0] : '',
            label,
            text,
            locked: !anchor || !!priceNode || /locked|purchase|paid/i.test(text),
            price: priceNode ? priceNode.textContent.trim() : ''
        };
    }).filter((item) => item.label || item.href))()"""
    CHAPTER_PAGE_STATE = r"""(() => {
        const parsePage = (value) => {
            const number = Number.parseInt(String(value || '').trim(), 10);
            return Number.isFinite(number) && number > 0 ? number : null;
        };
        const activeNode = document.querySelector('.npager__num.is-active');
        const active = (activeNode?.textContent || '1').trim();
        const activeNumber = parsePage(active);
        const numericPages = [...document.querySelectorAll('button.npager__num')]
            .map((button) => parsePage(button.textContent))
            .filter((page) => page !== null);
        const minPage = numericPages.length ? Math.min(...numericPages) : null;
        const maxPage = numericPages.length ? Math.max(...numericPages) : null;
        const rows = [...document.querySelectorAll('.mchap-row')];
        const signature = rows.map((row) => {
            const anchor = row.querySelector('a.mchap-row__primary');
            const label = row.querySelector('.mchap-row__ch');
            return [anchor ? anchor.getAttribute('href') || '' : '', label ? label.textContent.trim() : ''].join('|');
        }).join('\u001f');
        const next = document.querySelector(
            'button.npager__nav[aria-label="Next page"], button.nav-btn[aria-label="Next page"]'
        );
        const previous = document.querySelector(
            'button.npager__nav[aria-label="Previous page"], button.nav-btn[aria-label="Previous page"]'
        );
        const last = document.querySelector(
            'button.npager__nav[aria-label="Last page"], button.nav-btn[aria-label="Last page"]'
        );
        return {
            active,
            activeNumber,
            minPage,
            maxPage,
            signature,
            count: rows.length,
            hasNext: (!!next && !next.disabled)
                || (activeNumber !== null && maxPage !== null && activeNumber < maxPage),
            hasPrevious: (!!previous && !previous.disabled)
                || (activeNumber !== null && minPage !== null && activeNumber > minPage),
            hasLast: (!!last && !last.disabled)
                || (activeNumber !== null && maxPage !== null && activeNumber < maxPage)
        };
    })()"""
    LAST_PAGE_CLICK = r"""(() => {
        const isDisabled = (node) => node.disabled === true
            || node.getAttribute('aria-disabled') === 'true';
        const preferred = document.querySelector(
            'button.npager__nav[aria-label="Last page"], button.nav-btn[aria-label="Last page"]'
        );
        if (preferred && !isDisabled(preferred)) {
            preferred.click();
            return true;
        }
        const symbol = [...document.querySelectorAll('button, a')].find((node) => {
            if (node.getClientRects().length === 0 || isDisabled(node)) return false;
            const text = (node.textContent || '').trim();
            const label = node.getAttribute('aria-label') || node.getAttribute('title') || '';
            return text === '>>' || text === '»' || /last page/i.test(label);
        });
        if (symbol) {
            symbol.click();
            return true;
        }
        const pageButtons = [...document.querySelectorAll('button.npager__num')]
            .map((button, index) => {
                const page = Number.parseInt((button.textContent || '').trim(), 10);
                return Number.isFinite(page) ? {button, page, index} : null;
            })
            .filter(Boolean)
            .sort((left, right) => left.page - right.page || left.index - right.index);
        const lastNumber = pageButtons[pageButtons.length - 1];
        if (!lastNumber || lastNumber.button.classList.contains('is-active')) return false;
        lastNumber.button.click();
        return true;
    })()"""
    PREVIOUS_PAGE_CLICK = r"""(() => {
        const isDisabled = (node) => node.disabled === true
            || node.getAttribute('aria-disabled') === 'true';
        const preferred = document.querySelector(
            'button.npager__nav[aria-label="Previous page"], button.nav-btn[aria-label="Previous page"]'
        );
        if (preferred && !isDisabled(preferred)) {
            preferred.click();
            return true;
        }
        const symbol = [...document.querySelectorAll('button, a')].find((node) => {
            if (node.getClientRects().length === 0 || isDisabled(node)) return false;
            const text = (node.textContent || '').trim();
            const label = node.getAttribute('aria-label') || node.getAttribute('title') || '';
            return text === '<' || text === '‹' || text === '«' || /previous page/i.test(label);
        });
        if (symbol) {
            symbol.click();
            return true;
        }
        const activeButton = document.querySelector('button.npager__num.is-active');
        const activePage = Number.parseInt((activeButton?.textContent || '').trim(), 10);
        if (!Number.isFinite(activePage)) return false;
        const previous = [...document.querySelectorAll('button.npager__num')]
            .map((button, index) => {
                const page = Number.parseInt((button.textContent || '').trim(), 10);
                return Number.isFinite(page) && page < activePage ? {button, page, index} : null;
            })
            .filter(Boolean)
            .sort((left, right) => right.page - left.page || left.index - right.index)[0];
        if (!previous) return false;
        previous.button.click();
        return true;
    })()"""
    READ_WAIT = (
        "Boolean(document.querySelector('.rpage-page')) "
        "|| /locked|not available|purchase|unlock/i.test(document.body.innerText)"
    )
    READ_EXTRACT = r"""(() => [...document.querySelectorAll('.rpage-page')].map((element, index) => {
        const image = element.querySelector('img.rpage-page__img, img');
        const fiberKey = Object.keys(element).find((key) => key.startsWith('__reactFiber$'));
        let fiber = fiberKey ? element[fiberKey] : null;
        let page = null;
        for (let depth = 0; fiber && depth < 20; depth += 1) {
            const candidate = fiber.memoizedProps && fiber.memoizedProps.page;
            if (candidate && typeof candidate.url === 'string' && candidate.url) {
                page = candidate;
                break;
            }
            fiber = fiber.return;
        }
        const rawPage = Number(element.getAttribute('data-page'));
        const imageUrl = image ? (image.currentSrc || image.src || '') : '';
        return {
            page_number: Number.isFinite(rawPage) && rawPage > 0 ? rawPage : index + 1,
            image_url: page && page.url ? page.url : imageUrl,
            scrambled: !!(page && page.scramble)
        };
    }).filter((item) => item && item.image_url))()"""
    READER_SCROLL_TO_BOTTOM = r"""(() => {
        const reader = document.querySelector('.rpage-main--long-strip, .rpage-main');
        const target = reader || document.scrollingElement || document.documentElement;
        if (!target) return null;
        target.style.setProperty('scroll-behavior', 'auto', 'important');
        target.scrollTop = target.scrollHeight;
        return {
            scrollTop: target.scrollTop,
            scrollHeight: target.scrollHeight,
            clientHeight: target.clientHeight
        };
    })()"""
    READER_SCROLL_TO_TOP = r"""(() => {
        const reader = document.querySelector('.rpage-main--long-strip, .rpage-main');
        const target = reader || document.scrollingElement || document.documentElement;
        if (!target) return null;
        target.style.setProperty('scroll-behavior', 'auto', 'important');
        target.scrollTop = 0;
        return {scrollTop: target.scrollTop, scrollHeight: target.scrollHeight};
    })()"""
    READER_SCROLL_TO_INDEX = r"""(index) => {
        const pages = [...document.querySelectorAll('.rpage-page')];
        const element = pages[index];
        if (!element) return false;
        element.scrollIntoView({block: 'center', behavior: 'auto'});
        return true;
    }"""
    READER_LOAD_STATE = r"""(() => {
        const pages = [...document.querySelectorAll('.rpage-page')];
        let withSource = 0;
        let loaded = 0;
        for (const element of pages) {
            const image = element.querySelector('img.rpage-page__img, img');
            if (!image) continue;
            if (image.currentSrc || image.src) withSource += 1;
            if (image.complete && image.naturalWidth > 0) loaded += 1;
        }
        return {pages: pages.length, withSource, loaded};
    })()"""
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._closed = False
        self._window_open = False
        #: 轻量抓取模式（屏蔽图片/媒体/字体）当前是否生效
        self._light_mode = False
        #: 分类筛选控件基本不变：读一次后本次运行内复用
        self._categories_cache: list[dict[str, Any]] | None = None

    @property
    def _page(self) -> Any:
        try:
            page = hub().page_for("comix")
        except BrowserHubError as exc:
            raise BrowserError(str(exc)) from exc
        self._apply_light_mode(page)
        return page

    # ---------- 轻量模式：抓列表/搜索时不要图片、媒体、字体 ----------
    #: 列表页最重的就是几十张封面图。阅读页要读真实图片地址，那里会临时关掉。
    BLOCKED_RESOURCE_TYPES = ("image", "media", "font")

    def _route_handler(self, route: Any) -> None:
        try:
            if route.request.resource_type in self.BLOCKED_RESOURCE_TYPES:
                route.abort()
            else:
                route.continue_()
        except Exception:  # noqa: BLE001
            try:
                route.continue_()
            except Exception:  # noqa: BLE001
                pass

    def _apply_light_mode(self, page: Any) -> None:
        if self._light_mode:
            return
        try:
            page.route("**/*", self._route_handler)
            self._light_mode = True
        except Exception:  # noqa: BLE001 - 装不上就退回普通抓取
            self._light_mode = False

    def _apply_full_mode(self, page: Any) -> None:
        if not self._light_mode:
            return
        try:
            page.unroute("**/*", self._route_handler)
        except Exception:  # noqa: BLE001
            try:
                page.unroute("**/*")
            except Exception:  # noqa: BLE001
                pass
        self._light_mode = False

    def _close_impl(self) -> int:
        try:
            hub().close_page("comix")
        except Exception:
            pass
        self._window_open = False
        return 0

    def _execute(self, callback: Callable[..., Any], *args: Any) -> Any:
        """把回调派发到 hub 的唯一工作线程（Playwright 同步 API 线程亲和性）。"""
        with self._lock:
            # 共享 Chromium 一直在：切回 Comix 时自动复活（page 由 hub 重建）
            self._closed = False
        try:
            return hub().run(callback, *args)
        except BrowserError:
            raise
        except BrowserHubError as exc:
            raise BrowserError(str(exc)) from exc
        except Exception as exc:
            raise BrowserError(str(exc)) from exc

    def _wait_for(
        self,
        expression: str,
        timeout: float,
        cancel_event: threading.Event | None,
        message: str,
    ) -> Any:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                raise BrowserError("操作已停止")
            try:
                value = self._page.evaluate(expression)
                if value:
                    return value
            except Exception as exc:
                last_error = exc
            time.sleep(0.3)
        suffix = f"：{last_error}" if last_error else ""
        raise BrowserError(f"{message}{suffix}")

    def _wait_for_stable(
        self,
        expression: str,
        timeout: float,
        cancel_event: threading.Event | None,
        message: str,
        stable_samples: int = 3,
    ) -> Any:
        deadline = time.monotonic() + timeout
        last_value: Any = None
        stable_count = 0
        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                raise BrowserError("操作已停止")
            try:
                value = self._page.evaluate(expression)
            except Exception:
                value = None
            if value and value == last_value:
                stable_count += 1
                if stable_count >= stable_samples:
                    return value
            else:
                stable_count = 0
            last_value = value
            time.sleep(0.35)
        raise BrowserError(message)

    @staticmethod
    def _sort_navigation_wait(before_url: str, before_signature: str) -> str:
        before_query = urllib.parse.parse_qs(
            urllib.parse.urlparse(before_url).query
        )
        already_sorted = before_query.get("sort") == ["relevance:desc"]
        return f"""(() => {{
            const label = [...document.querySelectorAll('.fdrop__label')].find(
                (node) => node.textContent.trim().toUpperCase() === 'SORT BY'
            );
            const dropdown = label && label.closest('.fdrop');
            const value = dropdown && dropdown.querySelector('.fdrop__value');
            const params = new URLSearchParams(location.search);
            const signature = [...document.querySelectorAll(".list-grid a[href*='/title/']")]
                .map((anchor) => anchor.getAttribute('href') || '').join('\u001f');
            const changed = location.href !== {json.dumps(before_url)}
                || signature !== {json.dumps(before_signature)};
            return document.readyState === 'complete'
                && params.get('sort') === 'relevance:desc'
                && !!value && value.textContent.trim() === 'Best match'
                && signature.length > 0
                && ({str(already_sorted).lower()} || changed);
        }})()"""

    def _navigate(self, url: str, cancel_event: threading.Event | None) -> None:
        if cancel_event and cancel_event.is_set():
            raise BrowserError("操作已停止")
        token = f"manga-nav-{time.time_ns()}-{threading.get_ident()}"
        token_literal = json.dumps(token)
        self._page.evaluate(f"window.__mangaNavToken = {token_literal}; true")
        self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        expected = urllib.parse.urlparse(url)
        expected_origin = f"{expected.scheme}://{expected.netloc}"
        expected_path = expected.path or "/"
        self._wait_for(
            "(() => {"
            f"const token = {token_literal};"
            f"const origin = {json.dumps(expected_origin)};"
            f"const path = {json.dumps(expected_path)};"
            "return window.__mangaNavToken !== token"
            " && document.readyState === 'complete'"
            " && location.origin === origin"
            " && location.pathname === path;"
            "})()",
            45.0,
            cancel_event,
            "Comix 页面加载超时",
        )

    @classmethod
    def _search_url(cls, query: str) -> str:
        query_string = urllib.parse.urlencode(
            {
                "keyword": query.strip(),
                "order": cls.SEARCH_ORDER,
                "content_rating": cls.SEARCH_CONTENT_RATING,
            }
        )
        return f"{cls.SITE_BASE}/browse?{query_string}"

    def _select_best_match_sort(
        self,
        cancel_event: threading.Event | None,
    ) -> None:
        opened = self._page.evaluate(self.ADVANCED_FILTERS_OPEN)
        if not opened:
            raise BrowserError("Comix 高级筛选无法展开")
        self._wait_for(
            self.SORT_CONTROL_WAIT,
            15.0,
            cancel_event,
            "Comix 排序控件加载超时",
        )
        dropdown_opened = self._page.evaluate(self.SORT_DROPDOWN_OPEN)
        if not dropdown_opened:
            raise BrowserError("Comix 排序下拉框无法打开")
        self._wait_for(
            self.SORT_OPTION_WAIT,
            15.0,
            cancel_event,
            "Comix Best match 选项加载超时",
        )
        before_url = str(self._page.url)
        before_signature = str(
            self._page.evaluate(self.SEARCH_RESULT_SIGNATURE) or ""
        )
        selected = self._page.evaluate(self.SORT_BEST_MATCH_CLICK)
        if not selected:
            raise BrowserError("Comix 无法选择 Best match 排序")
        self._wait_for(
            self.SORT_BEST_MATCH_WAIT,
            15.0,
            cancel_event,
            "Comix Best match 排序未生效",
        )
        self._wait_for(
            self._sort_navigation_wait(before_url, before_signature),
            15.0,
            cancel_event,
            "Comix Best match 排序结果加载超时",
        )
        self._wait_for_stable(
            self.SEARCH_RESULT_SIGNATURE,
            15.0,
            cancel_event,
            "Comix Best match 排序结果未稳定",
        )

    def _search_impl(
        self,
        query: str,
        cancel_event: threading.Event | None,
    ) -> list[dict[str, Any]]:
        url = self._search_url(query)
        self._navigate(url, cancel_event)
        self._wait_for(
            self.SEARCH_WAIT,
            45.0,
            cancel_event,
            "Comix 搜索超时",
        )
        self._select_best_match_sort(cancel_event)
        results = self._page.evaluate(self.SEARCH_EXTRACT)
        return (
            [item for item in results if isinstance(item, dict)]
            if isinstance(results, list)
            else []
        )

    def search(
        self,
        query: str,
        cancel_event: threading.Event | None,
    ) -> list[dict[str, Any]]:
        return self._execute(self._search_impl, query, cancel_event)

    def _browse_impl(self, cancel_event: threading.Event | None) -> list[dict[str, Any]]:
        self._navigate("https://comix.to/", cancel_event)
        self._wait_for(
            self.GRID_READY,
            30.0,
            cancel_event,
            "Comix 首页内容加载超时",
        )
        results = self._page.evaluate(self.SEARCH_EXTRACT)
        return [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []

    def browse(self, cancel_event: threading.Event | None) -> list[dict[str, Any]]:
        return self._execute(self._browse_impl, cancel_event)

    # ---------- 分类与分页（/browse 是站点自带的完整目录页） ----------
    #: 站点 /browse 的分类筛选控件（勾选框形式），从这里读出站点自己的分类
    CATEGORY_EXTRACT = r"""(() => {
        const rows = [];
        const seen = new Set();
        const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
        const push = (token, label, kind) => {
            if (!token || !label) return;
            label = clean(label);
            if (label.length < 2 || label.length > 20) return;
            if (seen.has(token)) return;
            seen.add(token);
            rows.push({ token: token, label: label, kind: kind });
        };
        for (const input of document.querySelectorAll('input[type="checkbox"][name], input[type="checkbox"][value]')) {
            const token = input.getAttribute('name') || input.getAttribute('value') || '';
            if (!/genre|type|tag|theme|demo|status/i.test(token)) continue;
            const container = input.closest('label, li, .fdrop__item, div');
            const label = container ? clean(container.textContent) : '';
            const kind = /type/i.test(token) ? 'type' : (/status|demo/i.test(token) ? 'status' : 'genre');
            push(`${token}=${input.getAttribute('value') || ''}`, label, kind);
        }
        for (const anchor of document.querySelectorAll('a[href*="/browse?"]')) {
            const href = anchor.getAttribute('href') || '';
            const query = new URLSearchParams(href.split('?')[1] || '');
            for (const key of ['genres', 'types', 'tags', 'themes', 'statuses', 'demographics']) {
                if (!query.get(key)) continue;
                const kind = key === 'types' ? 'type' : (key === 'statuses' ? 'status' : 'genre');
                push(`${key}=${query.get(key)}`, clean(anchor.textContent), kind);
            }
        }
        return rows.slice(0, 300);
    })()"""
    #: /browse 列表页翻页按钮
    BROWSE_PAGE_EXTRACT = r"""(() => {
        const anchors = [...document.querySelectorAll('a[href]')];
        const next = anchors.find((anchor) => {
            const href = anchor.getAttribute('href') || '';
            const text = String(anchor.textContent || '').trim();
            const rel = (anchor.getAttribute('rel') || '').toLowerCase();
            if (!href.includes('/browse')) return false;
            return rel === 'next' || /^(next|下一页|下页|›|»|>)$/i.test(text);
        });
        const numbered = anchors
            .filter((anchor) => (anchor.getAttribute('href') || '').includes('/browse'))
            .map((anchor) => ({
                href: anchor.getAttribute('href') || '',
                label: String(anchor.textContent || '').trim(),
            }))
            .filter((item) => /^\d+$/.test(item.label))
            .map((item) => parseInt(item.label, 10));
        return {
            next: next ? next.getAttribute('href') : '',
            max: numbered.length ? Math.max(...numbered) : 0,
        };
    })()"""

    def browse_page(
        self,
        page: int = 1,
        category: str = "",
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        """读取 /browse 的一页（可带站点分类筛选）。"""
        return self._execute(self._browse_page_impl, page, category, cancel_event)

    def _browse_page_impl(
        self,
        page: int,
        category: str,
        cancel_event: threading.Event | None,
    ) -> list[dict[str, Any]]:
        page = max(1, int(page or 1))
        query: list[tuple[str, str]] = []
        if category:
            # token 形如 "genres=action" / "types=manga" 或直接的查询串
            query.extend(urllib.parse.parse_qsl(category, keep_blank_values=True))
        if page > 1:
            query.append(("page", str(page)))
        url = f"{self.SITE_BASE}/browse"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        self._navigate(url, cancel_event)
        self._wait_for(
            self.GRID_READY,
            30.0,
            cancel_event,
            "Comix 列表页加载超时",
        )
        # 列表页是懒加载：滚一遍等条目数稳定即可，不用固定睡 3×500ms
        self._settle_rows(cancel_event)
        results = self._page.evaluate(self.SEARCH_EXTRACT)
        return [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []

    def _settle_rows(
        self,
        cancel_event: threading.Event | None,
        timeout: float = 4.0,
        quiet_samples: int = 2,
    ) -> None:
        """滚到底触发懒加载，等条目数不再增长（连续两次一样就收工）。"""
        try:
            self._page.evaluate(
                "window.scrollTo(0, document.documentElement.scrollHeight)"
            )
        except Exception:  # noqa: BLE001
            return
        best = 0
        stable = 0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                return
            self._page.wait_for_timeout(150)
            rows = self._page.evaluate(self.SEARCH_EXTRACT)
            count = len(rows) if isinstance(rows, list) else 0
            if count > best:
                best = count
                stable = 0
                continue
            stable += 1
            if stable >= quiet_samples:
                break
        self._page.evaluate("window.scrollTo(0, 0)")

    def browse_meta(
        self,
        page: int = 1,
        category: str = "",
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """返回 {has_next, max_page}。

        必须在 `browse_page()` 之后紧接着调用（页面已经在目标列表页上），只读
        DOM 分页控件，不会再导航一次，避免重复加载。
        """
        try:
            return dict(self._execute(self._browse_meta_impl, page, category, cancel_event))
        except BrowserError:
            return {"has_next": False, "max_page": 0}

    def _browse_meta_impl(
        self,
        page: int,
        category: str,
        cancel_event: threading.Event | None,
    ) -> dict[str, Any]:
        data = self._page.evaluate(self.BROWSE_PAGE_EXTRACT)
        if not isinstance(data, dict):
            return {"has_next": False, "max_page": 0}
        max_page = int(data.get("max") or 0)
        current = max(1, int(page or 1))
        return {
            "has_next": bool(data.get("next")) or (max_page > current),
            "max_page": max_page,
            "next": str(data.get("next") or ""),
        }

    def categories(self, cancel_event: threading.Event | None = None) -> list[dict[str, Any]]:
        return list(self._execute(self._categories_impl, cancel_event))

    def _categories_impl(self, cancel_event: threading.Event | None) -> list[dict[str, Any]]:
        # 分类筛选控件基本不变：读一次就缓存，换分类时不用再导航一遍（省 2-3 秒）
        if self._categories_cache is not None:
            return list(self._categories_cache)
        self._navigate(f"{self.SITE_BASE}/browse", cancel_event)
        self._wait_for(self.GRID_READY, 30.0, cancel_event, "Comix 列表页加载超时")
        raw = self._page.evaluate(self.CATEGORY_EXTRACT)
        rows = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
        self._categories_cache = rows
        return list(rows)

    def open_site(self, url: str = "") -> str:
        """显示共享浏览器窗口（Comix 需要登录态或人工排查时使用）。"""
        return str(self._execute(self._open_shared, url or self.SITE_BASE))

    def _open_shared(self, url: str) -> str:
        try:
            hub().ensure_visible(True)
            self._window_open = True
            return hub().show_window(url)
        except BrowserHubError as exc:
            raise BrowserError(str(exc)) from exc

    def _get_chapters_impl(
        self,
        series: dict[str, Any],
        cancel_event: threading.Event | None,
    ) -> list[dict[str, Any]]:
        series_url = str(
            series.get("_url") or series.get("url") or series.get("href") or ""
        )
        if series_url.startswith("/"):
            series_url = "https://comix.to" + series_url
        if not series_url:
            raise BrowserError("Comix 作品缺少详情页地址")
        self._navigate(series_url, cancel_event)
        self._wait_for(
            self.CHAPTER_WAIT,
            45.0,
            cancel_event,
            "Comix 章节列表加载超时",
        )
        initial_state = self._page.evaluate(self.CHAPTER_PAGE_STATE)
        if not isinstance(initial_state, dict):
            raise BrowserError("Comix 章节分页状态读取失败")
        initial_active = str(initial_state.get("active") or "1")
        initial_signature = str(initial_state.get("signature") or "")
        initial_has_previous = bool(initial_state.get("hasPrevious"))
        if initial_state.get("hasLast"):
            clicked = self._page.evaluate(self.LAST_PAGE_CLICK)
            if not clicked:
                raise BrowserError("Comix 章节分页无法跳转到最后一页")
            wait_expression = (
                "(() => {"
                f" const state = {self.CHAPTER_PAGE_STATE};"
                f" const active = {json.dumps(initial_active)};"
                f" const signature = {json.dumps(initial_signature)};"
                f" const hasPrevious = {json.dumps(initial_has_previous)};"
                " return state.count > 0 && ("
                " state.active !== active"
                " || state.signature !== signature"
                " || state.hasPrevious !== hasPrevious);"
                "})()"
            )
            self._wait_for(
                wait_expression,
                15.0,
                cancel_event,
                "Comix 章节跳转到最后一页超时（页面内容尚未更新）",
            )
            time.sleep(0.6)
        page_batches: list[list[dict[str, Any]]] = []
        for _turn in range(self.MAX_CHAPTER_PAGE_TURNS):
            if cancel_event and cancel_event.is_set():
                raise BrowserError("操作已停止")
            state = self._page.evaluate(self.CHAPTER_PAGE_STATE)
            if not isinstance(state, dict):
                raise BrowserError("Comix 章节分页状态读取失败")
            active_page = str(state.get("active") or "1")
            signature = str(state.get("signature") or "")
            has_previous = bool(state.get("hasPrevious"))
            rows = self._page.evaluate(self.CHAPTER_EXTRACT)
            page_batches.append(
                [item for item in rows if isinstance(item, dict)]
                if isinstance(rows, list)
                else []
            )
            if not has_previous:
                return [
                    item
                    for batch in reversed(page_batches)
                    for item in batch
                ]
            clicked = self._page.evaluate(self.PREVIOUS_PAGE_CLICK)
            if not clicked:
                latest = self._page.evaluate(self.CHAPTER_PAGE_STATE)
                if isinstance(latest, dict) and not latest.get("hasPrevious"):
                    return [
                        item
                        for batch in reversed(page_batches)
                        for item in batch
                    ]
                raise BrowserError("Comix 章节分页无法返回上一页")
            wait_expression = (
                "(() => {"
                f" const state = {self.CHAPTER_PAGE_STATE};"
                f" const active = {json.dumps(active_page)};"
                f" const signature = {json.dumps(signature)};"
                " return state.count > 0 && ("
                " state.active !== active"
                " || state.signature !== signature"
                " || !state.hasPrevious);"
                "})()"
            )
            self._wait_for(
                wait_expression,
                15.0,
                cancel_event,
                "Comix 章节返回上一页超时（页面内容尚未更新）",
            )
            time.sleep(0.6)
        raise BrowserError(
            f"Comix 章节超过 {self.MAX_CHAPTER_PAGE_TURNS} 页，已停止加载"
        )

    def get_chapters(
        self,
        series: dict[str, Any],
        cancel_event: threading.Event | None,
    ) -> list[dict[str, Any]]:
        return self._execute(self._get_chapters_impl, series, cancel_event)

    @staticmethod
    def _dedupe_pages(rows: list[Any]) -> list[dict[str, Any]]:
        pages: dict[int, dict[str, Any]] = {}
        for index, item in enumerate(rows, start=1):
            if not isinstance(item, dict):
                continue
            image_url = item.get("image_url") or item.get("url")
            if not isinstance(image_url, str) or not image_url:
                continue
            try:
                page_number = int(item.get("page_number") or item.get("order") or index)
            except (TypeError, ValueError):
                page_number = index
            if page_number <= 0:
                page_number = index
            normalized = dict(item)
            normalized["page_number"] = page_number
            normalized["image_url"] = image_url
            pages[page_number] = normalized
        return [pages[number] for number in sorted(pages)]

    def _get_pages_impl(
        self,
        chapter_url: str,
        cancel_event: threading.Event | None,
    ) -> list[dict[str, Any]]:
        if chapter_url.startswith("/"):
            chapter_url = "https://comix.to" + chapter_url
        if not chapter_url:
            raise BrowserError("Comix 章节缺少阅读页地址")
        # 阅读页要读真实图片地址（含 scramble/canvas 特殊页），这里必须退出
        # 轻量模式让图片正常加载，否则提取不到内部状态里的地址。
        self._apply_full_mode(self._page)
        self._navigate(chapter_url, cancel_event)
        self._wait_for(
            self.READ_WAIT,
            45.0,
            cancel_event,
            "Comix 阅读页加载超时",
        )
        expected = int(self._page.locator('.rpage-page').count())
        if expected <= 0:
            return []

        def extract() -> list[dict[str, Any]]:
            raw = self._page.evaluate(self.READ_EXTRACT)
            return self._dedupe_pages(raw if isinstance(raw, list) else [])

        def scroll_edges() -> None:
            self._page.evaluate(self.READER_SCROLL_TO_BOTTOM)
            self._page.wait_for_timeout(900)
            self._page.evaluate(self.READER_SCROLL_TO_TOP)
            self._page.wait_for_timeout(450)

        scroll_edges()
        pages = extract()
        if len(pages) >= expected:
            return pages

        best = pages
        for _round in range(3):
            if cancel_event and cancel_event.is_set():
                raise BrowserError("操作已停止")
            for index in range(expected):
                if cancel_event and cancel_event.is_set():
                    raise BrowserError("操作已停止")
                self._page.evaluate(self.READER_SCROLL_TO_INDEX, index)
                self._page.wait_for_timeout(140)
            self._page.wait_for_timeout(1200)
            pages = extract()
            if len(pages) > len(best):
                best = pages
            scroll_edges()
            pages = extract()
            if len(pages) > len(best):
                best = pages
            if len(best) >= expected:
                return best
        return best

    def get_pages(
        self,
        chapter_url: str,
        cancel_event: threading.Event | None,
    ) -> list[dict[str, Any]]:
        return self._execute(self._get_pages_impl, chapter_url, cancel_event)

    def _clear_cache_impl(self) -> int:
        """清掉共享 Chromium 的磁盘缓存（不关闭窗口，避免搜索/取图反复闪退）。"""
        return hub().clear_caches()

    def clear_cache(self) -> int:
        try:
            return int(self._execute(self._clear_cache_impl))
        except BrowserError:
            return 0

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._close_impl()
