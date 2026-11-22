from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _candidate_paths() -> list[Path]:
    candidates: list[Path] = []
    override = os.getenv("MANGA_CHROMIUM_PATH")
    if override:
        candidates.append(Path(override).expanduser())

    if getattr(sys, "frozen", False):
        bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidates.append(bundle_root / "chromium_runtime" / "chrome.exe")

    project_root = Path(__file__).resolve().parent
    candidates.append(project_root / ".chromium" / "chrome.exe")

    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        local_root = Path(local_app_data)
        playwright_root = local_root / "ms-playwright"
        # Playwright has used both chrome-win and chrome-win64 over time.
        candidates.extend(
            sorted(
                playwright_root.glob("chromium-*/**/chrome.exe"),
                reverse=True,
            )
        )
        candidates.extend(
            [
                local_root / "Google" / "Chrome" / "Application" / "chrome.exe",
                local_root / "Chromium" / "Application" / "chrome.exe",
                local_root / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            ]
        )

    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        program_files = os.getenv(variable)
        if not program_files:
            continue
        root = Path(program_files)
        candidates.extend(
            [
                root / "Google" / "Chrome" / "Application" / "chrome.exe",
                root / "Chromium" / "Application" / "chrome.exe",
                root / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            ]
        )

    for executable in ("chrome", "chromium", "chromium-browser", "msedge"):
        found = shutil.which(executable)
        if found:
            candidates.append(Path(found))
    return candidates


def find_chromium_executable() -> Path | None:
    """Return the first usable Chromium-compatible executable on this machine."""
    seen: set[Path] = set()
    for candidate in _candidate_paths():
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved
    return None
