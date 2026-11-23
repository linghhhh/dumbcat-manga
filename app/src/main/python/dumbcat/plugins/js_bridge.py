"""Python ↔ Java 的 QuickJS 桥。

JS 图源真正跑在安卓壳的内嵌 QuickJS 里（``com.dumbcat.manga.JsRuntime``），
这个模块只负责把 Python 侧的字典变成 JSON 字符串递过去。

桌面版（没有安卓壳）取不到 ``java`` 模块，:func:`available` 返回 False，
上层据此把 JS 图源标记成「当前版本不支持」，而不是让整个注册表炸掉。
"""

from __future__ import annotations

import json
import logging
from typing import Any

__all__ = ["available", "load", "call", "unload", "set_prelude_path", "JsError"]

log = logging.getLogger("dumbcat.js")

_CLASS: Any = None
_MISSING = False


class JsError(RuntimeError):
    """JS 图源执行失败（脚本抛错、超时、找不到函数都走这里）。"""


def _runtime() -> Any:
    """拿到 Java 侧的 ``JsRuntime`` 类；拿不到就返回 None（桌面版）。"""
    global _CLASS, _MISSING
    if _CLASS is not None:
        return _CLASS
    if _MISSING:
        return None
    try:
        from java import jclass  # type: ignore[import-not-found]

        _CLASS = jclass("com.dumbcat.manga.JsRuntime")
    except Exception as exc:  # noqa: BLE001 - 桌面版没有 java 模块
        _MISSING = True
        log.debug("JS 运行时不可用：%s", exc)
        return None
    return _CLASS


def available() -> bool:
    """当前环境能不能跑 JS 图源。"""
    return _runtime() is not None


def _require() -> Any:
    runtime = _runtime()
    if runtime is None:
        raise JsError("当前版本不支持 JS 图源（需要安卓版应用）")
    return runtime


def _decode(text: Any) -> Any:
    if text is None:
        return None
    try:
        return json.loads(str(text))
    except ValueError as exc:
        raise JsError(f"JS 返回值不是 JSON：{str(text)[:200]}") from exc


def load(key: str, script: str) -> dict[str, Any]:
    """把一个 JS 脚本塞进 QuickJS，返回它 ``registerSource`` 的元信息。"""
    runtime = _require()
    try:
        meta = runtime.load(str(key), str(script))
    except Exception as exc:  # noqa: BLE001 - Java 异常统一转成 JsError
        raise JsError(str(exc)) from exc
    data = _decode(meta)
    if not isinstance(data, dict):
        raise JsError("图源脚本没有 registerSource")
    return data


def call(key: str, fn: str, payload: dict[str, Any] | None = None) -> Any:
    """调用图源里的一个函数，payload 与返回值都是普通 dict/list。"""
    runtime = _require()
    body = json.dumps(payload or {}, ensure_ascii=False)
    try:
        text = runtime.call(str(key), str(fn), body)
    except Exception as exc:  # noqa: BLE001
        raise JsError(str(exc)) from exc
    return _decode(text)


def unload(key: str) -> None:
    """卸载图源（禁用/删除/热重载）。"""
    runtime = _runtime()
    if runtime is None:
        return
    try:
        runtime.unload(str(key))
    except Exception as exc:  # noqa: BLE001
        log.debug("卸载 JS 图源失败 %s：%s", key, exc)


def set_prelude_path(path: str) -> None:
    """启动时告诉 Java 侧 prelude.js 在哪（assets 解压后的绝对路径）。"""
    runtime = _runtime()
    if runtime is None:
        return
    try:
        runtime.setPreludePath(str(path or ""))
    except Exception as exc:  # noqa: BLE001
        log.warning("登记 prelude 路径失败：%s", exc)
