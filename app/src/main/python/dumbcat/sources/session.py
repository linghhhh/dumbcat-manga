"""漫源登录态：每个漫源一份 Cookie / 令牌 / 自定义请求头，落盘复用。

为什么要单独一层：

- 有的漫源用 Cookie 会话（绅士漫画、漫画柜、e-hentai…），有的用 Bearer 令牌
  （Komiic、再漫画…），有的要固定设备指纹头（User-Agent、X-Client-ID）。这些
  都属于"这个漫源的登录态"，不应该散落在各个 provider 里。
- 用户希望在**程序内**用官方账号登录，然后既能在软件里看收藏夹，也能把作品
  收藏回官方收藏夹。所以登录态必须能持久化、能被 HTTP 请求层自动带上。

登录方式统一为两条路：

1. `login_url()`：用共享 Chromium 打开站点自己的登录页，用户登完以后
   `capture_from_browser()` 把该域名的 Cookie 采集进会话（对所有站点都适用）；
2. 部分漫源还支持直接账号密码登录（provider 自己实现 `login()`）。

落盘位置：`%APPDATA%\\DumbCatManga\\sessions\\<漫源key>.json`。里面是登录凭据，
只写当前用户目录，不写程序目录，也不进日志。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import dumbcat.app_config as cfg

#: 会话文件里不写进日志的敏感字段名
SENSITIVE_HINTS = ("cookie", "token", "password", "passwd", "secret", "auth", "ig")


def sessions_dir() -> Path:
    return Path(cfg.APP_CONFIG_DIR) / "sessions"


def _session_path(key: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(key or ""))
    return sessions_dir() / f"{safe or 'unknown'}.json"


def _write_atomically(target: Path, payload: dict[str, Any]) -> None:
    tmp = target.with_name(target.name + ".tmp")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def parse_set_cookie(raw: str) -> tuple[str, str] | None:
    """从一条 Set-Cookie 里取出 (name, value)；删除型 Cookie 返回 (name, "")。

    注意：站点的大小写不统一（`Expires=` / `expires=` 都见过），所以属性名一律
    按忽略大小写解析；整个函数不允许抛异常——它跑在每一个响应上，解析失败最多
    是这条 Cookie 不收，绝不能让一次请求失败。
    """
    text = str(raw or "").strip()
    if not text or "=" not in text:
        return None
    head = text.split(";", 1)[0]
    name, _, value = head.partition("=")
    name = name.strip()
    if not name:
        return None
    expired = False
    try:
        compact = text.lower().replace(" ", "")
        if "max-age=0" in compact or "max-age=-" in compact:
            expired = True
        else:
            match = re.search(r"expires=([^;]+)", text, re.IGNORECASE)
            if match:
                expired = _is_expired(match.group(1).strip())
    except Exception:  # noqa: BLE001 - 解析属性失败不影响 Cookie 本身
        expired = False
    return name, ("" if expired else value.strip())


def _is_expired(stamp: str) -> bool:
    """判断 Cookie 的 Expires 是否已经过期。

    站点会通过下发一个"过去的时间"来删除 Cookie（退出登录就是这么做），所以
    这里必须真的判过期——判错会让退出登录失效、旧凭据一直被当成有效。
    用标准库的 RFC 日期解析（能处理 RFC1123 / RFC850 / asctime 三种写法），
    失败时退回 strptime，再失败就当作未过期（保守，不会误删会话）。
    """
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(stamp)
    except (TypeError, ValueError):
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed <= datetime.now(timezone.utc)
    for fmt in ("%a, %d-%b-%Y %H:%M:%S GMT", "%a, %d %b %Y %H:%M:%S GMT"):
        try:
            return time.mktime(time.strptime(stamp, fmt)) <= time.time()
        except (ValueError, OverflowError):
            continue
    return False


def safe_parse_set_cookie(raw: str) -> tuple[str, str] | None:
    try:
        return parse_set_cookie(raw)
    except Exception:  # noqa: BLE001
        return None


def cookie_header_for(cookies: dict[str, str]) -> str:
    return "; ".join(
        f"{name}={value}" for name, value in cookies.items() if name and value
    )


@dataclass
class SourceSession:
    """一个漫源的登录态。所有字段都可 JSON 序列化。"""

    key: str
    cookies: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    #: 最近一次登录/采集时间（仅用于界面提示）
    updated_at: float = 0.0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ---------- 读写 ----------
    @property
    def has_credentials(self) -> bool:
        """是否有 Cookie / 令牌。

        注意：匿名访客 Cookie 也算凭据，所以**不能**用它判断"已登录"——
        见 `confirmed`。界面要的是后者。
        """
        return bool(self.cookies or self.headers.get("Authorization"))

    @property
    def confirmed(self) -> bool:
        """用户是否已经明确完成过登录（点过「完成登录」或账号密码登录成功）。

        站点给的匿名 Cookie 很常见（访客 sid、地区标记），如果拿 Cookie 数量
        当登录依据，界面一开局就显示"已登录"，用户会以为收藏能用。所以登录
        状态必须由"用户确认过"或"漫源真的校验通过"来判定。
        """
        return bool(self.data.get("login_confirmed"))

    def mark_login_confirmed(self, value: bool = True) -> None:
        if value:
            self.data["login_confirmed"] = True
            self.data["login_confirmed_at"] = time.time()
        else:
            self.data.pop("login_confirmed", None)
            self.data.pop("login_confirmed_at", None)

    @property
    def cookie_header(self) -> str:
        with self._lock:
            return cookie_header_for(self.cookies)

    def merge_headers(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """把登录态并进一组请求头（Cookie 与自定义头都由会话决定）。"""
        merged = dict(base or {})
        with self._lock:
            for name, value in self.headers.items():
                if name and value:
                    merged[name] = value
            cookie = cookie_header_for(self.cookies)
            if cookie:
                existing = merged.get("Cookie")
                merged["Cookie"] = f"{existing}; {cookie}" if existing else cookie
        return merged

    # ---------- 更新 ----------
    def update_cookies(self, pairs: Iterable[tuple[str, str]]) -> None:
        changed = False
        with self._lock:
            for name, value in pairs:
                if not name:
                    continue
                if value:
                    if self.cookies.get(name) != value:
                        self.cookies[name] = value
                        changed = True
                elif name in self.cookies:
                    # 删除型 Cookie：站点明确要求清掉
                    self.cookies.pop(name, None)
                    changed = True
        if changed:
            self.updated_at = time.time()

    def absorb_set_cookie(self, values: Iterable[str]) -> None:
        pairs = []
        for raw in values or ():
            parsed = safe_parse_set_cookie(raw)
            if parsed:
                pairs.append(parsed)
        if pairs:
            self.update_cookies(pairs)

    def absorb_browser_cookies(self, items: Iterable[dict[str, Any]]) -> int:
        """采集共享浏览器里的 Cookie（登录后调用）。返回采集条数。"""
        pairs: list[tuple[str, str]] = []
        for item in items or ():
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "")
            if name:
                pairs.append((name, value))
        if pairs:
            self.update_cookies(pairs)
        return len(pairs)

    def set_headers(self, headers: dict[str, str]) -> None:
        with self._lock:
            for name, value in (headers or {}).items():
                if value:
                    self.headers[str(name)] = str(value)
                else:
                    self.headers.pop(str(name), None)
        self.updated_at = time.time()

    def clear(self) -> None:
        with self._lock:
            self.cookies.clear()
            self.headers.clear()
            self.data.clear()
        self.updated_at = time.time()
        try:
            _session_path(self.key).unlink(missing_ok=True)
        except OSError:
            pass

    # ---------- 持久化 ----------
    def to_payload(self) -> dict[str, Any]:
        with self._lock:
            return {
                "key": self.key,
                "cookies": dict(self.cookies),
                "headers": dict(self.headers),
                "data": dict(self.data),
                "updated_at": self.updated_at,
            }

    def save(self) -> None:
        try:
            _write_atomically(_session_path(self.key), self.to_payload())
        except OSError:
            # 登录态写不进去不该让功能不可用，只是下次要重新登录
            pass

    def load(self) -> "SourceSession":
        try:
            payload = json.loads(_session_path(self.key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self
        if not isinstance(payload, dict):
            return self
        with self._lock:
            cookies = payload.get("cookies")
            if isinstance(cookies, dict):
                self.cookies = {
                    str(k): str(v) for k, v in cookies.items() if v
                }
            headers = payload.get("headers")
            if isinstance(headers, dict):
                self.headers = {
                    str(k): str(v) for k, v in headers.items() if v
                }
            data = payload.get("data")
            if isinstance(data, dict):
                self.data = dict(data)
            if isinstance(payload.get("updated_at"), (int, float)):
                self.updated_at = float(payload["updated_at"])
        return self


_SESSIONS: dict[str, SourceSession] = {}
_SESSIONS_LOCK = threading.RLock()


def session_for(key: str) -> SourceSession:
    """取得（并首次加载）某个漫源的登录态单例。"""
    name = str(key or "")
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(name)
        if session is None:
            session = SourceSession(name).load()
            _SESSIONS[name] = session
        return session


def clobber_session(key: str) -> SourceSession:
    """测试/退出登录用：清掉内存缓存并返回一个空会话。"""
    name = str(key or "")
    with _SESSIONS_LOCK:
        _SESSIONS.pop(name, None)
    return session_for(name)


def describe_session(key: str) -> str:
    """给界面用的一句话状态（不含凭据本身）。"""
    session = session_for(key)
    if not session.confirmed:
        if session.has_credentials:
            return "有访客 Cookie · 未登录账号"
        return "未登录"
    count = len(session.cookies)
    if count:
        return f"已登录（{count} 项 Cookie）"
    return "已登录（令牌）"
