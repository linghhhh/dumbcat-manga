"""书架 / 官方收藏 / 阅读历史的本地存储层。

纯标准库：JSON 落盘、原子写（``.tmp`` + :func:`os.replace`）、进程内可重入锁。
这里完全不碰网络，也不 import 任何图源；远端收藏由路由层（``server/app.py``）
通过 provider 的 ``add_favorite`` / ``remove_favorite`` 调用后再回写 ``remote`` 状态。

存储文件：``dumbcat.app_config.APP_CONFIG_DIR / "shelves.json"``
结构（与 docs/书架接口.md 第 2 节一致）::

    {
      "version": 1,
      "migrated": true,
      "shelves": [{"id", "name", "kind", "source", "created", "updated"}],
      "items":   [{"id", "shelfId", "uid", "source", "key", "title", "cover",
                   "coverUrl", "author", "tags", "nsfw", "url", "providerName",
                   "added", "remote"}],
      "history": [{"uid", "source", "title", "cover", "coverUrl", "chapterUid",
                   "chapterTitle", "page", "total", "ts"}]
    }
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from dumbcat.app_config import APP_CONFIG_DIR

log = logging.getLogger("dumbcat.shelf")

STORE_VERSION = 1
STORE_PATH: Path = APP_CONFIG_DIR / "shelves.json"
LEGACY_DOWNLOADS_PATH: Path = APP_CONFIG_DIR / "legacy_downloads.json"

DEFAULT_SHELF_ID = "s_default"
DEFAULT_SHELF_NAME = "我的书架"
LEGACY_SHELF_ID = "s_legacy"
LEGACY_SHELF_NAME = "旧版下载"
SOURCE_SHELF_PREFIX = "s_source_"

MAX_HISTORY = 500
MAX_NAME = 60

_TAG_SPLIT = re.compile(r"[,，/|、;；]+")
_SHELF_NUMBER = re.compile(r"^书架\s*(\d+)$")
_REMOTE_STATES = ("synced", "unsupported", "not-logged-in", "failed", "kept", "")

_LOCK = threading.RLock()


class ShelfError(Exception):
    """可读的书架错误；路由层统一转成 ``400 + detail``。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _now() -> int:
    return int(time.time())


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _text(value: Any, limit: int = 400) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _clean_cover(url: Any) -> str:
    """封面地址里去掉 ``token`` 查询参数。

    图源返回的封面常常带 ``?token=xxx``（当前进程的临时令牌），存下来重启后就失效；
    前端会用 ``withToken(...)`` 自己追加当前令牌，所以这里保持干净地址。
    """
    text = _text(url, 2000)
    if not text or "token=" not in text:
        return text
    parsed = urllib.parse.urlsplit(text)
    if not parsed.query:
        return text
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    kept = [(key, value) for key, value in pairs if key != "token"]
    if len(kept) == len(pairs):
        return text
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(kept), parsed.fragment)
    )


def _source_from_uid(uid: str) -> str:
    text = _text(uid, 200)
    for sep in (":", "."):
        if sep in text:
            return text.split(sep, 1)[0]
    return ""


def source_from_uid(uid: str) -> str:
    """公开的小工具：从 uid 里猜图源 key（``18comic.abc`` / ``18comic:123``）。"""
    return _source_from_uid(uid)


def _source_of(series: dict[str, Any], uid: str = "") -> str:
    for name in ("source", "provider", "_provider", "providerKey", "site"):
        value = _text(series.get(name), 40)
        if value:
            return value
    return _source_from_uid(uid)


def _uid_of(series: dict[str, Any], source: str) -> str:
    for name in ("uid", "seriesUid", "series_uid"):
        value = _text(series.get(name), 200)
        if value:
            return value
    ident = series.get("id")
    if ident not in (None, "", 0, "0"):
        return f"{source}.{ident}" if source else str(ident)
    return ""


def _tags_of(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates: Any = _TAG_SPLIT.split(value)
    elif isinstance(value, (list, tuple, set)):
        candidates = value
    else:
        candidates = []
    tags: list[str] = []
    for entry in candidates:
        if isinstance(entry, dict):
            entry = entry.get("name") or entry.get("title") or entry.get("label") or ""
        text = _text(entry, 40)
        if text and text not in tags:
            tags.append(text)
        if len(tags) >= 16:
            break
    return tags


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


# ---------------------------------------------------------------------------
# 对象构造
# ---------------------------------------------------------------------------


def _new_shelf(name: str, kind: str = "local", source: str = "", shelf_id: str = "") -> dict[str, Any]:
    at = _now()
    return {
        "id": shelf_id or _new_id("s"),
        "name": _text(name, MAX_NAME) or "未命名书架",
        "kind": kind if kind in ("local", "source") else "local",
        "source": _text(source, 40),
        "created": at,
        "updated": at,
    }


def _series_fields(series: dict[str, Any], uid: str, source: str) -> dict[str, Any]:
    data = series if isinstance(series, dict) else {}
    cover = _clean_cover(
        data.get("cover")
        or data.get("coverUrl")
        or data.get("coverRemote")
        or data.get("poster")
        or data.get("image")
        or ""
    )
    provider_name = (
        _text(data.get("providerName"), 60)
        or _text(data.get("_source_label"), 60)
        or _text(data.get("provider"), 60)
        or source
    )
    title = _text(data.get("title"), 200) or _text(data.get("name"), 200) or "未命名作品"
    return {
        "source": source,
        "key": source,
        "title": title,
        "cover": cover,
        "coverUrl": cover,
        "author": _text(data.get("author") or data.get("artist"), 100),
        "tags": _tags_of(data.get("tags")),
        "nsfw": _bool(data.get("nsfw")),
        "url": _text(data.get("url") or data.get("link"), 1000),
        "providerName": provider_name,
    }


def _new_item(shelf_id: str, series: dict[str, Any], remote: str = "") -> dict[str, Any]:
    data = series if isinstance(series, dict) else {}
    uid = _uid_of(data, _source_of(data))
    source = _source_of(data, uid)
    fields = _series_fields(data, uid, source)
    return {
        "id": _new_id("i"),
        "shelfId": shelf_id,
        "uid": uid,
        "source": fields["source"],
        "key": fields["key"],
        "title": fields["title"],
        "cover": fields["cover"],
        "coverUrl": fields["coverUrl"],
        "author": fields["author"],
        "tags": fields["tags"],
        "nsfw": fields["nsfw"],
        "url": fields["url"],
        "providerName": fields["providerName"],
        "added": _now(),
        "remote": remote if remote in _REMOTE_STATES else "",
    }


def _public_shelf(store: dict[str, Any], shelf: dict[str, Any]) -> dict[str, Any]:
    count = sum(1 for item in store["items"] if item.get("shelfId") == shelf.get("id"))
    return {
        "id": _text(shelf.get("id"), 80),
        "name": _text(shelf.get("name"), MAX_NAME),
        "kind": shelf.get("kind") if shelf.get("kind") in ("local", "source") else "local",
        "source": _text(shelf.get("source"), 40),
        "count": count,
        "created": _int(shelf.get("created")),
        "updated": _int(shelf.get("updated")),
    }


def _find_shelf(store: dict[str, Any], shelf_id: str) -> dict[str, Any] | None:
    target = _text(shelf_id, 80)
    for shelf in store["shelves"]:
        if shelf.get("id") == target:
            return shelf
    return None


def _find_item(store: dict[str, Any], shelf_id: str, uid: str) -> dict[str, Any] | None:
    for item in store["items"]:
        if item.get("shelfId") == shelf_id and item.get("uid") == uid:
            return item
    return None


def _history_public(entry: dict[str, Any]) -> dict[str, Any]:
    cover = _clean_cover(entry.get("cover") or entry.get("coverUrl") or "")
    return {
        "uid": _text(entry.get("uid"), 200),
        "source": _text(entry.get("source"), 40) or _source_from_uid(_text(entry.get("uid"), 200)),
        "title": _text(entry.get("title"), 200),
        "cover": cover,
        "coverUrl": cover,
        "chapterUid": _text(entry.get("chapterUid") or entry.get("chapter_uid"), 200),
        "chapterTitle": _text(entry.get("chapterTitle") or entry.get("chapter_title"), 200),
        "page": max(0, _int(entry.get("page"))),
        "total": max(0, _int(entry.get("total"))),
        "ts": _int(entry.get("ts")),
    }


# ---------------------------------------------------------------------------
# 读 / 写（原子）
# ---------------------------------------------------------------------------


def _new_store() -> dict[str, Any]:
    return {
        "version": STORE_VERSION,
        "migrated": False,
        "shelves": [],
        "items": [],
        "history": [],
    }


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("读取书架数据失败（%s）：%s", path, exc)
        return None


def _sanitize(raw: dict[str, Any]) -> dict[str, Any]:
    store = _new_store()
    store["version"] = _int(raw.get("version"), STORE_VERSION) or STORE_VERSION
    store["migrated"] = raw.get("migrated") is True

    shelves: list[dict[str, Any]] = []
    seen_shelves: set[str] = set()
    for entry in raw.get("shelves") or []:
        if not isinstance(entry, dict):
            continue
        shelf_id = _text(entry.get("id"), 80)
        if not shelf_id or shelf_id in seen_shelves:
            continue
        seen_shelves.add(shelf_id)
        kind = entry.get("kind") if entry.get("kind") in ("local", "source") else "local"
        shelves.append(
            {
                "id": shelf_id,
                "name": _text(entry.get("name"), MAX_NAME) or "未命名书架",
                "kind": kind,
                "source": _text(entry.get("source"), 40),
                "created": _int(entry.get("created")),
                "updated": _int(entry.get("updated")),
            }
        )
    store["shelves"] = shelves

    shelf_ids = {shelf["id"] for shelf in shelves}
    items: list[dict[str, Any]] = []
    seen_items: set[tuple[str, str]] = set()
    for entry in raw.get("items") or []:
        if not isinstance(entry, dict):
            continue
        shelf_id = _text(entry.get("shelfId"), 80)
        uid = _text(entry.get("uid"), 200)
        if not shelf_id or not uid or shelf_id not in shelf_ids:
            continue
        marker = (shelf_id, uid)
        if marker in seen_items:
            continue
        seen_items.add(marker)
        source = _text(entry.get("source"), 40) or _text(entry.get("key"), 40) or _source_from_uid(uid)
        cover = _clean_cover(entry.get("cover") or entry.get("coverUrl") or "")
        remote = entry.get("remote")
        items.append(
            {
                "id": _text(entry.get("id"), 80) or _new_id("i"),
                "shelfId": shelf_id,
                "uid": uid,
                "source": source,
                "key": _text(entry.get("key"), 40) or source,
                "title": _text(entry.get("title"), 200) or "未命名作品",
                "cover": cover,
                "coverUrl": cover,
                "author": _text(entry.get("author"), 100),
                "tags": _tags_of(entry.get("tags")),
                "nsfw": _bool(entry.get("nsfw")),
                "url": _text(entry.get("url"), 1000),
                "providerName": _text(entry.get("providerName"), 60) or source,
                "added": _int(entry.get("added")),
                "remote": remote if remote in _REMOTE_STATES else "",
            }
        )
    store["items"] = items

    history: list[dict[str, Any]] = []
    seen_history: set[str] = set()
    for entry in raw.get("history") or []:
        if not isinstance(entry, dict):
            continue
        uid = _text(entry.get("uid"), 200)
        if not uid or uid in seen_history:
            continue
        seen_history.add(uid)
        history.append(_history_public(entry))
    store["history"] = history[:MAX_HISTORY]
    return store


def _load_from_disk() -> dict[str, Any]:
    raw = _read_json(STORE_PATH)
    if not isinstance(raw, dict):
        if STORE_PATH.exists():
            log.warning("书架数据格式不对，按空书架重建：%s", STORE_PATH)
        return _new_store()
    return _sanitize(raw)


def _write_store(store: dict[str, Any]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_name(STORE_PATH.name + ".tmp")
    payload = json.dumps(store, ensure_ascii=False, indent=1)
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, STORE_PATH)


# ---------------------------------------------------------------------------
# 旧版下载迁移
# ---------------------------------------------------------------------------


def _legacy_rows() -> list[dict[str, Any]]:
    raw = _read_json(LEGACY_DOWNLOADS_PATH)
    if raw is None:
        return []
    if not isinstance(raw, list):
        log.warning("旧版下载记录不是列表，已跳过：%s", LEGACY_DOWNLOADS_PATH)
        return []
    return [row for row in raw if isinstance(row, dict)]


def _apply_legacy(store: dict[str, Any], rows: list[dict[str, Any]]) -> int:
    shelf: dict[str, Any] | None = None
    seen = {_text(item.get("uid"), 200) for item in store["items"]}
    added = 0
    for row in rows:
        ident = _text(row.get("id"), 120)
        provider = _text(row.get("provider"), 40)
        title = _text(row.get("title"), 200)
        if not title and not ident:
            continue
        uid = _text(row.get("uid"), 200)
        if not uid:
            uid = f"{provider}.{ident}" if provider and ident else (ident or title)
        if not uid or uid in seen:
            continue
        if shelf is None:
            shelf = _find_shelf(store, LEGACY_SHELF_ID)
            if shelf is None:
                shelf = _new_shelf(LEGACY_SHELF_NAME, shelf_id=LEGACY_SHELF_ID)
                store["shelves"].append(shelf)
        store["items"].append(
            {
                "id": _new_id("i"),
                "shelfId": LEGACY_SHELF_ID,
                "uid": uid,
                "source": provider,
                "key": provider,
                "title": title or ident,
                "cover": "",
                "coverUrl": "",
                "author": "",
                "tags": [],
                "nsfw": False,
                "url": "",
                "providerName": _text(row.get("providerName"), 60) or provider,
                "added": _int(row.get("finishedAt")) or _now(),
                "remote": "",
            }
        )
        seen.add(uid)
        added += 1
    if shelf is not None and added:
        shelf["updated"] = _now()
    return added


def migrate_legacy_downloads(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """把旧版 ``downloads.json`` 的行写进 local 架「旧版下载」（幂等）。"""
    with _LOCK:
        store = load_store()
        added = _apply_legacy(store, [row for row in (rows or []) if isinstance(row, dict)])
        store["migrated"] = True
        _write_store(store)
        shelf = _find_shelf(store, LEGACY_SHELF_ID)
        return {
            "ok": True,
            "added": added,
            "shelfId": LEGACY_SHELF_ID if shelf is not None else "",
        }


# ---------------------------------------------------------------------------
# 书架
# ---------------------------------------------------------------------------


def load_store() -> dict[str, Any]:
    """读取并补齐书架数据；首次加载（或文件损坏）时建默认架、迁移旧下载。"""
    with _LOCK:
        store = _load_from_disk()
        changed = False
        if not store["migrated"]:
            if not any(shelf["id"] == DEFAULT_SHELF_ID for shelf in store["shelves"]):
                store["shelves"].insert(
                    0, _new_shelf(DEFAULT_SHELF_NAME, shelf_id=DEFAULT_SHELF_ID)
                )
                changed = True
            try:
                if _apply_legacy(store, _legacy_rows()):
                    changed = True
            except Exception as exc:  # 迁移失败只打日志，不能拖垮启动
                log.warning("旧版下载迁移失败：%s", exc)
            store["migrated"] = True
            changed = True
        if not store["shelves"]:
            store["shelves"].append(_new_shelf(DEFAULT_SHELF_NAME, shelf_id=DEFAULT_SHELF_ID))
            changed = True
        if changed:
            _write_store(store)
        return store


def default_shelf_id() -> str:
    store = load_store()
    for shelf in store["shelves"]:
        if shelf.get("id") == DEFAULT_SHELF_ID:
            return DEFAULT_SHELF_ID
    return _text(store["shelves"][0].get("id"), 80)


def list_shelves() -> list[dict[str, Any]]:
    store = load_store()
    return [_public_shelf(store, shelf) for shelf in store["shelves"]]


def get_shelf(shelf_id: str) -> dict[str, Any] | None:
    store = load_store()
    shelf = _find_shelf(store, shelf_id)
    return _public_shelf(store, shelf) if shelf is not None else None


def _next_shelf_name(store: dict[str, Any]) -> str:
    names = {_text(shelf.get("name"), MAX_NAME) for shelf in store["shelves"]}
    number = 2
    while f"书架 {number}" in names:
        number += 1
    return f"书架 {number}"


def create_shelf(name: str = "") -> dict[str, Any]:
    with _LOCK:
        store = load_store()
        text = _text(name, MAX_NAME)
        if not text:
            text = _next_shelf_name(store)
        shelf = _new_shelf(text)
        store["shelves"].append(shelf)
        _write_store(store)
        return _public_shelf(store, shelf)


def rename_shelf(shelf_id: str, name: str) -> dict[str, Any]:
    with _LOCK:
        store = load_store()
        shelf = _find_shelf(store, shelf_id)
        if shelf is None:
            raise ShelfError("书架不存在")
        text = _text(name, MAX_NAME)
        if not text:
            raise ShelfError("书架名字不能为空")
        shelf["name"] = text
        shelf["updated"] = _now()
        _write_store(store)
        return _public_shelf(store, shelf)


def delete_shelf(shelf_id: str) -> dict[str, Any]:
    with _LOCK:
        store = load_store()
        shelf = _find_shelf(store, shelf_id)
        if shelf is None:
            raise ShelfError("书架不存在")
        if shelf.get("kind") == "source":
            raise ShelfError("图源书架不能删除，可以先退出图源账号")
        before = len(store["items"])
        store["shelves"] = [entry for entry in store["shelves"] if entry.get("id") != shelf_id]
        store["items"] = [item for item in store["items"] if item.get("shelfId") != shelf_id]
        removed = before - len(store["items"])
        _write_store(store)
        return {"ok": True, "removed": removed}


# ---------------------------------------------------------------------------
# 收藏项
# ---------------------------------------------------------------------------


def list_items(shelf_id: str = "") -> list[dict[str, Any]]:
    store = load_store()
    target = _text(shelf_id, 80)
    rows = [
        dict(item)
        for item in store["items"]
        if not target or item.get("shelfId") == target
    ]
    rows.sort(key=lambda item: _int(item.get("added")), reverse=True)
    return rows


def get_item(shelf_id: str, uid: str) -> dict[str, Any] | None:
    store = load_store()
    item = _find_item(store, _text(shelf_id, 80), _text(uid, 200))
    return dict(item) if item is not None else None


def add_item(shelf_id: str, series: dict[str, Any], remote: str = "") -> dict[str, Any]:
    """把作品加入书架；同架同 uid 已存在时原样返回已有项（幂等）。"""
    with _LOCK:
        store = load_store()
        shelf = _find_shelf(store, shelf_id)
        if shelf is None:
            raise ShelfError("书架不存在")
        data = dict(series) if isinstance(series, dict) else {}
        uid = _uid_of(data, _source_of(data))
        if not uid:
            raise ShelfError("缺少作品 uid")
        existing = _find_item(store, shelf["id"], uid)
        if existing is not None:
            return dict(existing)
        item = _new_item(shelf["id"], data, remote=remote)
        item["uid"] = uid
        store["items"].append(item)
        shelf["updated"] = _now()
        _write_store(store)
        return dict(item)


def set_item_remote(shelf_id: str, uid: str, remote: str) -> dict[str, Any] | None:
    with _LOCK:
        store = load_store()
        item = _find_item(store, _text(shelf_id, 80), _text(uid, 200))
        if item is None:
            return None
        state = remote if remote in _REMOTE_STATES else ""
        if item.get("remote") != state:
            item["remote"] = state
            _write_store(store)
        return dict(item)


def remove_item(shelf_id: str, uid: str) -> bool:
    with _LOCK:
        store = load_store()
        target_shelf = _text(shelf_id, 80)
        target_uid = _text(uid, 200)
        before = len(store["items"])
        store["items"] = [
            item
            for item in store["items"]
            if not (item.get("shelfId") == target_shelf and item.get("uid") == target_uid)
        ]
        if len(store["items"]) == before:
            return False
        shelf = _find_shelf(store, target_shelf)
        if shelf is not None:
            shelf["updated"] = _now()
        _write_store(store)
        return True


def shelves_of(uid: str) -> list[dict[str, Any]]:
    store = load_store()
    target = _text(uid, 200)
    out: list[dict[str, Any]] = []
    for shelf in store["shelves"]:
        if any(
            item.get("shelfId") == shelf.get("id") and item.get("uid") == target
            for item in store["items"]
        ):
            out.append({"id": _text(shelf.get("id"), 80), "name": _text(shelf.get("name"), MAX_NAME)})
    return out


# ---------------------------------------------------------------------------
# 图源书架同步
# ---------------------------------------------------------------------------


def sync_source_items(
    source: str, series_list: list[dict[str, Any]], display_name: str = ""
) -> dict[str, Any]:
    """把图源官方收藏增量合并进 ``s_source_<source>``，不动 local 架。"""
    key = _text(source, 40)
    if not key:
        raise ShelfError("缺少图源标识")
    with _LOCK:
        store = load_store()
        shelf_id = f"{SOURCE_SHELF_PREFIX}{key}"
        shelf = _find_shelf(store, shelf_id)
        shelf_name = f"{_text(display_name, 60) or key} 收藏"
        if shelf is None:
            shelf = _new_shelf(shelf_name, kind="source", source=key, shelf_id=shelf_id)
            store["shelves"].append(shelf)
        elif _text(display_name, 60):
            shelf["name"] = shelf_name

        existing = {
            item.get("uid"): item
            for item in store["items"]
            if item.get("shelfId") == shelf_id
        }
        incoming: dict[str, dict[str, Any]] = {}
        for series in series_list or []:
            if not isinstance(series, dict):
                continue
            uid = _uid_of(series, _source_of(series, ""))
            if not uid:
                continue
            incoming[uid] = series

        added = 0
        for uid, series in incoming.items():
            fresh = _new_item(shelf_id, series, remote="synced")
            fresh["uid"] = uid
            old = existing.get(uid)
            if old is None:
                store["items"].append(fresh)
                added += 1
            else:
                fresh["id"] = old.get("id") or fresh["id"]
                fresh["added"] = _int(old.get("added")) or fresh["added"]
                index = store["items"].index(old)
                store["items"][index] = fresh

        removed = 0
        for uid, old in existing.items():
            if uid not in incoming:
                store["items"].remove(old)
                removed += 1

        shelf["updated"] = _now()
        _write_store(store)
        return {
            "shelf": _public_shelf(store, shelf),
            "added": added,
            "removed": removed,
            "total": len(incoming),
            "at": _now(),
        }


# ---------------------------------------------------------------------------
# 阅读历史
# ---------------------------------------------------------------------------


def history_add(record: dict[str, Any]) -> dict[str, Any]:
    data = record if isinstance(record, dict) else {}
    uid = _text(data.get("uid"), 200)
    if not uid:
        raise ShelfError("缺少作品 uid")
    with _LOCK:
        store = load_store()
        entry = _history_public(
            {
                "uid": uid,
                "source": data.get("source"),
                "title": data.get("title"),
                "cover": data.get("cover") or data.get("coverUrl"),
                "chapterUid": data.get("chapterUid") or data.get("chapter_uid"),
                "chapterTitle": data.get("chapterTitle") or data.get("chapter_title"),
                "page": data.get("page"),
                "total": data.get("total"),
                "ts": data.get("ts") or _now(),
            }
        )
        store["history"] = [item for item in store["history"] if item.get("uid") != uid]
        store["history"].insert(0, entry)
        del store["history"][MAX_HISTORY:]
        _write_store(store)
        return dict(entry)


def history_list(limit: int = 100) -> list[dict[str, Any]]:
    store = load_store()
    rows = sorted(
        (_history_public(item) for item in store["history"]),
        key=lambda item: _int(item.get("ts")),
        reverse=True,
    )
    try:
        count = int(limit)
    except (TypeError, ValueError):
        count = 100
    if count <= 0:
        count = len(rows)
    return rows[: min(count, MAX_HISTORY)]


def history_count() -> int:
    return len(load_store()["history"])


def history_clear() -> int:
    with _LOCK:
        store = load_store()
        removed = len(store["history"])
        store["history"] = []
        _write_store(store)
        return removed


def history_remove(uid: str) -> bool:
    target = _text(uid, 200)
    if not target:
        raise ShelfError("缺少作品 uid")
    with _LOCK:
        store = load_store()
        before = len(store["history"])
        store["history"] = [item for item in store["history"] if item.get("uid") != target]
        if len(store["history"]) == before:
            return False
        _write_store(store)
        return True
