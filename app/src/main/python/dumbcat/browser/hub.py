"""全局共享的 Chromium 会话（单例 + 单线程）。

为什么需要它：旧实现里每个浏览器型漫源各自 `launch_persistent_context`，
于是切换一次漫源就多一个 Chromium 进程 / 一个可见窗口，内存和句柄成倍增长。

现在整个程序只启动**一个** Chromium 进程和一个 persistent context，每个漫源
在里面用**独立 page**（Playwright 的 page 天然隔离 Cookie / LocalStorage），
所以：

- 任意时刻桌面上最多只有一个浏览器窗口；
- 切换漫源时旧 page 被关掉（`close_page`），共享 Chromium 内核不重启；
- 需要给用户看时复用同一个窗口（`show_window` 直接导航），不新开窗口；
- 只有用户主动点「打开网站」或需要站点原生阅读页时才显示窗口，其余
  （浏览、分类、分页、章节、取图）全部在 headless 下完成，不打扰用户也不占显卡。

## 线程模型（重要）

Playwright 的同步 API **只能在创建它的那个线程里使用**：跨线程调用会抛
`greenlet.error: cannot switch to a different thread`。每个漫源原先各有自己的
单线程执行器，却共用同一个 Playwright 实例——第一个浏览器漫源能用，切到第二个
就整体失败。

因此本模块自带**唯一一个**工作线程（`_run`），所有触碰 Playwright 对象的操作
都必须经它派发：漫源的执行器线程只负责提交任务，真正的 `page.goto/evaluate`
全部发生在 hub 线程上。`_run` 检测到自己已经在 hub 线程上时直接内联执行，
所以 hub 内部互相调用不会死锁。
"""
from __future__ import annotations

import concurrent.futures
import os
import threading
from pathlib import Path
from typing import Any, Callable

from dumbcat.browser.runtime import find_chromium_executable
from dumbcat.runtime_cleanup import remove_tree, runtime_root


class BrowserHubError(RuntimeError):
    pass


class _BrowserHub:
    """进程级单例：一个 Chromium、一个 persistent context、多个 page。"""

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    )

    #: 让 Chromium 尽量少吃内存与 CPU（用户明确要求「对设备运行要求最小」）
    BASE_ARGS = (
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--disable-extensions",
        "--disable-default-apps",
        "--disable-features=Translate,MediaRouter,OptimizationHints",
        "--metrics-recording-only",
        "--no-service-autorun",
        "--password-store=basic",
        "--window-size=1280,900",
    )

    #: headless 抓取时把可能出现的窗口挪到屏幕外（用户绝对看不到）
    OFFSCREEN_ARGS = ("--window-position=-32000,-32000",)

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="browser-hub",
        )
        self._thread_id: int | None = None
        self._playwright: Any = None
        self._context: Any = None
        self._pages: dict[str, Any] = {}
        self._visible = False
        self._shared_page: Any = None
        self._profile_dir: Path | None = None
        self.started_count = 0

    # ---------- 线程派发 ----------
    def on_hub_thread(self) -> bool:
        return self._thread_id is not None and self._thread_id == threading.get_ident()

    def _invoke(self, fn: Callable[..., Any], args: tuple[Any, ...]) -> Any:
        self._thread_id = threading.get_ident()
        return fn(*args)

    def run(
        self,
        fn: Callable[..., Any],
        *args: Any,
        timeout: float | None = None,
    ) -> Any:
        """在 hub 线程上执行 fn；已经在 hub 线程上时直接内联执行（可重入）。"""
        if self.on_hub_thread():
            return fn(*args)
        future = self._executor.submit(self._invoke, fn, args)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            raise BrowserHubError("浏览器操作超时") from exc

    # ---------- 对外状态 ----------
    @property
    def running(self) -> bool:
        return self._context is not None

    @property
    def visible(self) -> bool:
        return self._visible

    def page_names(self) -> list[str]:
        return sorted(self._pages)

    # ---------- 生命周期 ----------
    def _profile_path(self) -> Path:
        override = os.getenv("MANGA_BROWSER_PROFILE")
        if override:
            return Path(override).expanduser()
        app_data = os.getenv("APPDATA")
        root = Path(app_data) if app_data else Path.home()
        new_root = root / "DumbCatManga" / "browser_profiles" / "shared"
        legacy_root = root / "NuviatoonDownloader" / "browser_profiles" / "shared"
        if not new_root.exists() and legacy_root.exists():
            return legacy_root
        return new_root

    def _launch(self, visible: bool) -> dict[str, Any]:
        """启动（或按可见性重建）共享 context，并把已有 Cookie 迁移过去。"""
        browser_path = find_chromium_executable()
        if browser_path is None:
            raise BrowserHubError(
                "未找到 Chromium。请运行 `python -m playwright install chromium`，"
                "或设置 MANGA_CHROMIUM_PATH 指向 chrome.exe。"
            )
        from playwright.sync_api import sync_playwright

        state: dict[str, Any] = {}
        if self._context is not None:
            # 切换 headless/headed 必须重建 context；先把登录态取出来别弄丢。
            try:
                state = self._context.storage_state()
            except Exception:  # noqa: BLE001 - 拿不到就退化为未登录
                state = {}
            self._teardown_context()

        if self._playwright is None:
            self._playwright = sync_playwright().start()

        profile_dir = self._profile_path()
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._profile_dir = profile_dir
        cache_dir = runtime_root() / "shared-browser-cache"
        extra_args = [*self.BASE_ARGS, f"--disk-cache-dir={cache_dir}"]
        if not visible:
            extra_args.extend(self.OFFSCREEN_ARGS)
        options: dict[str, Any] = {
            "headless": not visible,
            "executable_path": str(browser_path),
            "ignore_default_args": ["--enable-automation"],
            "args": extra_args,
            "user_agent": self.USER_AGENT,
            "locale": "zh-CN",
            "viewport": {"width": 1280, "height": 900},
        }
        if state.get("cookies"):
            options["storage_state"] = state
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                str(profile_dir),
                **options,
            )
        except Exception as exc:
            self._context = None
            raise BrowserHubError(f"启动 Chromium 失败：{exc}") from exc
        self._visible = visible
        self.started_count += 1
        return state

    def _teardown_context(self) -> None:
        context = self._context
        self._context = None
        self._pages.clear()
        self._shared_page = None
        if context is not None:
            try:
                context.close()
            except Exception:  # noqa: BLE001 - 关闭阶段尽力而为
                pass

    def _ensure_context(self, visible: bool = False) -> None:
        if self._context is None:
            self._launch(visible)
            return
        if visible and not self._visible:
            self._launch(True)

    def ensure_visible(self, visible: bool = True) -> None:
        """确保共享会话以指定可见性运行（切换可见性会重建 context 并迁移 Cookie）。"""
        self.run(self._ensure_visible_impl, bool(visible), timeout=120.0)

    def _ensure_visible_impl(self, visible: bool) -> None:
        with self._lock:
            self._ensure_context(visible)

    def page_for(self, name: str) -> Any:
        """取得（必要时创建）名为 name 的独立 page。"""
        return self.run(self._page_for_impl, name)

    def _page_for_impl(self, name: str) -> Any:
        with self._lock:
            self._ensure_context(False)
            page = self._pages.get(name)
            if page is not None and not page.is_closed():
                return page
            try:
                page = self._context.new_page()
            except Exception as exc:
                raise BrowserHubError(f"创建浏览器页面失败：{exc}") from exc
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            self._pages[name] = page
            return page

    def request_context(self, name: str) -> Any:
        """共享 context 的 request API（自动带上该 context 的 Cookie，可下载图片）。"""
        return self.run(self._request_context_impl, name)

    def _request_context_impl(self, name: str) -> Any:
        with self._lock:
            if name not in self._pages:
                self._page_for_impl(name)
            return self._context.request

    def close_page(self, name: str) -> None:
        """关闭某个漫源的 page（释放内存），但保留共享 Chromium 进程。

        切换漫源时调用：桌面上不会堆积窗口，浏览器内核也不用反复启停。
        """
        try:
            self.run(self._close_page_impl, name, timeout=30.0)
        except BrowserHubError:
            pass

    def _close_page_impl(self, name: str) -> None:
        with self._lock:
            page = self._pages.pop(name, None)
            if page is self._shared_page:
                self._shared_page = None
        if page is not None:
            try:
                page.close()
            except Exception:  # noqa: BLE001 - 关闭阶段尽力而为
                pass

    def show_window(self, url: str = "") -> str:
        """显示共享窗口并把共享页面导航到 url（切换漫源时复用同一个窗口）。"""
        return str(self.run(self._show_window_impl, url, timeout=180.0))

    def _show_window_impl(self, url: str) -> str:
        with self._lock:
            self._ensure_context(True)
            page = self._shared_page
            if page is None or page.is_closed():
                page = self._context.new_page()
                self._shared_page = page
            try:
                if url:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.bring_to_front()
                return str(page.url)
            except Exception as exc:
                raise BrowserHubError(f"打开站点失败：{exc}") from exc

    def navigate_shared_window(self, url: str) -> str:
        """只做导航（窗口已显示时用它刷新，不重建 context）。"""
        return self.show_window(url)

    def cookies_for(self, url: str) -> list[dict[str, Any]]:
        """导出某个站点域名下的 Cookie（登录后供纯 HTTP 请求复用）。

        这是"每个漫源都能用官方账号登录"的统一实现：用户在站点自己的登录页
        （含验证码 / 二次验证）登完，程序只把结果 Cookie 收下来，不绕任何
        访问控制。
        """
        try:
            return list(self.run(self._cookies_for_impl, url, timeout=60.0))
        except BrowserHubError:
            return []

    def _cookies_for_impl(self, url: str) -> list[dict[str, Any]]:
        with self._lock:
            if self._context is None:
                return []
            try:
                items = self._context.cookies(url)
            except Exception:  # noqa: BLE001 - 没有会话/域名不匹配都返回空
                return []
        result: list[dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            result.append(
                {
                    "name": name,
                    "value": str(item.get("value") or ""),
                    "domain": str(item.get("domain") or ""),
                    "path": str(item.get("path") or "/"),
                }
            )
        return result

    def hide_window(self) -> None:
        """关闭可见窗口但保留会话（内存占用最小的状态）。"""
        try:
            self.run(self._hide_window_impl, timeout=180.0)
        except BrowserHubError:
            pass

    def _hide_window_impl(self) -> None:
        with self._lock:
            if not self._visible:
                return
            self._teardown_context()
            if self._playwright is not None:
                # 立即以 headless 重建，保持后续抓取仍可用
                self._launch(False)

    def clear_caches(self) -> int:
        """清掉共享 Chromium 的磁盘缓存与临时 profile 残留。"""
        return int(self.run(self._clear_caches_impl, timeout=60.0))

    def _clear_caches_impl(self) -> int:
        removed = 0
        for target in (
            runtime_root() / "shared-browser-cache",
            *runtime_root().glob("chromium-profile-*"),
            *runtime_root().glob("playwright_chromiumdev_profile-*"),
        ):
            try:
                size, ok = remove_tree(target)
            except OSError:
                continue
            if ok:
                removed += size
        return removed

    def close(self, wait_seconds: float = 15.0) -> None:
        """停止 Playwright，但保留工作线程（程序可能还要重新打开浏览器）。"""
        try:
            self.run(self._close_impl, timeout=max(5.0, float(wait_seconds)))
        except (BrowserHubError, Exception):  # noqa: BLE001 - 退出阶段尽力而为
            pass

    def _close_impl(self) -> None:
        with self._lock:
            self._teardown_context()
            playwright = self._playwright
            self._playwright = None
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:  # noqa: BLE001
                    pass

    def shutdown(self, wait_seconds: float = 10.0) -> None:
        """程序退出：停浏览器 + 收掉工作线程。"""
        self.close(wait_seconds)
        try:
            self._executor.shutdown(wait=True, cancel_futures=True)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="browser-hub",
            )
            self._thread_id = None


_HUB = _BrowserHub()


def hub() -> _BrowserHub:
    """取得进程级共享浏览器会话。"""
    return _HUB
