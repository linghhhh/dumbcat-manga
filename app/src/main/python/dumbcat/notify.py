"""后台下载的通知桥。

安卓侧由 ``DownloadService`` 挂一个前台服务，这里只负责把「开始 / 进度 / 结束」
转给 Java 静态方法；桌面版和其它环境里所有调用都是空操作。
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("dumbcat.notify")

_JAVA: object | None = None
_MISSING = False


def _bridge() -> object | None:
    """拿到 MainActivity 的 Java 类；桌面版 / 导入失败返回 None。"""
    global _JAVA, _MISSING
    if _JAVA is not None:
        return _JAVA
    if _MISSING:
        return None
    if not os.getenv("DUMBCAT_ANDROID"):
        _MISSING = True
        return None
    try:
        from java import jclass  # type: ignore[import-not-found]

        _JAVA = jclass("com.dumbcat.manga.MainActivity")
    except Exception as exc:  # noqa: BLE001 - Chaquopy 之外必然失败
        log.debug("通知桥不可用：%s", exc)
        _MISSING = True
        return None
    return _JAVA


def start() -> None:
    bridge = _bridge()
    if bridge is None:
        return
    try:
        bridge.startDownloadService()
    except Exception as exc:  # noqa: BLE001
        log.debug("启动后台下载服务失败：%s", exc)


def stop() -> None:
    bridge = _bridge()
    if bridge is None:
        return
    try:
        bridge.stopDownloadService()
    except Exception as exc:  # noqa: BLE001
        log.debug("停止后台下载服务失败：%s", exc)


def progress(done: int, total: int, text: str = "") -> None:
    bridge = _bridge()
    if bridge is None:
        return
    try:
        bridge.updateDownloadNotification(int(done), int(total), str(text or ""))
    except Exception as exc:  # noqa: BLE001
        log.debug("更新下载通知失败：%s", exc)
