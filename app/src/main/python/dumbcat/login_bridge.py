"""安卓端登录桥：用 WebView 打开站点登录页，并把 Cookie 读回来。

桌面版登录靠 ``dumbcat.browser.hub`` 里的 Playwright Chromium；Chaquopy 环境里
没有 Chromium，所以手机上走 ``LoginActivity``：用户在一个真实 WebView 里登录，
点「完成」后我们通过 ``CookieManager`` 把该站点的 Cookie 取出来，交给
``sources.session`` 复用。

所有方法在非安卓环境或桥不可用时返回「不支持」，调用方据此退回桌面逻辑。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger("dumbcat.login")

_MISSING: Any = None


def _bridge() -> Any:
    global _MISSING
    if _MISSING is not None:
        return None if _MISSING is False else _MISSING
    if not os.getenv("DUMBCAT_ANDROID"):
        _MISSING = False
        return None
    try:
        from java import jclass  # type: ignore

        cls = jclass("com.dumbcat.manga.MainActivity")
        _MISSING = cls
        return cls
    except Exception as exc:  # noqa: BLE001 - 桌面版没有 java 模块，属正常情况
        log.debug("安卓登录桥不可用：%s", exc)
        _MISSING = False
        return None


def available() -> bool:
    return _bridge() is not None


def open_window(url: str) -> bool:
    """打开登录窗口；成功返回 True。"""
    cls = _bridge()
    if cls is None:
        return False
    cls.openLoginWindow(str(url))
    return True


def read_cookies(url: str, base_url: str = "") -> list[dict[str, str]]:
    """读取登录窗口里该站点的 Cookie。"""
    cls = _bridge()
    if cls is None:
        return []
    raw = cls.readLoginCookies(str(url or ""), str(base_url or ""))
    try:
        items = json.loads(str(raw or "[]"))
    except Exception:  # noqa: BLE001 - 桥返回异常格式时不至于让登录接口 500
        log.warning("登录 Cookie 解析失败：%s", raw)
        return []
    out: list[dict[str, str]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out.append({"name": name, "value": str(item.get("value") or "")})
    return out
