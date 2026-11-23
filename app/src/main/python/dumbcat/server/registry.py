"""图源注册表：内置漫源 + 用户插件的统一入口。

内置的 10 个漫源（Qt 时代写的 ``SeriesProvider``）不重写，直接包成同一套
接口；用户插件走 :mod:`dumbcat.plugins.api`。上层（HTTP 接口、下载器）只认
这里的 ``SourceAdapter``，因此「加一个图源」不需要改任何核心代码。
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import importlib.util
import inspect
import json
import logging
import re
import shutil
import sys
import threading
import time
import traceback
import types
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from dumbcat.plugins.api import (
    API_VERSION,
    PluginContext,
    PluginError,
    SourcePlugin,
)
from dumbcat.server.config import (
    BUILTIN_PLUGIN_DIR,
    PLUGIN_CACHE_DIR,
    USER_PLUGIN_DIR,
    load_enabled_overlay,
    load_settings,
    save_enabled_overlay,
)

log = logging.getLogger("dumbcat.registry")

PLUGIN_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

#: 各调用的大致超时（秒）。超过就报错，避免一个坏插件把整页拖死。
TIMEOUTS = {"search": 75.0, "browse": 75.0, "detail": 60.0, "chapters": 90.0,
            "pages": 120.0, "image": 45.0, "categories": 40.0}

_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=12, thread_name_prefix="source-call"
)


def call_with_timeout(fn: Callable[[], Any], seconds: float, what: str) -> Any:
    future = _POOL.submit(fn)
    try:
        return future.result(timeout=seconds)
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        raise SourceError(f"{what}超时（超过 {int(seconds)} 秒）") from exc


class SourceError(RuntimeError):
    """图源调用失败的统一异常；message 会原样展示给用户。"""


@dataclass
class SourceInfo:
    key: str
    name: str
    kind: str = "builtin"          # builtin | plugin
    version: str = ""
    author: str = ""
    description: str = ""
    icon: str = "🐾"
    nsfw: bool = False
    base_url: str = ""
    origin: str = ""               # 插件来源文件，便于界面显示
    error: str = ""                # 非空表示加载失败
    enabled: bool = True

    def public(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "kind": self.kind,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "icon": self.icon,
            "nsfw": self.nsfw,
            "baseUrl": self.base_url,
            "origin": self.origin,
            "error": self.error,
            "enabled": self.enabled,
        }


# ---------------------------------------------------------------------------
# 统一适配器接口
# ---------------------------------------------------------------------------
class SourceAdapter:
    """上层视角的一个图源。子类只需要实现下面这些方法。"""

    info: SourceInfo

    @property
    def key(self) -> str:
        return self.info.key

    @property
    def name(self) -> str:
        return self.info.name

    def categories(self) -> list[dict[str, Any]]:
        return []

    def browse(self, page: int, category: str) -> tuple[list[dict[str, Any]], bool, int | None]:
        """返回 (原始作品列表, has_more, total)。"""
        raise NotImplementedError

    def search(self, query: str, page: int) -> tuple[list[dict[str, Any]], bool, int | None]:
        raise NotImplementedError

    def detail(self, series: dict[str, Any]) -> dict[str, Any]:
        return {}

    def chapters(self, series: dict[str, Any]) -> list[dict[str, Any]]:
        raise NotImplementedError

    def pages(self, series: dict[str, Any], chapter: dict[str, Any]) -> list[Any]:
        raise NotImplementedError

    def referer(self, series: dict[str, Any], chapter: dict[str, Any]) -> str:
        return ""

    def fetch_image(self, url: str, referer: str = "", chapter: dict[str, Any] | None = None,
                    series: dict[str, Any] | None = None) -> bytes:
        raise NotImplementedError

    # ---------- 登录态（官方账号 Cookie） ----------
    def _session(self) -> Any:
        from dumbcat.sources.session import session_for

        return session_for(self.key)

    def login_url(self) -> str:
        """站点登录页地址；空字符串表示这个图源不支持登录。"""
        return str(self.info.base_url or "")

    def login_hint(self) -> str:
        return ("在程序打开的站点页面里用官方账号登录（验证码、二次验证都行），"
                "登录完成后点「完成登录」，程序只保存结果 Cookie 供后续请求复用。")

    def login_info(self) -> dict[str, Any]:
        """给界面用的登录状态（不发任何网络请求）。"""
        try:
            session = self._session()
        except Exception:  # noqa: BLE001 - 登录态读不到不该让图源列表挂掉
            session = None
        cookies = dict(getattr(session, "cookies", {}) or {})
        url = self.login_url()
        return {
            "key": self.key,
            "name": self.name,
            "enabled": bool(url),
            "loggedIn": bool(getattr(session, "confirmed", False)),
            "hasCookies": bool(getattr(session, "has_credentials", False)),
            "cookieCount": len(cookies),
            "loginUrl": url,
            "hint": self.login_hint(),
            "supportsPassword": False,
            "updatedAt": float(getattr(session, "updated_at", 0) or 0),
        }

    def open_login(self) -> str:
        url = self.login_url()
        if not url:
            raise SourceError(f"{self.name} 没有可登录的站点地址")
        from dumbcat.browser.hub import hub

        hub().ensure_visible(True)
        return hub().show_window(url)

    def capture_login(self) -> int:
        """把共享浏览器里该站点的 Cookie 收进会话（用户点「完成登录」时调用）。"""
        url = self.login_url()
        if not url:
            raise SourceError(f"{self.name} 没有可登录的站点地址")
        from dumbcat.browser.hub import hub

        items = hub().cookies_for(url)
        session = self._session()
        count = session.absorb_browser_cookies(items)
        if count:
            session.mark_login_confirmed(True)
            session.save()
        return int(count)

    def password_login(self, account: str, password: str) -> bool:
        raise SourceError(f"{self.name} 不支持在程序里用账号密码登录，请用「打开登录页」。")

    def logout_source(self) -> None:
        self._session().clear()

    def close(self) -> None:
        pass


def _wrap_error(exc: BaseException) -> SourceError:
    if isinstance(exc, SourceError):
        return exc
    if isinstance(exc, PluginError):
        return SourceError(str(exc))
    return SourceError(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 内置漫源适配器
# ---------------------------------------------------------------------------
class BuiltinAdapter(SourceAdapter):
    def __init__(self, provider: Any) -> None:
        self.provider = provider
        # 图标 / 18+ / 说明 / 排序都来自 dumbcat/sources/json/<key>.json，
        # 没写才退回 provider 自己的默认值。
        meta: dict[str, Any] = dict(getattr(provider, "source_meta", {}) or {})
        key = str(provider.key)
        base_url = ""
        try:
            base_url = str(provider.site_base_url() or "")
        except Exception:  # noqa: BLE001 - 有的 provider 没有这个方法
            base_url = ""
        nsfw = meta.get("nsfw")
        if nsfw is None:
            nsfw = key in {"jm", "jjmhw", "wnacg", "manga18fx", "18comic"}
        self.info = SourceInfo(
            key=key,
            name=str(meta.get("name") or provider.display_name),
            kind="builtin",
            version="内置",
            author="呆猫漫画",
            description=str(meta.get("note") or "随程序内置的漫源"),
            icon=str(meta.get("icon") or "🍊"),
            nsfw=bool(nsfw),
            base_url=base_url,
            origin=str(meta.get("file") or "dumbcat/sources/json/"),
        )

    @staticmethod
    def _page_result(result: Any) -> tuple[list[dict[str, Any]], bool, int | None]:
        items = list(getattr(result, "items", []) or [])
        return items, bool(getattr(result, "has_more", False)), getattr(result, "total", None)

    def categories(self) -> list[dict[str, Any]]:
        def work() -> list[dict[str, Any]]:
            raw = self.provider.categories() or []
            out = []
            for item in raw:
                token = str(getattr(item, "token", "") or (item.get("token") if isinstance(item, dict) else ""))
                label = str(getattr(item, "label", "") or (item.get("label") if isinstance(item, dict) else token))
                kind = str(getattr(item, "kind", "genre") or "genre")
                if token:
                    out.append({"token": token, "label": label or token, "kind": kind})
            return out

        try:
            return call_with_timeout(work, TIMEOUTS["categories"], "读取分类")
        except Exception as exc:  # noqa: BLE001 - 分类失败不该拦住整页
            log.warning("分类读取失败 %s: %s", self.key, exc)
            return []

    def browse(self, page: int, category: str) -> tuple[list[dict[str, Any]], bool, int | None]:
        def work() -> Any:
            return self.provider.browse_page(page=page, category=category or "")

        try:
            result = call_with_timeout(work, TIMEOUTS["browse"], "浏览")
        except Exception as exc:
            raise _wrap_error(exc) from exc
        items, more, total = self._page_result(result)
        return [self.provider.decorate_series(item) for item in items], more, total

    def search(self, query: str, page: int) -> tuple[list[dict[str, Any]], bool, int | None]:
        def work() -> Any:
            return self.provider.search_page(query, page=page)

        try:
            result = call_with_timeout(work, TIMEOUTS["search"], "搜索")
        except Exception as exc:
            raise _wrap_error(exc) from exc
        items, more, total = self._page_result(result)
        return [self.provider.decorate_series(item) for item in items], more, total

    def detail(self, series: dict[str, Any]) -> dict[str, Any]:
        def work() -> Any:
            return self.provider.detail(series)

        try:
            extra = call_with_timeout(work, TIMEOUTS["detail"], "读取详情")
        except Exception as exc:  # noqa: BLE001 - 详情只是补充字段，失败不该拦住页面
            log.warning("详情读取失败 %s: %s", self.key, exc)
            return {}
        return extra if isinstance(extra, dict) else {}

    def chapters(self, series: dict[str, Any]) -> list[dict[str, Any]]:
        def work() -> Any:
            return self.provider.get_chapters(series)

        try:
            items = call_with_timeout(work, TIMEOUTS["chapters"], "读取章节")
        except Exception as exc:
            raise _wrap_error(exc) from exc
        return [self.provider.decorate_chapter(series, item) for item in (items or [])]

    def pages(self, series: dict[str, Any], chapter: dict[str, Any]) -> list[Any]:
        def work() -> Any:
            return self.provider.get_chapter_pages(series, chapter)

        try:
            return list(call_with_timeout(work, TIMEOUTS["pages"], "读取图片") or [])
        except Exception as exc:
            raise _wrap_error(exc) from exc

    def referer(self, series: dict[str, Any], chapter: dict[str, Any]) -> str:
        try:
            return str(self.provider.chapter_referer(series, chapter) or "")
        except Exception:
            return str(chapter.get("url") or series.get("url") or "")

    def fetch_image(self, url: str, referer: str = "", chapter: dict[str, Any] | None = None,
                    series: dict[str, Any] | None = None) -> bytes:
        def work() -> bytes:
            return self.provider.fetch_image_bytes(url, referer)

        try:
            return call_with_timeout(work, TIMEOUTS["image"], "下载图片")
        except Exception as exc:
            raise _wrap_error(exc) from exc

    def login_url(self) -> str:
        try:
            return str(self.provider.login_url() or "")
        except Exception:  # noqa: BLE001
            return str(self.info.base_url or "")

    def login_hint(self) -> str:
        try:
            return str(self.provider.login_hint() or "")
        except Exception:  # noqa: BLE001
            return super().login_hint()

    def login_info(self) -> dict[str, Any]:
        info = super().login_info()
        try:
            info["loggedIn"] = bool(self.provider.is_logged_in())
        except Exception:  # noqa: BLE001 - 校验失败就用会话里的确认状态
            pass
        try:
            info["supportsPassword"] = bool(self.provider.supports_password_login())
        except Exception:  # noqa: BLE001
            info["supportsPassword"] = False
        session = getattr(self.provider, "_session", None)
        username = ""
        try:
            username = str(session.data.get("username") or "") if session else ""
        except Exception:  # noqa: BLE001
            username = ""
        info["username"] = username
        return info

    def password_login(self, account: str, password: str) -> bool:
        def work() -> bool:
            return bool(self.provider.login(account, password))

        try:
            return bool(call_with_timeout(work, 90.0, "登录"))
        except Exception as exc:
            raise _wrap_error(exc) from exc

    def logout_source(self) -> None:
        session = self._session()
        session.clear()
        try:
            self.provider.logout()
        except Exception:  # noqa: BLE001 - 退出登录尽力而为
            pass

    def close(self) -> None:
        try:
            self.provider.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 用户插件适配器
# ---------------------------------------------------------------------------
class PluginAdapter(SourceAdapter):
    def __init__(self, plugin: SourcePlugin, info: SourceInfo, module: Any) -> None:
        self.plugin = plugin
        self.module = module
        self.info = info

    def categories(self) -> list[dict[str, Any]]:
        def work() -> list[dict[str, Any]]:
            out = []
            for item in self.plugin.categories() or []:
                token = getattr(item, "token", "") or (item.get("token", "") if isinstance(item, dict) else "")
                label = getattr(item, "label", "") or (item.get("label", "") if isinstance(item, dict) else token)
                kind = getattr(item, "kind", "genre") or "genre"
                if token:
                    out.append({"token": str(token), "label": str(label), "kind": str(kind)})
            return out

        try:
            return call_with_timeout(work, TIMEOUTS["categories"], "读取分类")
        except Exception as exc:  # noqa: BLE001
            log.warning("插件分类失败 %s: %s", self.key, exc)
            return []

    @staticmethod
    def _page(result: Any) -> tuple[list[dict[str, Any]], bool, int | None]:
        items = list(getattr(result, "items", []) or [])
        has_more = bool(getattr(result, "has_more", False))
        total = getattr(result, "total", None)
        return items, has_more, total

    def browse(self, page: int, category: str) -> tuple[list[dict[str, Any]], bool, int | None]:
        def work() -> Any:
            try:
                return self.plugin.browse(page=page, category=category or "")
            except TypeError:
                return self.plugin.browse(page)

        try:
            return self._page(call_with_timeout(work, TIMEOUTS["browse"], "浏览"))
        except Exception as exc:
            raise _wrap_error(exc) from exc

    def search(self, query: str, page: int) -> tuple[list[dict[str, Any]], bool, int | None]:
        def work() -> Any:
            try:
                return self.plugin.search(query, page=page)
            except TypeError:
                return self.plugin.search(query)

        try:
            return self._page(call_with_timeout(work, TIMEOUTS["search"], "搜索"))
        except Exception as exc:
            raise _wrap_error(exc) from exc

    def detail(self, series: dict[str, Any]) -> dict[str, Any]:
        try:
            extra = call_with_timeout(lambda: self.plugin.detail(series), TIMEOUTS["detail"], "读取详情")
            return extra if isinstance(extra, dict) else {}
        except Exception:
            return {}

    def chapters(self, series: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            items = call_with_timeout(lambda: self.plugin.chapters(series), TIMEOUTS["chapters"], "读取章节")
            return list(items or [])
        except Exception as exc:
            raise _wrap_error(exc) from exc

    def pages(self, series: dict[str, Any], chapter: dict[str, Any]) -> list[Any]:
        try:
            items = call_with_timeout(
                lambda: self.plugin.pages(series, chapter), TIMEOUTS["pages"], "读取图片"
            )
            return list(items or [])
        except Exception as exc:
            raise _wrap_error(exc) from exc

    def referer(self, series: dict[str, Any], chapter: dict[str, Any]) -> str:
        try:
            return str(self.plugin.referer(series, chapter) or "")
        except Exception:
            return str(chapter.get("url") or self.info.base_url or "")

    def fetch_image(self, url: str, referer: str = "", chapter: dict[str, Any] | None = None,
                    series: dict[str, Any] | None = None) -> bytes:
        try:
            return call_with_timeout(
                lambda: self.plugin.fetch_image(url, referer), TIMEOUTS["image"], "下载图片"
            )
        except Exception as exc:
            raise _wrap_error(exc) from exc

    def close(self) -> None:
        try:
            self.plugin.on_unload()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 插件加载
# ---------------------------------------------------------------------------
@dataclass
class LoadedPlugin:
    info: SourceInfo
    adapter: SourceAdapter | None
    module: Any = None
    path: Path | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.adapter is not None


def _manifest_for(path: Path) -> dict[str, Any]:
    if path.is_dir():
        manifest = path / "plugin.json"
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                return {}
    return {}


def _entry_file(path: Path, manifest: dict[str, Any]) -> Path | None:
    if path.is_file() and path.suffix == ".py":
        return path
    entry = str(manifest.get("entry") or "").strip()
    if entry:
        candidate = path / entry
        if candidate.exists():
            return candidate
    for name in ("main.py", "plugin.py", "__init__.py"):
        candidate = path / name
        if candidate.exists():
            return candidate
    py_files = sorted(path.glob("*.py"))
    return py_files[0] if py_files else None


def _module_name(key: str, path: Path) -> str:
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:8]
    return f"dumbcat_user_plugin_{key}_{digest}"


# ---------------------------------------------------------------------------
# 图源健康度：由 app.py 在浏览 / 详情成功或失败时打点，侧栏用它显示绿/黄/红点。
# 只存内存，重启归零——它回答的是「这个源现在还能不能打开」，不是历史统计。
# ---------------------------------------------------------------------------
_HEALTH: dict[str, dict[str, Any]] = {}
_HEALTH_LOCK = threading.Lock()


def note_health(key: str, ok: bool, ms: float | None = None, error: str = "") -> None:
    """记录一次对某图源的调用结果；连续失败保留最近一次错误，成功后清零。"""
    now = time.time()
    with _HEALTH_LOCK:
        item = _HEALTH.setdefault(
            key, {"ok": True, "fails": 0, "lastMs": 0.0, "lastError": "", "checkedAt": 0.0}
        )
        item["checkedAt"] = now
        if ms:
            item["lastMs"] = round(float(ms) * 1000, 1)
        if ok:
            item["ok"] = True
            item["fails"] = 0
            item["lastError"] = ""
        else:
            item["ok"] = False
            item["fails"] = int(item.get("fails") or 0) + 1
            item["lastError"] = str(error or "")[:200]


def health_snapshot() -> dict[str, dict[str, Any]]:
    with _HEALTH_LOCK:
        return {key: dict(value) for key, value in _HEALTH.items()}


class Registry:

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._adapters: dict[str, SourceAdapter] = {}
        self._failed: dict[str, LoadedPlugin] = {}
        self._builtin_providers: dict[str, Any] = {}
        self._loaded_at = 0.0
        self._last_error = ""
        self._user_dir = USER_PLUGIN_DIR
        self._builtin_dir = BUILTIN_PLUGIN_DIR
        self._extra_dirs: list[Path] = []

    # ---------- 生命周期 ----------
    def reload_all(self) -> None:
        """重建内置漫源 + 重新扫描用户插件。"""
        with self._lock:
            self._close_all()
            self._adapters.clear()
            self._failed.clear()
            self._load_builtins()
            self._load_user_plugins()
            self._loaded_at = time.time()

    def reload_plugins(self) -> dict[str, Any]:
        """只重扫用户插件，内置漫源保持不动（热重载按钮用）。"""
        with self._lock:
            plugin_keys = [
                key for key, adapter in self._adapters.items()
                if adapter.info.kind == "plugin"
            ]
            for key in plugin_keys:
                self._adapters.pop(key, None)
            self._failed.clear()
            self._load_user_plugins()
            self._loaded_at = time.time()
            return {"sources": len(self._adapters), "failed": len(self._failed)}

    def _close_all(self) -> None:
        for adapter in list(self._adapters.values()):
            try:
                adapter.close()
            except Exception:
                pass

    def _load_builtins(self) -> None:
        try:
            from dumbcat.sources.providers import build_providers

            include_breeze = bool(load_settings().include_browser_sources)
            providers = build_providers(
                include_breeze=include_breeze,
                enabled_overlay=load_enabled_overlay(),
            )
        except Exception as exc:  # noqa: BLE001 - 内置源全军覆没时仍要能跑插件
            self._last_error = f"内置漫源加载失败：{exc}"
            log.error(self._last_error)
            log.debug(traceback.format_exc())
            return
        self._builtin_providers = dict(providers)
        for key, provider in providers.items():
            try:
                adapter = BuiltinAdapter(provider)
                self._adapters[str(key)] = adapter
            except Exception as exc:  # noqa: BLE001
                log.error("内置漫源 %s 适配失败：%s", key, exc)
        self._load_builtin_rule_sources()

    def _load_builtin_rule_sources(self) -> None:
        """``dumbcat/sources/json/`` 里 ``engine: rules`` 的清单当图源加载。"""
        try:
            from dumbcat.sources.manifest import JSON_SOURCE_DIR
        except Exception:  # noqa: BLE001
            return
        if not JSON_SOURCE_DIR.is_dir():
            return
        seen = set(self._adapters)
        overlay = load_enabled_overlay()
        for path in sorted(JSON_SOURCE_DIR.glob("*.json")):
            try:
                cfg = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(cfg, dict) or str(cfg.get("engine") or "").lower() != "rules":
                continue
            _key, enabled = self._json_enabled(path, overlay)
            if not enabled:
                continue
            try:
                self._load_json_source(path, seen)
            except Exception as exc:  # noqa: BLE001
                log.error("规则图源 %s 加载失败：%s", path.name, exc)

    @staticmethod
    def _read_json_cfg(path: Path) -> dict[str, Any] | None:
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 坏文件当不存在
            return None
        return cfg if isinstance(cfg, dict) else None

    @staticmethod
    def _json_key(path: Path, cfg: dict[str, Any]) -> str:
        fallback = re.sub(r"[^a-z0-9_]", "_", path.stem.lower()) or "broken"
        if not PLUGIN_KEY_RE.match(fallback):
            fallback = "p_" + hashlib.sha1(fallback.encode()).hexdigest()[:8]
        declared = str(cfg.get("key") or "").strip().lower()
        if declared and PLUGIN_KEY_RE.match(declared):
            return declared
        return fallback

    def _json_enabled(
        self, path: Path, overlay: dict[str, bool] | None = None
    ) -> tuple[str, bool]:
        """返回 ``(key, 是否启用)``；用户拨过的开关（overlay）优先于清单。"""
        cfg = self._read_json_cfg(path)
        if cfg is None:
            return ("", True)
        key = self._json_key(path, cfg)
        if overlay and key in overlay:
            return (key, bool(overlay[key]))
        return (key, cfg.get("enabled") is not False)

    def _plugin_dirs(self) -> list[Path]:
        dirs = [self._user_dir]
        if self._builtin_dir.exists():
            dirs.append(self._builtin_dir)
        dirs.extend(self._extra_dirs)
        return dirs

    def _load_user_plugins(self) -> None:
        seen: set[str] = set()
        for folder in self._plugin_dirs():
            if not folder.exists():
                continue
            candidates: list[Path] = []
            for child in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
                if child.name.startswith((".", "_")):
                    continue
                if child.is_file() and child.suffix == ".py":
                    candidates.append(child)
                elif (
                    child.is_file()
                    and child.suffix == ".json"
                    and child.name != "plugin.json"
                ):
                    candidates.append(child)
                elif child.is_dir() and (child / "plugin.json").exists():
                    candidates.append(child)
            # 内置示例目录允许直接放 main.py 的文件夹
            if folder == self._builtin_dir:
                for child in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
                    if child.is_dir() and not (child / "plugin.json").exists():
                        if (child / "main.py").exists() or (child / "__init__.py").exists():
                            candidates.append(child)
            for path in candidates:
                loaded = self._load_one(path, seen)
                if loaded is None:
                    continue
                if loaded.ok:
                    self._adapters[loaded.info.key] = loaded.adapter  # type: ignore[assignment]
                else:
                    self._failed[loaded.info.key] = loaded

    def _load_json_source(self, path: Path, seen: set[str]) -> LoadedPlugin:
        """把一个 .json 文件变成图源（规则用 CSS 选择器描述，见 json_source.py）。"""
        from dumbcat.plugins.json_source import JsonRuleSource, config_errors

        fallback_key = re.sub(r"[^a-z0-9_]", "_", path.stem.lower()) or "broken"
        if not PLUGIN_KEY_RE.match(fallback_key):
            fallback_key = "p_" + hashlib.sha1(fallback_key.encode()).hexdigest()[:8]
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            detail = f"JSON 解析失败：{type(exc).__name__}: {exc}"
            return LoadedPlugin(
                info=SourceInfo(key=fallback_key, name=path.stem, kind="plugin",
                                origin=str(path), error=detail),
                adapter=None, path=path, error=detail,
            )
        errors = config_errors(cfg)
        declared = str(cfg.get("key") or "").strip().lower() if isinstance(cfg, dict) else ""
        key = declared or fallback_key
        if not PLUGIN_KEY_RE.match(key):
            key = fallback_key
        name = str(cfg.get("name") or path.stem) if isinstance(cfg, dict) else path.stem
        if errors:
            detail = "；".join(errors)
            return LoadedPlugin(
                info=SourceInfo(key=key, name=name, kind="plugin", origin=str(path), error=detail),
                adapter=None, path=path, error=detail,
            )
        if key in seen or key in self._builtin_providers:
            key = f"{key}_{hashlib.sha1(str(path).encode()).hexdigest()[:4]}"
        seen.add(key)
        attrs = {
            "CONFIG": cfg,
            "key": key,
            "name": name,
            "base_url": str(cfg.get("base_url") or ""),
            "version": str(cfg.get("version") or "1.0.0"),
            "author": str(cfg.get("author") or ""),
            "description": str(cfg.get("description") or "JSON 规则图源"),
            "icon": str(cfg.get("icon") or "📄"),
            "nsfw": bool(cfg.get("nsfw")),
            "page_size": int(cfg.get("page_size") or 30),
        }
        try:
            cls = type(f"JsonSource_{key}", (JsonRuleSource,), attrs)
            ctx = PluginContext(
                key,
                plugin_dir=path.parent,
                cache_dir=PLUGIN_CACHE_DIR / key,
                logger=logging.getLogger(f"dumbcat.plugin.{key}"),
            )
            instance = cls(ctx)
        except Exception as exc:  # noqa: BLE001
            detail = f"JSON 图源初始化失败：{type(exc).__name__}: {exc}"
            return LoadedPlugin(
                info=SourceInfo(key=key, name=name, kind="plugin", origin=str(path), error=detail),
                adapter=None, path=path, error=detail,
            )
        info = SourceInfo(
            key=key,
            name=name,
            kind="plugin",
            version=attrs["version"],
            author=attrs["author"],
            description=attrs["description"],
            icon=attrs["icon"],
            nsfw=attrs["nsfw"],
            base_url=attrs["base_url"],
            origin=str(path),
        )
        module = types.SimpleNamespace(__file__=str(path), __name__=f"dumbcat_json_source_{key}")
        return LoadedPlugin(
            info=info,
            adapter=PluginAdapter(instance, info, module),
            module=module,
            path=path,
        )

    def _load_one(self, path: Path, seen: set[str]) -> LoadedPlugin | None:
        if path.is_file() and path.suffix.lower() == ".json":
            _key, enabled = self._json_enabled(path, load_enabled_overlay())
            if not enabled:
                return None
            return self._load_json_source(path, seen)
        manifest = _manifest_for(path)
        entry = _entry_file(path, manifest)
        declared_key = str(manifest.get("key") or "").strip()
        if entry is None:
            key = declared_key or re.sub(r"[^a-z0-9_]", "_", path.stem.lower())
            return LoadedPlugin(
                info=SourceInfo(key=key or "broken", name=path.stem, kind="plugin",
                                origin=str(path), error="找不到入口 .py 文件"),
                adapter=None, path=path, error="找不到入口 .py 文件",
            )
        key_hint = declared_key or re.sub(r"[^a-z0-9_]", "_", entry.stem.lower())
        if not PLUGIN_KEY_RE.match(key_hint):
            key_hint = "p_" + hashlib.sha1(key_hint.encode()).hexdigest()[:8]
        module_name = _module_name(key_hint, entry)
        added_path = False
        try:
            spec = importlib.util.spec_from_file_location(module_name, entry)
            if spec is None or spec.loader is None:
                raise ImportError(f"无法读取 {entry.name}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            plugin_root = str(entry.parent)
            if plugin_root not in sys.path:
                sys.path.insert(0, plugin_root)
                added_path = True
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            log.warning("插件加载失败 %s：%s", path, detail)
            sys.modules.pop(module_name, None)
            if added_path:
                try:
                    sys.path.remove(str(entry.parent))
                except ValueError:
                    pass
            return LoadedPlugin(
                info=SourceInfo(key=key_hint, name=path.stem, kind="plugin",
                                origin=str(path), error=detail),
                adapter=None, path=path, error=detail,
            )
        finally:
            pass
        # 找到插件类
        cls: type[SourcePlugin] | None = None
        candidate = getattr(module, "PLUGIN", None)
        if inspect.isclass(candidate) and issubclass(candidate, SourcePlugin):
            cls = candidate
        if cls is None:
            for _name, obj in vars(module).items():
                if (
                    inspect.isclass(obj)
                    and issubclass(obj, SourcePlugin)
                    and obj is not SourcePlugin
                    and obj.__module__ == module.__name__
                ):
                    cls = obj
                    break
        api_version = int(getattr(module, "API_VERSION", API_VERSION) or API_VERSION)
        if cls is None:
            detail = "模块里没有 SourcePlugin 子类"
            return LoadedPlugin(
                info=SourceInfo(key=key_hint, name=path.stem, kind="plugin",
                                origin=str(path), error=detail),
                adapter=None, module=module, path=path, error=detail,
            )
        if api_version > API_VERSION:
            detail = f"插件要求 API v{api_version}，当前只支持 v{API_VERSION}"
            return LoadedPlugin(
                info=SourceInfo(key=key_hint, name=path.stem, kind="plugin",
                                origin=str(path), error=detail),
                adapter=None, module=module, path=path, error=detail,
            )
        plugin_key = str(getattr(cls, "key", "") or key_hint).strip().lower()
        if not PLUGIN_KEY_RE.match(plugin_key):
            plugin_key = key_hint
        if plugin_key in seen or plugin_key in self._builtin_providers:
            plugin_key = f"{plugin_key}_{hashlib.sha1(str(path).encode()).hexdigest()[:4]}"
        seen.add(plugin_key)
        try:
            ctx = PluginContext(
                plugin_key,
                plugin_dir=entry.parent,
                cache_dir=PLUGIN_CACHE_DIR / plugin_key,
                logger=logging.getLogger(f"dumbcat.plugin.{plugin_key}"),
            )
            instance = cls(ctx)
        except Exception as exc:  # noqa: BLE001
            detail = f"插件初始化失败：{type(exc).__name__}: {exc}"
            return LoadedPlugin(
                info=SourceInfo(key=plugin_key, name=str(getattr(cls, "name", "") or path.stem),
                                kind="plugin", origin=str(path), error=detail),
                adapter=None, module=module, path=path, error=detail,
            )
        info = SourceInfo(
            key=plugin_key,
            name=str(getattr(instance, "name", "") or path.stem),
            kind="plugin",
            version=str(getattr(instance, "version", "") or manifest.get("version") or ""),
            author=str(getattr(instance, "author", "") or manifest.get("author") or ""),
            description=str(getattr(instance, "description", "") or manifest.get("description") or ""),
            icon=str(getattr(instance, "icon", "") or "🐾"),
            nsfw=bool(getattr(instance, "nsfw", False)),
            base_url=str(getattr(instance, "base_url", "") or ""),
            origin=str(entry),
        )
        return LoadedPlugin(info=info, adapter=PluginAdapter(instance, info, module), module=module,
                            path=path)

    # ---------- 查询 ----------
    def get(self, key: str) -> SourceAdapter | None:
        return self._adapters.get(key)

    def require(self, key: str) -> SourceAdapter:
        adapter = self._adapters.get(key)
        if adapter is None:
            raise SourceError(f"图源不存在或未启用：{key}")
        return adapter

    def sources(self) -> list[dict[str, Any]]:
        """全部图源：已加载的 + 加载失败的 + 被关掉的（enabled=False）。

        关掉的图源也要列出来，否则图源管理里一旦关闭就再也打不开了。
        """
        overlay = load_enabled_overlay()
        with self._lock:
            items = [adapter.info.public() for adapter in self._adapters.values()]
            items.extend(entry.info.public() for entry in self._failed.values())
            seen = {item["key"] for item in items}
            for info in self._disabled_infos(overlay):
                if info.key in seen:
                    continue
                seen.add(info.key)
                items.append(info.public())
        items.sort(key=lambda item: (item["kind"] != "builtin", item["name"].lower()))
        return items

    def _disabled_infos(self, overlay: dict[str, bool]) -> list[SourceInfo]:
        """被关掉的图源（内置清单 + 规则型 + 用户目录），只用于列表展示。"""
        infos: dict[str, SourceInfo] = {}
        try:
            from dumbcat.sources.manifest import JSON_SOURCE_DIR, load_specs

            for spec in load_specs(include_disabled=True, enabled_overlay=overlay):
                if spec.get("enabled") is not False:
                    continue
                key = str(spec["key"])
                infos[key] = SourceInfo(
                    key=key,
                    name=str(spec.get("name") or key),
                    kind="builtin",
                    icon=str(spec.get("icon") or "🐾"),
                    nsfw=bool(spec.get("nsfw")),
                    base_url=str(spec.get("homepage") or ""),
                    origin=str(spec.get("path") or ""),
                    enabled=False,
                )
            roots = [JSON_SOURCE_DIR]
        except Exception:  # noqa: BLE001 - 清单读不出来不影响已加载的源
            roots = []
        roots.append(self._user_dir)
        for root in roots:
            try:
                paths = sorted(root.glob("*.json")) if root.is_dir() else []
            except OSError:
                paths = []
            for path in paths:
                if path.name == "plugin.json":
                    continue
                raw = self._read_json_cfg(path)
                if raw is None:
                    continue
                declared = str(raw.get("key") or "").strip().lower()
                if declared and declared in infos:
                    # 已在上面按清单列过了（python 型引擎），别重复
                    continue
                key, enabled = self._json_enabled(path, overlay)
                if not key or enabled or key in infos:
                    continue
                cfg = raw
                infos[key] = SourceInfo(
                    key=key,
                    name=str(cfg.get("name") or path.stem),
                    kind="plugin" if root == self._user_dir else "builtin",
                    icon=str(cfg.get("icon") or "🐾"),
                    nsfw=bool(cfg.get("nsfw")),
                    base_url=str(cfg.get("base_url") or ""),
                    origin=str(path),
                    enabled=False,
                )
        return list(infos.values())

    def set_source_enabled(self, key: str, enabled: bool) -> dict[str, Any]:
        """打开 / 关闭一个图源：写覆盖层，然后重载注册表。"""
        name = str(key or "").strip().lower()
        if not name:
            raise SourceError("图源标识为空")
        if name not in self._known_source_keys():
            raise SourceError(f"图源不存在：{name}")
        overlay = load_enabled_overlay()
        overlay[name] = bool(enabled)
        save_enabled_overlay(overlay)
        self.reload_all()
        return {"key": name, "enabled": bool(enabled), "stats": self.stats()}

    def _known_source_keys(self) -> set[str]:
        keys = {item["key"] for item in self.sources()}
        keys.update(load_enabled_overlay())
        try:
            from dumbcat.sources.manifest import load_specs

            keys.update(str(spec["key"]) for spec in load_specs(include_disabled=True))
        except Exception:  # noqa: BLE001
            pass
        return keys

    def failed(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {**entry.info.public(), "error": entry.error}
                for entry in self._failed.values()
            ]

    @property
    def last_error(self) -> str:
        return self._last_error

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "sources": len(self._adapters),
                "builtin": sum(1 for a in self._adapters.values() if a.info.kind == "builtin"),
                "plugins": sum(1 for a in self._adapters.values() if a.info.kind == "plugin"),
                "failed": len(self._failed),
                "loadedAt": self._loaded_at,
                "error": self._last_error,
            }

    # ---------- 插件文件管理 ----------
    def plugin_dir(self) -> Path:
        return self._user_dir

    def install_from_zip(self, data: bytes) -> dict[str, Any]:
        """解压到用户插件目录；只接受 .py / plugin.json / 子目录结构，防目录穿越。"""
        import io

        target_root = self._user_dir
        target_root.mkdir(parents=True, exist_ok=True)
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise SourceError("不是有效的 zip 文件") from exc
        names = [name for name in archive.namelist() if not name.endswith("/")]
        if not names:
            raise SourceError("zip 里没有文件")
        # 若所有文件都在同一个顶层目录里，直接以该目录名安装
        prefixes = {name.split("/")[0] for name in names}
        strip_prefix = ""
        if len(prefixes) == 1 and all("/" in name for name in names):
            strip_prefix = names[0].split("/")[0] + "/"
        installed: list[str] = []
        total = 0
        for name in names:
            relative = name[len(strip_prefix):] if strip_prefix and name.startswith(strip_prefix) else name
            relative = relative.replace("\\", "/").strip("/")
            if not relative or ".." in relative.split("/"):
                continue
            if not (relative.endswith(".py") or relative.endswith(".json")
                    or relative.endswith(".md") or relative.endswith(".txt")
                    or relative.endswith(".png") or relative.endswith(".svg")):
                continue
            info = archive.getinfo(name)
            total += info.file_size
            if total > 8 * 1024 * 1024:
                raise SourceError("插件包太大（超过 8MB）")
            destination = target_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, open(destination, "wb") as sink:
                shutil.copyfileobj(source, sink)
            installed.append(relative)
        if not any(item.endswith(".py") for item in installed):
            raise SourceError("压缩包里没有 .py 插件文件")
        self.reload_plugins()
        return {"installed": installed, "count": len(installed)}

    def read_plugin_file(self, filename: str) -> dict[str, Any]:
        """读取用户插件目录里的源码，供界面编辑器使用。"""
        raw = str(filename or "").strip()
        if not raw:
            raise SourceError("没有指定插件文件")
        path = Path(raw)
        if not path.is_absolute():
            path = self._user_dir / path
        try:
            local = path.resolve()
            user_root = self._user_dir.resolve()
        except OSError as exc:
            raise SourceError(f"路径无效：{exc}") from exc
        if not str(local).startswith(str(user_root)):
            raise SourceError("只能编辑用户插件目录里的文件")
        if not local.is_file():
            raise SourceError("文件不存在")
        if local.suffix.lower() not in {".py", ".json"}:
            raise SourceError("只能编辑 .py / .json 图源文件")
        if local.stat().st_size > 1024 * 1024:
            raise SourceError("源码超过 1MB")
        try:
            content = local.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = local.read_text(encoding="utf-8", errors="replace")
        try:
            relative = str(local.relative_to(user_root))
        except ValueError:
            relative = local.name
        return {"file": relative, "content": content}

    def save_plugin_file(self, filename: str, content: str) -> dict[str, Any]:
        name = Path(filename).name
        suffix = Path(name).suffix.lower()
        if suffix not in {".py", ".json"} or name.startswith("."):
            raise SourceError("只接受 .py 插件或 .json 图源文件")
        if name == "plugin.json":
            raise SourceError("plugin.json 是插件目录清单，请直接编辑插件目录")
        if len(content.encode("utf-8")) > 1024 * 1024:
            raise SourceError("文件超过 1MB")
        if suffix == ".json":
            from dumbcat.plugins.json_source import config_errors

            try:
                cfg = json.loads(content)
            except ValueError as exc:
                raise SourceError(f"JSON 格式不对：{exc}") from exc
            errors = config_errors(cfg)
            if errors:
                raise SourceError("；".join(errors))
        self._user_dir.mkdir(parents=True, exist_ok=True)
        target = self._user_dir / name
        target.write_text(content, encoding="utf-8")
        self.reload_plugins()
        return {"file": name, "reloaded": True}

    def delete_plugin(self, key: str) -> dict[str, Any]:
        adapter = self._adapters.get(key)
        origin = ""
        if adapter is not None and adapter.info.kind == "plugin":
            origin = adapter.info.origin
        elif key in self._failed:
            origin = str(self._failed[key].path or "")
        if not origin:
            raise SourceError("内置图源不能删除")
        path = Path(origin)
        try:
            local = path.resolve()
            user_root = self._user_dir.resolve()
            builtin_root = self._builtin_dir.resolve()
            if str(local).startswith(str(user_root)):
                target = path.parent if path.name.lower() in {
                    "main.py", "plugin.py", "__init__.py", "plugin.json"
                } else path
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
            elif str(local).startswith(str(builtin_root)):
                raise SourceError("示例插件属于程序目录，请先复制到用户插件目录再修改")
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            raise SourceError(f"删除失败：{exc}") from exc
        self.reload_plugins()
        return {"deleted": key}

    def copy_builtin_to_user(self, key: str) -> dict[str, Any]:
        """把内置示例插件复制一份到用户插件目录，方便用户改。"""
        adapter = self._adapters.get(key)
        if adapter is None or adapter.info.kind != "plugin":
            raise SourceError("只能复制插件")
        origin = Path(adapter.info.origin)
        if not str(origin.resolve()).startswith(str(self._builtin_dir.resolve())):
            raise SourceError("这个插件已经在用户目录里了")
        source_dir = origin.parent
        target = self._user_dir / f"{key}_copy"
        if target.exists():
            target = self._user_dir / f"{key}_copy_{int(time.time())}"
        shutil.copytree(source_dir, target)
        main = target / origin.name
        if origin.name != "main.py":
            try:
                main.rename(target / "main.py")
            except OSError:
                pass
        self.reload_plugins()
        return {"copied": str(target)}


registry = Registry()


def plugin_template(name: str = "my_source", display: str = "我的图源",
                    base_url: str = "https://example.com") -> str:
    """生成一个能跑通的最小插件模板（界面里「新建插件」用）。"""
    key = re.sub(r"[^a-z0-9_]", "_", name.strip().lower()) or "my_source"
    if not PLUGIN_KEY_RE.match(key):
        key = "my_source"
    return f'''"""呆猫漫画 · 我的图源插件

改完保存，在「图源管理」里点热重载即可生效。
"""
from dumbcat.plugins.api import Page, SourcePlugin, chapter, series


class MySource(SourcePlugin):
    key = "{key}"
    name = "{display}"
    base_url = "{base_url}"
    version = "0.1.0"
    author = "我"
    description = "用 CSS 选择器抓一个站点的示例插件"
    icon = "🐾"

    def search(self, query: str, page: int = 1) -> Page:
        html = self.ctx.http.get_text(
            f"{{self.base_url}}/search",
            params={{"q": query, "page": page}},
        )
        doc = self.ctx.parse(html)
        items = []
        for node in doc.select(".comic-item"):
            link = node.one("a")
            if link is None:
                continue
            cover = node.one("img")
            items.append(series(
                title=node.text(".title") or link.attr("title") or link.text(),
                url=self.abs(link.attr("href")),
                cover=self.abs(cover.attr("src")) if cover else "",
            ))
        return Page(items=items, page=page, has_more=bool(items))

    def chapters(self, s):
        doc = self.ctx.parse(self.ctx.http.get_text(s["url"]))
        return [
            chapter(title=node.text(), url=self.abs(node.attr("href")))
            for node in doc.select(".chapter-list a")
        ]

    def pages(self, s, c):
        doc = self.ctx.parse(self.ctx.http.get_text(c["url"]))
        return [self.abs(img.attr("data-src") or img.attr("src"))
                for img in doc.select(".comic-page img")]


PLUGIN = MySource
'''
