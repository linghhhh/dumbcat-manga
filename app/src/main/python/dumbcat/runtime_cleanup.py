from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

RUNTIME_DIR_NAME = ".DumbCatMangaRuntime"
LEGACY_RUNTIME_DIR_NAMES = (
    ".MangaDownloaderRuntime",
    "NuviatoonDownloaderRuntime",
)
LEGACY_RUNTIME_DIR_NAME = "NuviatoonDownloaderRuntime"
APP_CACHE_PREFIXES = (
    "manga-comix-browser-",
    "manga-chromium-probe-",
    "manga-comix-smoke-",
    "playwright_chromiumdev_profile-",
    "playwright-artifacts-",
)
RUNTIME_BROWSER_CACHE_SUFFIX = "-browser-cache"
PLAYWRIGHT_STALE_SECONDS = 30 * 60
SYSTEM_TEMP_DIR = Path(tempfile.gettempdir()).resolve()
_RUNTIME_ROOT: Path | None = None


@dataclass(frozen=True)
class CleanupReport:
    removed: tuple[Path, ...] = ()
    failed: tuple[Path, ...] = ()
    bytes_removed: int = 0

    @property
    def removed_mb(self) -> float:
        return self.bytes_removed / (1024 * 1024)


def _application_root() -> Path:
    """程序所在目录（运行时缓存放在它的 `.DumbCatMangaRuntime` 里）。

    源码运行时本模块位于 `dumbcat/` 包内，如果直接用自身的父目录，缓存就会
    混进源码包里（`.DumbCatMangaRuntime` 会出现在 dumbcat 目录下）。这里向上
    跳过所有包目录，让缓存落在项目根目录，和启动器保持一致。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    here = Path(__file__).resolve().parent
    while (here / "__init__.py").is_file() and here.parent != here:
        here = here.parent
    return here


def _is_writable_directory(path: Path) -> bool:
    probe = path / f".write-test-{os.getpid()}-{time.time_ns()}"
    try:
        path.mkdir(parents=True, exist_ok=True)
        with probe.open("wb"):
            pass
        probe.unlink()
        return True
    except OSError:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def runtime_root() -> Path:
    global _RUNTIME_ROOT
    if _RUNTIME_ROOT is not None:
        return _RUNTIME_ROOT

    override = os.getenv("MANGA_RUNTIME_DIR")
    if override:
        candidate = Path(override).expanduser()
    else:
        candidate = _application_root() / RUNTIME_DIR_NAME

    if not _is_writable_directory(candidate):
        local_app_data = os.getenv("LOCALAPPDATA")
        fallback_base = (
            Path(local_app_data)
            if local_app_data
            else Path.home() / ".local" / "share"
        )
        candidate = fallback_base / "DumbCatManga" / "runtime"
        if not _is_writable_directory(candidate):
            candidate = SYSTEM_TEMP_DIR / LEGACY_RUNTIME_DIR_NAMES[0]
            candidate.mkdir(parents=True, exist_ok=True)

    _RUNTIME_ROOT = candidate.resolve()
    return _RUNTIME_ROOT


def configure_process_temp() -> Path:
    root = runtime_root()
    os.environ["TMP"] = str(root)
    os.environ["TEMP"] = str(root)
    os.environ["TMPDIR"] = str(root)
    tempfile.tempdir = str(root)
    return root


def path_size(path: Path) -> int:
    total = 0
    try:
        if path.is_symlink():
            return path.lstat().st_size
        if path.is_file():
            return path.stat().st_size
        if not path.is_dir():
            return 0
        for child in path.rglob("*"):
            try:
                if child.is_file() and not child.is_symlink():
                    total += child.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _protected_roots(extra: Iterable[Path] = ()) -> set[Path]:
    roots = {Path.home().resolve(), SYSTEM_TEMP_DIR, _application_root().resolve()}
    anchor = Path.home().anchor
    if anchor:
        roots.add(Path(anchor).resolve())
    roots.update(Path(path).resolve() for path in extra)
    for name in ("WINDIR", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)"):
        value = os.getenv(name)
        if value:
            roots.add(Path(value).resolve())
    return roots


def _is_safe_to_remove(path: Path, protected: set[Path]) -> bool:
    resolved = path.resolve()
    if resolved in protected:
        return False
    if any(resolved in root.parents for root in protected):
        return False
    return bool(resolved.name)


def remove_tree(path: Path, protected: Iterable[Path] = ()) -> tuple[int, bool]:
    target = Path(path)
    if not target.exists() and not target.is_symlink():
        return 0, True
    guards = _protected_roots(protected)
    if not _is_safe_to_remove(target, guards):
        return 0, False

    size = path_size(target)
    for attempt in range(4):
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
            else:
                shutil.rmtree(target)
            return size, True
        except OSError:
            if attempt >= 3:
                return 0, False
            time.sleep(0.25 * (attempt + 1))
    return 0, False


def _looks_like_manga_mei(path: Path) -> bool:
    if not path.name.startswith("_MEI") or not path.is_dir():
        return False
    return (
        (path / "chromium_runtime" / "chrome.exe").is_file()
        and (path / "_tcl_data").is_dir()
    )


def _cache_name_is_safe(path: Path) -> bool:
    name = path.name
    if name in LEGACY_RUNTIME_DIR_NAMES:
        return True
    if any(name.startswith(prefix) for prefix in APP_CACHE_PREFIXES):
        if name.startswith("playwright_") or name.startswith("playwright-"):
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                return False
            return age >= PLAYWRIGHT_STALE_SECONDS
        return True
    return _looks_like_manga_mei(path)


def _cache_roots() -> list[Path]:
    roots = [SYSTEM_TEMP_DIR]
    current_temp = Path(tempfile.gettempdir()).resolve()
    if current_temp != SYSTEM_TEMP_DIR:
        roots.append(current_temp)

    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        local_root = Path(local_app_data)
        roots.extend(
            [
                local_root / "Temp",
                local_root / "DumbCatManga" / "runtime",
                local_root / "NuviatoonDownloader" / "runtime",
            ]
        )

    app_root = _application_root()
    for legacy_name in LEGACY_RUNTIME_DIR_NAMES:
        roots.append(app_root / legacy_name)
    return roots


def cache_candidates(exclude: Iterable[Path] = ()) -> list[Path]:
    excluded = {Path(path).resolve() for path in exclude}
    candidates: list[Path] = []
    seen: set[Path] = set()

    for root in _cache_roots():
        if root.name in LEGACY_RUNTIME_DIR_NAMES or root.name == "runtime":
            if root.exists():
                resolved = root.resolve()
                if resolved not in excluded and resolved not in seen:
                    seen.add(resolved)
                    candidates.append(root)
            continue

        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                resolved = child.resolve()
            except OSError:
                continue
            if resolved in excluded or resolved in seen:
                continue
            if _cache_name_is_safe(child):
                seen.add(resolved)
                candidates.append(child)

    return sorted(candidates, key=lambda item: str(item).casefold())


def cleanup_paths(
    paths: Iterable[Path],
    protected: Iterable[Path] = (),
) -> CleanupReport:
    removed: list[Path] = []
    failed: list[Path] = []
    total = 0
    guards = list(protected)
    for path in paths:
        size, ok = remove_tree(path, guards)
        if ok:
            if size:
                removed.append(Path(path))
                total += size
        else:
            failed.append(Path(path))
    return CleanupReport(
        removed=tuple(removed),
        failed=tuple(failed),
        bytes_removed=total,
    )


def cleanup_c_drive_caches(exclude: Iterable[Path] = ()) -> CleanupReport:
    excluded = [*exclude, runtime_root()]
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        excluded.append(Path(bundle_dir))
    return cleanup_paths(cache_candidates(excluded), protected=excluded)




def cleanup_runtime_cache() -> CleanupReport:
    root = runtime_root()
    try:
        children = list(root.iterdir())
    except OSError:
        children = []
    candidates = [
        child
        for child in children
        if child.name.startswith("chromium-profile-")
        or any(child.name.startswith(prefix) for prefix in APP_CACHE_PREFIXES)
        or child.name.endswith(RUNTIME_BROWSER_CACHE_SUFFIX)
    ]
    return cleanup_paths(candidates, protected=(root,))
