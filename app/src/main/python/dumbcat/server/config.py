"""呆猫漫画 Web 版的服务端配置。

配置单独存一份 ``web.json``（与旧 Qt 版的 ``config.json`` 并列），避免两套
界面互相覆盖字段；第一次启动时会从旧配置里继承下载目录，老用户不用重选。

所有路径都是字符串（JSON 友好），需要 ``Path`` 的地方自行转换。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from dumbcat.app_config import APP_CONFIG_DIR, CONFIG_PATH, DEFAULT_OUTPUT

#: Web 版配置路径
WEB_CONFIG_PATH = APP_CONFIG_DIR / "web.json"

#: 图源开关的覆盖层：``{key: bool}``。
#:
#: 图源清单（appdata/json/*.json）里的 ``enabled`` 只是出厂默认值，而且升级时
#: 会被重新解压覆盖。用户在图源管理里拨的开关单独存这里，加载图源时以这里的
#: 值优先——这样关掉的内置图源仍然能被列出来重新打开，升级也不会丢开关。
SOURCES_ENABLED_PATH = APP_CONFIG_DIR / "sources-enabled.json"

#: 项目根目录（本文件在 dumbcat/server/ 下）
PROJECT_ROOT = Path(os.getenv("DUMBCAT_ROOT") or Path(__file__).resolve().parent.parent.parent)

#: 用户插件目录：放单个 .py 或一个带 plugin.json 的文件夹
USER_PLUGIN_DIR = PROJECT_ROOT / "用户插件"

#: 内置示例插件目录（随包发布，界面里可一键复制成自己的模板）
BUILTIN_PLUGIN_DIR = PROJECT_ROOT / "plugins_src"

#: 运行时目录（缓存、日志、图片缓存）
RUNTIME_ROOT = PROJECT_ROOT / ".DumbCatMangaRuntime"
WEB_RUNTIME = RUNTIME_ROOT / "web"
IMAGE_CACHE_DIR = WEB_RUNTIME / "images"
PLUGIN_CACHE_DIR = WEB_RUNTIME / "plugin-cache"
LOG_DIR = WEB_RUNTIME / "logs"


@dataclass
class Settings:
    """可在「设置」页修改的选项。"""

    #: 下载根目录（每个作品会在其下建子目录）
    output_dir: str = str(DEFAULT_OUTPUT)
    #: 下载根目录下的子目录；留空表示直接用根目录
    output_subdir: str = ""
    #: 每章最多下载多少张图（0 表示不限）
    max_images_per_chapter: int = 0
    #: 并发下载线程
    workers: int = 8
    #: 每章图片之间的并发
    image_workers: int = 4
    #: 界面主题：cream（呆猫奶油）/ night（夜猫）
    theme: str = "cream"
    #: 减少动效（晕动症 / 低配机器）
    reduce_motion: bool = False
    #: 封面网格最小宽度（px），越大每行越少
    grid_min_width: int = 168
    #: 滚动到底自动加载下一页（关闭后显示「加载更多」按钮）
    auto_load_more: bool = True
    #: 章节列表默认倒序（新章节在前）
    chapter_newest_first: bool = True
    #: 是否接入需要浏览器的那几个站点（漫画柜/包子/B站/快看）
    # 安卓版默认关闭：手机上没有 Chromium
    include_browser_sources: bool = False
    #: 记住上次停留的图源
    last_source: str = ""
    #: 下载完成后是否在系统里打开目录
    open_after_download: bool = False
    #: 图片缓存上限（MB），0 表示不限制
    image_cache_mb: int = 512
    #: 搜索时最多同时请求的图源数量（0=全部）
    search_concurrency: int = 4
    #: 额外的配置项（留给以后，不会丢）
    extra: dict[str, Any] = field(default_factory=dict)


_LOCK = threading.RLock()
_cached: Settings | None = None


def _clamp(value: int, low: int, high: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def _coerce(raw: dict[str, Any], base: Settings) -> Settings:
    """把磁盘上的半可信 JSON 收进合法范围，坏字段用默认值。"""
    data = asdict(base)
    for item in fields(Settings):
        if item.name not in raw:
            continue
        value = raw[item.name]
        if item.name in {"reduce_motion", "auto_load_more",
                          "chapter_newest_first", "include_browser_sources",
                          "open_after_download"}:
            data[item.name] = bool(value)
        elif item.name in {"workers", "image_workers"}:
            data[item.name] = _clamp(value, 1, 16, data[item.name])
        elif item.name == "grid_min_width":
            data[item.name] = _clamp(value, 110, 320, data[item.name])
        elif item.name == "image_cache_mb":
            data[item.name] = _clamp(value, 0, 65536, data[item.name])
        elif item.name == "search_concurrency":
            data[item.name] = _clamp(value, 0, 16, data[item.name])
        elif item.name == "theme":
            data[item.name] = "night" if str(value) == "night" else "cream"
        elif item.name == "output_dir":
            text = str(value or "").strip()
            if text:
                data[item.name] = text
        elif item.name == "output_subdir":
            #: 只接受相对路径，挡掉 .. 和绝对路径，避免跑到根目录外面
            text = str(value or "").replace("\\", "/").strip().strip("/")
            parts = [piece for piece in text.split("/") if piece not in ("", ".", "..")]
            data[item.name] = "/".join(parts)[:80]
        elif item.name == "max_images_per_chapter":
            data[item.name] = _clamp(value, 0, 5000, data[item.name])
        elif item.name == "last_source":
            data[item.name] = str(value or "")
        elif item.name == "extra" and isinstance(value, dict):
            data[item.name] = value
    return Settings(**data)


def _legacy_output_dir() -> str:
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(raw, dict):
        return ""
    value = raw.get("output_dir") or raw.get("download_dir") or ""
    return str(value).strip() if value else ""


def load_settings(refresh: bool = False) -> Settings:
    """读取配置（进程内缓存，写盘后自动失效）。"""
    global _cached
    with _LOCK:
        if _cached is not None and not refresh:
            return _cached
        data: dict[str, Any] = {}
        try:
            raw = json.loads(WEB_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except (OSError, ValueError):
            data = {}
        settings = _coerce(data, Settings())
        if not WEB_CONFIG_PATH.exists():
            legacy = _legacy_output_dir()
            if legacy:
                settings.output_dir = legacy
        _cached = settings
        return settings


def save_settings(settings: Settings) -> Settings:
    """写回配置；原子替换，避免半截文件。"""
    global _cached
    with _LOCK:
        WEB_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = WEB_CONFIG_PATH.with_suffix(".tmp")
        payload = json.dumps(asdict(settings), ensure_ascii=False, indent=2)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, WEB_CONFIG_PATH)
        _cached = settings
        return settings


def update_settings(patch: dict[str, Any]) -> Settings:
    """按字段补丁更新并写盘，返回新配置。"""
    with _LOCK:
        current = asdict(load_settings())
        merged = {**current, **{k: v for k, v in patch.items() if v is not None}}
        settings = _coerce(merged, Settings())
        ensure_dirs()
        return save_settings(settings)


def ensure_dirs() -> None:
    for path in (WEB_RUNTIME, IMAGE_CACHE_DIR, PLUGIN_CACHE_DIR, LOG_DIR, USER_PLUGIN_DIR):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def output_root() -> Path:
    root = Path(load_settings().output_dir or str(DEFAULT_OUTPUT)).expanduser()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return root


def load_enabled_overlay() -> dict[str, bool]:
    """读图源开关覆盖层；文件缺失或损坏都当作「没有任何覆盖」。"""
    try:
        raw = json.loads(SOURCES_ENABLED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    overlay: dict[str, bool] = {}
    for key, value in raw.items():
        name = str(key or "").strip().lower()
        if name:
            overlay[name] = bool(value)
    return overlay


def save_enabled_overlay(overlay: dict[str, bool]) -> dict[str, bool]:
    """写回图源开关覆盖层；原子替换，避免半截文件。"""
    clean = {str(k).strip().lower(): bool(v) for k, v in overlay.items() if str(k).strip()}
    with _LOCK:
        SOURCES_ENABLED_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SOURCES_ENABLED_PATH.with_suffix(".tmp")
        payload = json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, SOURCES_ENABLED_PATH)
    return clean
