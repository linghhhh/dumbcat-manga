"""安卓存储权限探测（MANAGE_EXTERNAL_STORAGE / 传统写权限）。

所有 Java 桥调用都包在 try/except 里：桌面上没有 Chaquopy，导入本模块也
不能报错，调用方（下载路径解析、选目录接口）据此判断"有没有公共目录能力"。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("dumbcat.android_storage")

#: 公共目录下我们自己的文件夹名
PUBLIC_FOLDER = "呆猫漫画"

_ACTIVITY_CLASS = "com.dumbcat.manga.MainActivity"


def is_android() -> bool:
    """当前进程是否跑在安卓 App 里（android_main 会设置这个变量）。"""
    return bool(os.getenv("DUMBCAT_ANDROID"))


def _activity():
    from java import jclass  # type: ignore[import-not-found]

    return jclass(_ACTIVITY_CLASS)


def _call(name: str, default: Any = None) -> Any:
    """安全调用 MainActivity 的静态方法。"""
    if not is_android():
        return default
    try:
        method = getattr(_activity(), name)
        return method()
    except Exception as exc:  # noqa: BLE001 - Java 桥失败不能影响下载
        log.debug("调用 MainActivity.%s 失败：%s", name, exc)
        return default


def external_root() -> Path | None:
    """公共存储根目录（一般 /storage/emulated/0）。拿不到返回 None。"""
    raw = _call("externalStorageRoot", "")
    if not raw:
        return None
    try:
        return Path(str(raw))
    except (OSError, ValueError):
        return None


def default_public_dir() -> Path | None:
    """推荐给用户看的下载目录：公共根目录/呆猫漫画。"""
    root = external_root()
    if root is None:
        return None
    return root / PUBLIC_FOLDER


def permission_info() -> dict[str, Any]:
    """把 Java 侧的权限状态整理成前端能用的结构。"""
    info: dict[str, Any] = {
        "granted": False,
        "need": "none",
        "can_ask": False,
        "api": 0,
        "root": "",
        "platform": "android" if is_android() else "desktop",
        "label": "",
        "managed": False,
    }
    if not is_android():
        info["can_ask"] = False
        return info
    info["api"] = int(_call("storageApiLevel", 0) or 0)
    info["granted"] = bool(_call("hasStorageAccess", False))
    info["need"] = str(_call("storagePermissionKind", "none") or "none")
    info["can_ask"] = bool(_call("canRequestStorageAccess", False))
    root = external_root()
    if root is not None:
        info["root"] = str(root)
        info["label"] = PUBLIC_FOLDER
    info["managed"] = bool(_call("isExternalStorageManaged", False))
    return info


def request_permission() -> dict[str, Any]:
    """唤起系统权限页面；返回最新的权限状态。"""
    info = permission_info()
    if not is_android():
        info["ok"] = False
        info["detail"] = "桌面版不需要存储权限"
        return info
    if info["granted"]:
        info["ok"] = True
        return info
    ok = bool(_call("requestStorageAccess", False))
    info["ok"] = ok
    if not ok:
        info["detail"] = (
            "无法自动弹出权限设置，请到系统设置 → 应用 → 呆猫漫画 → 权限里打开存储/所有文件访问"
        )
    return info
