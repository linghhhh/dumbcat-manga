"""把「用户想下到哪」解析成一个真正可写的目录。

历史上这里只有一行 ``Path(output_dir or settings.output_dir)``：目录失效时要么
在 Android 私有目录里下载（文件管理器根本看不到），要么把原始 OSError 抛回
前端。现在统一走这里：显式指定的目录只做校验、失败就明确报错；没指定时按
候选顺序回落到第一个真能写进去的目录，并把"为什么换了目录"写进任务日志。
"""
from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dumbcat.android_storage import (
    PUBLIC_FOLDER,
    default_public_dir,
    is_android,
    permission_info,
)

log = logging.getLogger("dumbcat.download_paths")

#: 应用私有默认目录（android_main 启动时写进环境变量）
_ENV_APP_OUTPUT = "DUMBCAT_APP_OUTPUT"
#: 旧版本用的兜底变量
_ENV_OUTPUT = "DUMBCAT_OUTPUT"


class DownloadPathError(RuntimeError):
    """下载目录不可用（消息直接给用户看）。"""


@dataclass
class ResolvedOutput:
    path: Path
    #: requested | settings | android_public | android_app | desktop_default
    source: str = "settings"
    #: 落到用户可见的说明（换了目录时非空）
    note: str = ""
    #: 是否发生了回落
    fallback: bool = False


def probe_writable(path: Path) -> tuple[bool, str]:
    """真正建目录并写一个临时文件，确认能落盘。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        return False, f"没有写入权限（{_short(exc)}）"
    except FileExistsError as exc:
        return False, f"同名文件已存在（{_short(exc)}）"
    except OSError as exc:
        return False, f"无法创建目录（{_short(exc)}）"
    if not path.is_dir():
        return False, "目标不是文件夹"
    try:
        fd, name = tempfile.mkstemp(prefix=".dumbcat-", suffix=".tmp", dir=str(path))
        os.close(fd)
        os.unlink(name)
    except PermissionError as exc:
        return False, f"目录不能写入（{_short(exc)}）"
    except OSError as exc:
        return False, f"目录不可写（{_short(exc)}）"
    return True, ""


def _short(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _as_path(raw: Any) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return Path(text).expanduser()
    except (OSError, ValueError):
        return None


def _settings_target(settings: Any) -> Path | None:
    base = _as_path(getattr(settings, "output_dir", "") or "")
    if base is None:
        return None
    subdir = str(getattr(settings, "output_subdir", "") or "").strip().strip("/\\")
    if subdir and ".." not in subdir:
        base = base / subdir
    return base


def app_private_dir() -> Path | None:
    """应用私有下载目录（Android/data/.../files/呆猫漫画）。"""
    for key in (_ENV_APP_OUTPUT, _ENV_OUTPUT):
        path = _as_path(os.getenv(key) or "")
        if path is not None:
            return path
    return None


def permission() -> dict[str, Any]:
    return permission_info()


def public_dir_if_granted() -> Path | None:
    if not is_android():
        return None
    if not permission_info().get("granted"):
        return None
    return default_public_dir()


def resolve_output_dir(requested: str | None, settings: Any) -> ResolvedOutput:
    """解析下载根目录；显式目录不可写时抛 :class:`DownloadPathError`。"""
    wanted = (requested or "").strip()
    if wanted:
        path = _as_path(wanted)
        if path is None:
            raise DownloadPathError(f"下载目录无效：{wanted}")
        ok, reason = probe_writable(path)
        if not ok:
            raise DownloadPathError(f"下载目录不可用：{path}（{reason}）")
        return ResolvedOutput(path, "requested")

    private = app_private_dir()
    public = public_dir_if_granted()
    settings_path = _settings_target(settings)

    candidates: list[tuple[Path, str]] = []
    seen: set[str] = set()

    def add(path: Path | None, source: str) -> None:
        if path is None:
            return
        key = str(path)
        if key in seen:
            return
        seen.add(key)
        candidates.append((path, source))

    # 已经授权「所有文件访问」但设置里还是私有目录时，优先落到公共目录，
    # 这样用户下载完在文件管理器里就能看见。
    prefer_public = bool(public) and private is not None and settings_path == private
    if prefer_public:
        add(public, "android_public")
        add(settings_path, "settings")
        add(private, "android_app")
    else:
        add(settings_path, "settings")
        add(public, "android_public")
        add(private, "android_app")

    if not candidates:
        fallback = Path(tempfile.gettempdir()) / PUBLIC_FOLDER
        add(fallback, "desktop_default")
    if not candidates:
        raise DownloadPathError("没有可写的下载目录：无法确定保存位置")

    reasons: list[str] = []
    for index, (path, source) in enumerate(candidates):
        ok, reason = probe_writable(path)
        if ok:
            note = ""
            if reasons:
                note = (
                    f"原下载目录不可用（{'；'.join(reasons)}），"
                    f"已改用 {path}"
                )
                log.warning("下载目录回落：%s", note)
            return ResolvedOutput(path, source, note, fallback=bool(reasons))
        reasons.append(f"{path}：{reason}")

    raise DownloadPathError("没有可写的下载目录：" + "；".join(reasons))


def in_public_storage(path: Path) -> bool:
    """path 是否在公共存储里（用来限制目录浏览范围）。"""
    from dumbcat.android_storage import external_root

    root = external_root()
    if root is None:
        return False
    try:
        target = path.resolve()
        base = root.resolve()
    except (OSError, ValueError):
        return False
    return target == base or base in target.parents


def label_of(path: Path) -> str:
    name = path.name or str(path)
    return name
