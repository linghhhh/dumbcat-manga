"""网络访问层：通用 JSON/字节客户端与禁漫天堂签名客户端。

两个客户端都自带退避重试、`Retry-After` 支持、`.part` 原子写入与取消语义，
供各漫源 provider 复用。这里不包含任何漫源业务逻辑。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from dumbcat.app_config import (
    API_HEADERS,
    COMIC18_SITE_BASE,
    IMAGE_HEADERS,
    JM_AES_SEEDS,
    JM_API_BASE,
    JM_IMAGE_BASE,
    JM_VERSION,
    RETRYABLE_STATUS,
    USER_AGENT,
)
from dumbcat.errors import ApiError, friendly_network_error, normalize_image_url
from dumbcat.naming import parse_retry_after
from dumbcat.sources.session import SourceSession, session_for



class ApiClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        retries: int = 2,
        min_request_interval: float = 0.08,
        headers: dict[str, str] | None = None,
        session_key: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.min_request_interval = min_request_interval
        self.headers = dict(headers or API_HEADERS)
        self.ssl_context = ssl.create_default_context()
        self._request_lock = threading.Lock()
        self._last_request_at = 0.0
        #: 登录态（Cookie / 令牌 / 设备指纹头）。没给 key 时用匿名空会话。
        self.session: SourceSession = session_for(session_key or base_url)

    def emit(self, *args: Any, **kwargs: Any) -> None:
        """兼容旧下载管线：ApiClient 不持有界面回调，这里静默吞掉提示。"""
        return None

    def _wait_for_request_slot(self, cancel_event: threading.Event | None = None) -> None:
        while True:
            with self._request_lock:
                now = time.monotonic()
                wait = self.min_request_interval - (now - self._last_request_at)
                if wait <= 0:
                    self._last_request_at = now
                    return
            if cancel_event and cancel_event.is_set():
                return
            time.sleep(min(wait, 0.2))

    @staticmethod
    def _can_retry(error: ApiError) -> bool:
        return (
            "操作已停止" not in str(error)
            and (error.status is None or error.status in RETRYABLE_STATUS)
        )

    @staticmethod
    def _retry_delay(error: ApiError, attempt: int) -> float:
        if error.retry_after is not None:
            return min(30.0, max(0.0, error.retry_after))
        return min(8.0, 0.7 * (2**attempt))

    def _sleep_before_retry(
        self,
        error: ApiError,
        attempt: int,
        cancel_event: threading.Event | None,
    ) -> None:
        deadline = time.monotonic() + self._retry_delay(error, attempt)
        while True:
            if cancel_event and cancel_event.is_set():
                raise ApiError("操作已停止", url=error.url)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.2, remaining))

    def _request_bytes_once(
        self,
        path_or_url: str,
        headers: dict[str, str] | None = None,
        cancel_event: threading.Event | None = None,
        method: str = "GET",
        data: bytes | None = None,
    ) -> tuple[bytes, str, str | None]:
        url = (
            path_or_url
            if path_or_url.startswith(("http://", "https://"))
            else f"{self.base_url}/{path_or_url.lstrip('/')}"
        )
        self._wait_for_request_slot(cancel_event)
        if cancel_event and cancel_event.is_set():
            raise ApiError("操作已停止", url=url)
        request_headers = dict(self.headers)
        if headers:
            request_headers.update(headers)
        # 登录态最后合并：Cookie / Authorization 由会话决定，调用方不用关心
        request_headers = self.session.merge_headers(request_headers)
        request = urllib.request.Request(
            url, headers=request_headers, data=data, method=method.upper()
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
                context=self.ssl_context,
            ) as response:
                self._absorb_response_cookies(response)
                chunks: list[bytes] = []
                while True:
                    if cancel_event and cancel_event.is_set():
                        raise ApiError("操作已停止", url=url)
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return (
                    b"".join(chunks),
                    response.geturl(),
                    response.headers.get_content_charset(),
                )
        except urllib.error.HTTPError as exc:
            # 失败响应也可能带着"清登录态"的 Set-Cookie，同样要吸收
            try:
                self.session.absorb_set_cookie(exc.headers.get_all("Set-Cookie") or [])
            except Exception:  # noqa: BLE001 - 解析失败不影响报错
                pass
            retry_after = parse_retry_after(exc.headers.get("Retry-After"))
            raise ApiError(
                self._error_message(exc),
                exc.code,
                url,
                retry_after,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiError(
                f"网络请求失败：{friendly_network_error(exc, self.base_url)}",
                url=url,
            ) from exc

    def _absorb_response_cookies(self, response: Any) -> None:
        """把站点下发的 Set-Cookie 收进会话（登录/续期都靠它）。

        这里绝不允许抛异常：它跑在每个响应上，解析出错最多是这条 Cookie 不收，
        不能把一次正常请求变成失败（站点的大小写与格式五花八门）。
        """
        try:
            values = response.headers.get_all("Set-Cookie") or []
        except Exception:  # noqa: BLE001
            return
        if not values:
            return
        try:
            self.session.absorb_set_cookie(values)
        except Exception:  # noqa: BLE001
            return
        try:
            if any(
                key in " ".join(values).lower()
                for key in ("session", "token", "auth", "sid")
            ):
                self.session.save()
        except Exception:  # noqa: BLE001
            pass

    def request_bytes(
        self,
        path_or_url: str,
        headers: dict[str, str] | None = None,
        cancel_event: threading.Event | None = None,
        method: str = "GET",
        data: bytes | None = None,
    ) -> tuple[bytes, str, str | None]:
        last_error: ApiError | None = None
        for attempt in range(self.retries + 1):
            try:
                return self._request_bytes_once(
                    path_or_url,
                    headers,
                    cancel_event,
                    method,
                    data,
                )
            except ApiError as exc:
                last_error = exc
                if not self._can_retry(exc) or attempt >= self.retries:
                    raise
                self._sleep_before_retry(exc, attempt, cancel_event)
        raise last_error or ApiError("未知网络错误")

    def post_form(
        self,
        path_or_url: str,
        fields: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[bytes, str, str | None]:
        """表单 POST（很多站点的登录接口是 form-urlencoded）。"""
        body = urllib.parse.urlencode(
            {key: value for key, value in (fields or {}).items() if value is not None}
        ).encode("utf-8")
        merged = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
        }
        merged.update(headers or {})
        return self.request_bytes(
            path_or_url, merged, cancel_event, method="POST", data=body
        )

    def post_json(
        self,
        path_or_url: str,
        payload: Any = None,
        headers: dict[str, str] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[bytes, str, str | None]:
        """JSON POST（Twirp / 现代 API 都是这种）。"""
        body = json.dumps(payload if payload is not None else {}).encode("utf-8")
        merged = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
        }
        merged.update(headers or {})
        return self.request_bytes(
            path_or_url, merged, cancel_event, method="POST", data=body
        )

    def request_text(
        self,
        path_or_url: str,
        headers: dict[str, str] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[str, str]:
        raw, final_url, charset = self.request_bytes(
            path_or_url,
            headers,
            cancel_event,
        )
        if not charset:
            head = raw[:4096].decode("ascii", errors="ignore")
            match = re.search(r"charset\s*=\s*['\"]?([\w.-]+)", head, re.I)
            charset = match.group(1) if match else "utf-8"
        try:
            return raw.decode(charset, errors="replace"), final_url
        except LookupError:
            return raw.decode("utf-8", errors="replace"), final_url

    @staticmethod
    def _error_message(exc: urllib.error.HTTPError) -> str:
        try:
            body = exc.read(4096).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        if body:
            try:
                payload = json.loads(body)
                if isinstance(payload, dict):
                    message = payload.get("message") or payload.get("error")
                    if message:
                        return str(message)
            except json.JSONDecodeError:
                pass
            body = " ".join(body.split())
            if body:
                return body[:300]
        return f"HTTP {exc.code}"

    def _request_json_once(
        self,
        path: str,
        query: dict[str, Any] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Any:
        path = "/" + path.lstrip("/")
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        raw, final_url, _charset = self._request_bytes_once(
            url,
            cancel_event=cancel_event,
        )
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("接口返回了无法解析的数据", url=final_url) from exc


    def _request_json(
        self,
        path: str,
        query: dict[str, Any] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Any:
        last_error: ApiError | None = None
        for attempt in range(self.retries + 1):
            try:
                return self._request_json_once(path, query, cancel_event)
            except ApiError as exc:
                last_error = exc
                if not self._can_retry(exc) or attempt >= self.retries:
                    raise
                self._sleep_before_retry(exc, attempt, cancel_event)
        raise last_error or ApiError("未知网络错误")


    def request_json(
        self,
        path: str,
        query: dict[str, Any] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Any:
        return self._request_json(path, query, cancel_event)

    def search_series(
        self,
        query: str,
        series_type: str = "manga",
        per_page: int = 50,
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "q": query.strip(),
            "page": 1,
            "per_page": max(1, min(per_page, 100)),
            "sort": "popular",
            "dir": "desc",
        }
        if series_type and series_type != "all":
            params["type"] = series_type
        payload = self._request_json("/series", params, cancel_event)
        if isinstance(payload, dict):
            data = payload.get("data", [])
        else:
            data = payload
        return [item for item in data if isinstance(item, dict)]

    def get_series(
        self,
        slug: str,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        payload = self._request_json(
            f"/series/{urllib.parse.quote(slug, safe='')}",
            cancel_event=cancel_event,
        )
        if not isinstance(payload, dict):
            raise ApiError("作品详情接口返回格式异常")
        return payload

    def get_chapters(
        self,
        slug: str,
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        payload = self._request_json(
            f"/series/{urllib.parse.quote(slug, safe='')}/chapters",
            cancel_event=cancel_event,
        )
        if isinstance(payload, dict):
            data = payload.get("data", [])
        else:
            data = payload
        if not isinstance(data, list):
            raise ApiError("章节列表接口返回格式异常")
        return [item for item in data if isinstance(item, dict)]

    def get_chapter_pages(
        self,
        chapter_id: str,
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        payload = self._request_json(
            f"/chapters/{urllib.parse.quote(chapter_id, safe='')}/pages",
            cancel_event=cancel_event,
        )
        if isinstance(payload, dict):
            data = payload.get("data", payload.get("pages", []))
        else:
            data = payload
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def download_file(
        self,
        url: str,
        destination: Path,
        cancel_event: threading.Event | None = None,
        referer: str | None = None,
        retries: int | None = None,
    ) -> str:
        destination = Path(destination)
        if destination.exists() and destination.stat().st_size > 0:
            return "skipped"

        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(f"{destination.name}.part")
        attempts = self.retries + 1 if retries is None else max(1, retries + 1)
        last_error: ApiError | None = None
        verification_warned = False

        def wait_for_retry(error: ApiError, attempt: int) -> bool:
            """Wait with the shared backoff policy, preserving cancellation as a status."""
            try:
                self._sleep_before_retry(error, attempt, cancel_event)
            except ApiError:
                if cancel_event and cancel_event.is_set():
                    return False
                raise
            return True

        for attempt in range(attempts):
            if cancel_event and cancel_event.is_set():
                return "cancelled"
            headers = dict(IMAGE_HEADERS)
            if referer:
                headers["Referer"] = referer
            host = urllib.parse.urlparse(url).netloc
            if host:
                headers["Host"] = host
            # 很多图床要带登录 Cookie / 防盗链头，否则 403
            headers = self.session.merge_headers(headers)
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                if partial.exists():
                    partial.unlink()
                self._wait_for_request_slot(cancel_event)
                if cancel_event and cancel_event.is_set():
                    return "cancelled"
                with urllib.request.urlopen(
                    request,
                    timeout=self.timeout,
                    context=self.ssl_context,
                ) as response:
                    expected_length = response.headers.get("Content-Length")
                    written = 0
                    with partial.open("wb") as output:
                        while True:
                            if cancel_event and cancel_event.is_set():
                                raise ApiError("操作已停止", url=url)
                            chunk = response.read(256 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                            written += len(chunk)
                expected: int | None = None
                if expected_length:
                    try:
                        expected = int(expected_length)
                    except (TypeError, ValueError):
                        # 头部存在但不可解析：属于站点协议异常，重试不会改善，
                        # 因此不抛异常（否则会被重试循环吞掉并反复重下），
                        # 只按“无法校验”提示一次。
                        expected = None
                    if expected is not None and written != expected:
                        raise ApiError(
                            f"图片下载不完整：期望 {expected} 字节，实际 {written} 字节",
                            url=url,
                        )
                if expected is None and not verification_warned:
                    # 没有（或无法解析）Content-Length 时无法校验完整性，
                    # 该文件可能被截断却仍被当作完成，因此明确提示一次。
                    verification_warned = True
                    self.emit(
                        "log",
                        message=f"图片响应缺少可用的 Content-Length，无法校验完整性：{url}",
                        level="warning",
                        url=url,
                    )
                if cancel_event and cancel_event.is_set():
                    raise ApiError("操作已停止", url=url)
                os.replace(partial, destination)
                return "downloaded"
            except urllib.error.HTTPError as exc:
                retry_after = parse_retry_after(exc.headers.get("Retry-After"))
                last_error = ApiError(
                    self._error_message(exc),
                    exc.code,
                    url,
                    retry_after,
                )
                if exc.code not in RETRYABLE_STATUS or attempt >= attempts - 1:
                    break
                if not wait_for_retry(last_error, attempt):
                    return "cancelled"
            except ApiError as exc:
                last_error = exc
                if "操作已停止" in str(exc):
                    break
                if attempt >= attempts - 1:
                    break
                if not wait_for_retry(exc, attempt):
                    return "cancelled"
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = ApiError(
                    f"网络请求失败：{friendly_network_error(exc, self.base_url)}",
                    url=url,
                )
                if attempt >= attempts - 1:
                    break
                if not wait_for_retry(last_error, attempt):
                    return "cancelled"

        if partial.exists():
            try:
                partial.unlink()
            except OSError:
                pass
        if cancel_event and cancel_event.is_set():
            return "cancelled"
        raise last_error or ApiError("图片下载失败", url=url)


class JmApiClient:
    """Small client for the public JM API used by Breeze's JM plugin."""

    def __init__(
        self,
        base_url: str = JM_API_BASE,
        image_base: str = JM_IMAGE_BASE,
        session_key: str = "18comic",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.image_base = image_base.rstrip("/")
        self.timeout = 30.0
        #: 登录态（jwttoken → Authorization、AVS 等 Cookie）
        self.session: SourceSession = session_for(session_key)

    @staticmethod
    def _decode_data(value: str, timestamp: str) -> Any:
        compact = re.sub(r"\s+", "", str(value or "")).replace("-", "+").replace("_", "/")
        if not compact:
            return value
        compact += "=" * (-len(compact) % 4)
        try:
            encrypted = base64.b64decode(compact, validate=True)
        except (ValueError, base64.binascii.Error):
            return value
        for seed in JM_AES_SEEDS:
            digest = hashlib.md5(f"{timestamp}{seed}".encode("utf-8")).digest()
            keys = (digest.hex().encode("ascii"), digest)
            for key in keys:
                try:
                    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

                    decryptor = Cipher(
                        algorithms.AES(key),
                        modes.ECB(),
                    ).decryptor()
                    plain = decryptor.update(encrypted) + decryptor.finalize()
                    padding = plain[-1]
                    if not padding or padding > 16 or plain[-padding:] != bytes([padding]) * padding:
                        continue
                    decoded = plain[:-padding].decode("utf-8")
                    return json.loads(decoded)
                except Exception:
                    continue
        return value

    def request_json(
        self,
        path: str,
        query: dict[str, Any] | None = None,
        cancel_event: threading.Event | None = None,
        *,
        method: str = "GET",
        form: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> Any:
        """调用 JM 应用接口。

        - 每个请求都带 `token` / `tokenparam`（md5(ts+密钥)）；
        - `auth=True` 时带上登录后的 `Authorization: Bearer <jwttoken>` 与 Cookie；
          登录接口本身必须 **不带** Authorization（否则站点直接判失败），所以
          `login()` 会传 `auth=False`；
        - 响应 `data` 是 AES-ECB 加密的 base64，密钥依赖**本次请求的 ts**。
        """
        if cancel_event and cancel_event.is_set():
            raise ApiError("操作已停止")
        # 站点按秒校验时间戳（毫秒会让 token 失效），解密也必须用同一个 ts
        timestamp = str(int(time.time()))
        token = hashlib.md5(f"{timestamp}{JM_VERSION}".encode("utf-8")).hexdigest()
        url = path if path.startswith(("http://", "https://")) else f"{self.base_url}/{path.lstrip('/')}"
        body: bytes | None = None
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": f"{COMIC18_SITE_BASE}/",
            "Origin": COMIC18_SITE_BASE,
            "token": token,
            "tokenparam": f"{timestamp},{JM_VERSION}",
        }
        if method.upper() == "GET":
            if query:
                url = f"{url}?{urllib.parse.urlencode(query)}"
        else:
            fields = {key: value for key, value in (form or query or {}).items() if value is not None}
            body = urllib.parse.urlencode(fields).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if auth:
            headers = self.session.merge_headers(headers)
        request = urllib.request.Request(
            url, headers=headers, data=body, method=method.upper()
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                status = int(response.status or 200)
                self._absorb_cookies(response)
        except urllib.error.HTTPError as exc:
            # 401 常常带着加密的 auth_fail 说明，读出来比"HTTP 401"有用得多
            detail = ""
            try:
                detail = self._error_detail(exc.read(), timestamp)
            except Exception:  # noqa: BLE001
                detail = ""
            raise ApiError(
                detail or f"JM API 请求失败：HTTP {exc.code}", exc.code, url
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiError(f"JM API 网络请求失败：{exc}", url=url) from exc
        if status < 200 or status >= 300:
            raise ApiError(f"JM API 请求失败：HTTP {status}", status, url)
        # 站点用 gzip 时 urllib 不会自动解压
        if raw[:2] == b"\x1f\x8b":
            import gzip

            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("JM API 返回格式异常", url=url) from exc
        if isinstance(payload, dict) and isinstance(payload.get("data"), str):
            decoded = self._decode_data(payload["data"], timestamp)
            if decoded is not payload["data"]:
                return decoded
        if isinstance(payload, dict) and int(payload.get("code") or 200) not in {0, 200}:
            raise ApiError(
                str(payload.get("errorMsg") or payload.get("msg") or "JM API 请求失败"),
                int(payload.get("code") or 0) or None,
                url,
            )
        return payload

    def _absorb_cookies(self, response: Any) -> None:
        try:
            values = response.headers.get_all("Set-Cookie") or []
        except Exception:  # noqa: BLE001
            return
        if not values:
            return
        try:
            self.session.absorb_set_cookie(values)
        except Exception:  # noqa: BLE001
            pass

    def _error_detail(self, raw: bytes, timestamp: str) -> str:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ""
        if not isinstance(payload, dict):
            return ""
        message = str(payload.get("errorMsg") or payload.get("msg") or "").strip()
        data = payload.get("data")
        if isinstance(data, str):
            decoded = self._decode_data(data, timestamp)
            if isinstance(decoded, dict) and decoded.get("type") == "auth_fail":
                return message or "请先登录禁漫账号"
        return message

    def download_file(
        self,
        url: str,
        destination: Path,
        referer: str = "",
        cancel_event: threading.Event | None = None,
    ) -> str:
        if destination.exists() and destination.stat().st_size > 0:
            return "skipped"
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(f"{destination.name}.part")
        host = urllib.parse.urlparse(url).netloc
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Referer": referer or f"{COMIC18_SITE_BASE}/",
                # Do not force Host for CDN aliases: urllib and TLS must use
                # the actual URL host, while Referer carries the site context.
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                with partial.open("wb") as output:
                    while True:
                        if cancel_event and cancel_event.is_set():
                            # 抛异常而不是直接 return，才能走到下面的清理分支，
                            # 否则会残留 .part 半成品。
                            raise ApiError("操作已停止", url=url)
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
            os.replace(partial, destination)
            return "downloaded"
        except Exception as exc:
            try:
                partial.unlink()
            except OSError:
                pass
            if cancel_event and cancel_event.is_set():
                return "cancelled"
            raise ApiError(f"JM 图片下载失败：{exc}", url=url) from exc


