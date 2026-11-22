"""图源清单：每个图源一个 JSON 文件，放在 ``dumbcat/sources/json/``。

这样「有哪些图源、叫什么、开不开、18+、需不需要浏览器」都是改 JSON 就行，
不用动 Python。JSON 里 ``engine`` 决定实际怎么抓：

``python``（默认）
    用现成的适配器：``module`` + ``class`` 指向实现类，``args`` / ``kwargs``
    是构造参数。接口型、需要登录、需要浏览器的站点都走这条，稳定。
``rules``
    纯 CSS / JSON 选择器规则，由 ``dumbcat.plugins.json_source`` 解释执行，
    适合自己加的 HTML 站点。规则型文件由 registry 当插件加载，这里跳过。

用户也可以在 ``用户插件/`` 里放同名 JSON 覆盖内置那一条（key 相同即覆盖）。
"""
from __future__ import annotations

import os

import importlib
import json
import logging
from pathlib import Path
from typing import Any, Iterable

from dumbcat.sources.base import SeriesProvider

log = logging.getLogger("dumbcat.sources.manifest")

#: 内置图源清单目录
JSON_SOURCE_DIR = Path(os.getenv("DUMBCAT_JSON_DIR") or (Path(__file__).resolve().parent / "json"))

#: 清单里允许直接写进 provider 的字段（其余字段忽略，留给人看）
META_KEYS = ("key", "name", "icon", "nsfw", "note", "status", "order", "homepage")


def load_specs(dirs: Iterable[Path | str] | None = None) -> list[dict[str, Any]]:
    """读出所有清单（不做实例化，失败的文件跳过并记日志）。"""
    roots = [Path(item) for item in (dirs or [JSON_SOURCE_DIR])]
    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.json")):
            if path.name == "plugin.json":
                continue
            try:
                cfg = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001 - 一个坏文件不该拖垮全部
                log.warning("图源清单 %s 解析失败：%s", path.name, exc)
                continue
            if not isinstance(cfg, dict):
                continue
            key = str(cfg.get("key") or path.stem).strip().lower()
            if not key or key in seen:
                continue
            engine = str(cfg.get("engine") or "python").strip().lower()
            if engine == "rules":
                # 规则型由 registry 当插件加载，这里不重复处理
                continue
            spec = dict(cfg)
            spec["key"] = key
            spec["engine"] = engine or "python"
            spec["path"] = str(path)
            seen.add(key)
            specs.append(spec)
    return specs


def build_provider(spec: dict[str, Any]) -> SeriesProvider:
    """按清单实例化一个 provider，并把清单里的元信息挂上去。"""
    module_name = str(spec.get("module") or "dumbcat.sources.providers")
    class_name = str(spec.get("class") or "").strip()
    if not class_name:
        raise ValueError("清单缺少 class")
    module = importlib.import_module(module_name)
    factory = getattr(module, class_name)
    args = list(spec.get("args") or [])
    kwargs = dict(spec.get("kwargs") or {})
    provider = factory(*args, **kwargs)

    meta = {key: spec.get(key) for key in META_KEYS if spec.get(key) is not None}
    meta.setdefault("key", str(getattr(provider, "key", spec["key"])))
    meta["file"] = spec.get("path", "")
    if spec.get("name"):
        # 清单里的现成名字优先（provider 自己也有一份，但 JSON 允许改）
        try:
            provider.display_name = str(spec["name"])
        except Exception:  # noqa: BLE001 - 只读属性就随它去
            pass
    provider.source_meta = meta  # type: ignore[attr-defined]
    return provider


def build_providers(
    dirs: Iterable[Path | str] | None = None,
    include_breeze: bool = True,
) -> dict[str, SeriesProvider]:
    """读出所有可用图源：key → provider。坏一条跳过，不影响其他。"""
    result: dict[str, SeriesProvider] = {}
    for spec in load_specs(dirs):
        if spec.get("enabled") is False:
            continue
        if spec.get("breeze") and not include_breeze:
            continue
        try:
            provider = build_provider(spec)
        except Exception as exc:  # noqa: BLE001
            log.error("图源 %s（%s）装载失败：%s", spec.get("key"), spec.get("class"), exc)
            continue
        key = str(getattr(provider, "key", "") or spec["key"])
        result[key] = provider
    return result
