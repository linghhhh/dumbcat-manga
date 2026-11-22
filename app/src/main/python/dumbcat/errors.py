"""错误类型与 URL 规范化。

从旧的主模块拆出；manga_core 会重新导出这些名字，方便调用方只导入一个模块。
"""
from __future__ import annotations

import urllib.parse
from typing import Any


class ApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        status: int | None = None,
        url: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.url = url
        self.retry_after = retry_after


def normalize_image_url(value: Any, base_url: str = "") -> str:
    """Normalize CDN and protocol-relative image addresses before UI/download use."""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        return f"https:{text}"
    if text.startswith(("http://", "https://")):
        return text
    if base_url:
        # urljoin also handles `/media/...` and other root-relative CDN paths.
        return urllib.parse.urljoin(f"{base_url.rstrip('/')}/", text)
    return text


def friendly_network_error(error: BaseException, base_url: str = "") -> str:
    text = str(error).lower()
    host = urllib.parse.urlparse(base_url).netloc or base_url or "目标站点"
    reset_markers = (
        "10054",
        "connection reset",
        "forcibly closed",
        "远程主机强迫关闭",
    )
    if any(marker in text for marker in reset_markers):
        return (
            f"无法连接 {host}：远程主机在网络/TLS 连接阶段重置了连接。"
            "请先确认浏览器可访问该站点，并检查代理、镜像域名或网络线路。"
        )
    # 站点前挂了 Cloudflare 之类的 CDN：5xx 往往是对源站不可达的中转报错，
    # 不是本程序的问题。直接把一大段 JSON 甩给用户没有意义。
    infra_markers = ("cloudflare", "error 5", '"status":5', "522", "523", "524")
    if any(marker in text for marker in infra_markers):
        return (
            f"{host} 当前不可用：站点前置的 CDN 报告源站连接失败"
            "（通常是站点自身故障或维护）。请稍后重试，"
            "或先用浏览器确认该站点能否打开。"
        )
    return str(error)
