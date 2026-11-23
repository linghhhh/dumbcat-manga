"""书架 / 官方收藏 / 阅读历史的本地存储层。

主存 SQLite（``dumbcat.server.db`` 的 ``shelf`` / ``shelf_item`` 两张表 +
``meta`` 里的历史记录），旧版 ``shelves.json`` 首次启动时自动迁进来。
进程内可重入锁；这里完全不碰网络，也不 import 任何图源；远端收藏由路由层
（``server/app.py``）通过 provider 的 ``add_favorite`` / ``remove_favorite``
调用后再回写 ``remote`` 状态。

内存里的结构（读写函数都用它，改完交给 :func:`_write_store` 整体同步）::

    {
      "version": 1,
      "migrated": true,
      "shelves": [{"id", "name", "kind", "source", "created", "updated"}],
      "items":   [{"id", "shelfId", "uid", "source", "key", "title", "cover",
                   "coverUrl", "author", "tags", "nsfw", "url", "providerName",
                   "added", "remote", "chapters", "summary", "updated"}],
      "history": [{"uid", "source", "title", "cover", "coverUrl", "chapterUid",
                   "chapterTitle", "page", "total", "ts"}]
    }

数据库不可用时自动退回旧版 JSON 落盘（``shelves.json``），不会丢数据。
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


def _history_limit() -> int:
    """历史条数上限：设置里可调，读不到设置就用默认值。"""
    try:
        from dumbcat.server.config import load_settings

        return max(100, min(3000, int(load_settings().history_limit or MAX_HISTORY)))
    except Exception:  # noqa: BLE001 - 设置坏了不该影响历史
        return MAX_HISTORY
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


MAX_SNAPSHOT_CHAPTERS = 6000


def _chapter_snapshot(chapters: Any) -> list[dict[str, Any]]:
    """把章节列表压成可离线展示的快照（只留展示与定位用得到的字段）。"""
    if not isinstance(chapters, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for entry in chapters[:MAX_SNAPSHOT_CHAPTERS]:
        if not isinstance(entry, dict):
            continue
        uid = _text(entry.get("uid"), 200)
        if not uid:
            continue
        try:
            number = float(entry.get("number")) if entry.get("number") not in (None, "") else None
        except (TypeError, ValueError):
            number = None
        out.append(
            {
                "uid": uid,
                "title": _text(entry.get("title"), 200),
                "number": number,
                "index": _int(entry.get("index")),
                "locked": _bool(entry.get("locked")),
            }
        )
    return out


def _summary_snapshot(series: dict[str, Any]) -> dict[str, Any]:
    """作品详情的离线快照：书架/历史断网时靠它把详情页画出来。"""
    data = series if isinstance(series, dict) else {}
    cover = _clean_cover(
        data.get("cover") or data.get("coverUrl") or data.get("coverRemote") or ""
    )
    return {
        "uid": _text(data.get("uid"), 200),
        "title": _text(data.get("title") or data.get("name"), 200),
        "author": _text(data.get("author") or data.get("artist"), 100),
        "cover": cover,
        "coverUrl": cover,
        "description": _text(data.get("description") or data.get("summary"), 4000),
        "tags": _tags_of(data.get("tags")),
        "status": _text(data.get("status"), 60),
        "url": _text(data.get("url") or data.get("link"), 1000),
        "nsfw": _bool(data.get("nsfw")),
        "providerName": _text(data.get("providerName"), 60),
        "updated": _now(),
    }


def _new_item(
    shelf_id: str,
    series: dict[str, Any],
    remote: str = "",
    chapters: Any = None,
    summary: Any = None,
) -> dict[str, Any]:
    data = series if isinstance(series, dict) else {}
    uid = _uid_of(data, _source_of(data))
    source = _source_of(data, uid)
    fields = _series_fields(data, uid, source)
    snap = _summary_snapshot(data) if isinstance(summary, dict) else _summary_snapshot(data)
    if isinstance(summary, dict) and summary:
        snap.update({k: v for k, v in summary.items() if v not in (None, "", [], {})})
    snap["uid"] = uid or snap.get("uid", "")
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
        "updated": _now(),
        "remote": remote if remote in _REMOTE_STATES else "",
        "chapters": _chapter_snapshot(chapters),
        "summary": snap,
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
        summary = entry.get("summary") if isinstance(entry.get("summary"), dict) else {}
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
                "updated": _int(entry.get("updated")) or _int(entry.get("added")),
                "remote": remote if remote in _REMOTE_STATES else "",
                "chapters": _chapter_snapshot(entry.get("chapters")),
                "summary": _summary_snapshot({**summary, "uid": uid, "title": _text(entry.get("title"), 200), "author": _text(entry.get("author"), 100), "cover": cover, "providerName": _text(entry.get("providerName"), 60)}),
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
    store["history"] = history[:_history_limit()]
    return store


# ---------------------------------------------------------------------------
# SQLite 读写（数据库不可用时退回 JSON）
# ---------------------------------------------------------------------------

_HISTORY_KEY = "shelf_history"
_MIGRATED_KEY = "shelf_migrated"


def _database():
    """按需打开数据库；不可用时返回 None（调用方退回 JSON）。"""
    try:
        from dumbcat.server import db as dbmod

        with dbmod.lock():
            return dbmod.conn()
    except Exception as exc:  # pragma: no cover - 极端环境
        log.warning("书架数据库不可用，继续用 JSON：%s", exc)
        return None


def _load_from_db() -> dict[str, Any] | None:
    """从 SQLite 读回内存结构；库还是空的返回 None。"""
    try:
        from dumbcat.server import db as dbmod

        rows = dbmod.query_all("SELECT * FROM shelf ORDER BY position, id")
        if not rows:
            return None
        items = dbmod.query_all("SELECT * FROM shelf_item")
        meta = {
            str(row["key"]): row["value"]
            for row in dbmod.query_all("SELECT key, value FROM meta")
        }
    except Exception as exc:  # pragma: no cover
        log.warning("读取书架数据库失败：%s", exc)
        return None

    store = _new_store()
    store["migrated"] = str(meta.get(_MIGRATED_KEY) or "") == "1"
    shelves = []
    for row in rows:
        payload = dbmod.loads(row["payload"], {}) or {}
        shelves.append(
            {
                "id": str(row["id"]),
                "name": str(row["name"]),
                "kind": str(row["kind"] or "local"),
                "source": _text(payload.get("source"), 40),
                "created": _int(payload.get("created")),
                "updated": _int(row["updated_at"]) or _int(payload.get("updated")),
            }
        )
    store["shelves"] = shelves
    store["items"] = [
        item for item in (dbmod.loads(row["payload"], None) for row in items) if isinstance(item, dict)
    ]
    history = dbmod.loads(meta.get(_HISTORY_KEY), [])
    store["history"] = [entry for entry in history if isinstance(entry, dict)] if isinstance(history, list) else []
    return store


def _write_json(store: dict[str, Any]) -> None:
    """数据库不可用时的退路：沿用旧版的原子写。"""
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_name(STORE_PATH.name + ".tmp")
    payload = json.dumps(store, ensure_ascii=False, indent=1)
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, STORE_PATH)


def _write_store(store: dict[str, Any]) -> None:
    """把整份内存结构同步进 SQLite（书架、收藏项、历史）。"""
    try:
        from dumbcat.server import db as dbmod

        with dbmod.lock(), dbmod.transaction() as connection:
            connection.execute("DELETE FROM shelf")
            connection.execute("DELETE FROM shelf_item")
            shelves = [shelf for shelf in store.get("shelves") or [] if isinstance(shelf, dict)]
            connection.executemany(
                "INSERT INTO shelf (id, name, kind, position, payload, updated_at) VALUES (?,?,?,?,?,?)",
                [
                    (
                        _text(shelf.get("id"), 80),
                        _text(shelf.get("name"), MAX_NAME) or "未命名书架",
                        shelf.get("kind") if shelf.get("kind") in ("local", "source") else "local",
                        index,
                        dbmod.dumps({"source": _text(shelf.get("source"), 40), "created": _int(shelf.get("created")), "updated": _int(shelf.get("updated"))}),
                        _int(shelf.get("updated")),
                    )
                    for index, shelf in enumerate(shelves)
                ],
            )
            items = [item for item in store.get("items") or [] if isinstance(item, dict)]
            connection.executemany(
                "INSERT INTO shelf_item (shelf_id, uid, payload, remote, added, updated)"
                " VALUES (?,?,?,?,?,?)",
                [
                    (
                        _text(item.get("shelfId"), 80),
                        _text(item.get("uid"), 200),
                        dbmod.dumps(item),
                        _text(item.get("remote"), 20),
                        _int(item.get("added")),
                        _int(item.get("updated")) or _int(item.get("added")),
                    )
                    for item in items
                    if item.get("shelfId") and item.get("uid")
                ],
            )
            for key, value in (
                (_HISTORY_KEY, dbmod.dumps((store.get("history") or [])[:_history_limit()])),
                (_MIGRATED_KEY, "1" if store.get("migrated") else "0"),
            ):
                connection.execute(
                    "INSERT INTO meta (key, value) VALUES (?,?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
        return
    except Exception as exc:  # pragma: no cover - 坏盘
        log.warning("写入书架数据库失败，退回 JSON：%s", exc)
    _write_json(store)


def _load_from_disk() -> dict[str, Any]:
    """优先读 SQLite；库里没有数据时把旧版 ``shelves.json`` 迁进来。"""
    store = _load_from_db()
    if store is not None and (store["shelves"] or store["items"] or store["history"]):
        return store

    raw = _read_json(STORE_PATH)
    if not isinstance(raw, dict):
        if STORE_PATH.exists():
            log.warning("书架数据格式不对，按空书架重建：%s", STORE_PATH)
        return _new_store()
    store = _sanitize(raw)
    _write_store(store)
    try:  # 迁移完成，留个备份就行，避免下次又读一遍旧文件
        STORE_PATH.replace(STORE_PATH.with_name(STORE_PATH.name + ".bak"))
        log.info("旧版书架数据已迁移到 SQLite：%s", STORE_PATH.name)
    except OSError:
        pass
    return store


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
    rows.sort(
        key=lambda item: (
            -_int(item.get("pin")),
            -_int(item.get("added")),
            -_int(item.get("seq")),
        )
    )
    return rows


def get_item(shelf_id: str, uid: str) -> dict[str, Any] | None:
    store = load_store()
    item = _find_item(store, _text(shelf_id, 80), _text(uid, 200))
    return dict(item) if item is not None else None


def add_item(
    shelf_id: str,
    series: dict[str, Any],
    remote: str = "",
    chapters: Any = None,
    summary: Any = None,
) -> dict[str, Any]:
    """把作品加入书架；同架同 uid 已存在时补一次快照并原样返回（幂等）。"""
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
            changed = _refresh_snapshot(existing, data, chapters, summary)
            if changed:
                _write_store(store)
            return dict(existing)
        item = _new_item(shelf["id"], data, remote=remote, chapters=chapters, summary=summary)
        item["uid"] = uid
        item["seq"] = max((_int(row.get("seq")) for row in store["items"]), default=0) + 1
        store["items"].append(item)
        shelf["updated"] = _now()
        _write_store(store)
        return dict(item)


def _refresh_snapshot(
    item: dict[str, Any],
    series: dict[str, Any],
    chapters: Any,
    summary: Any,
) -> bool:
    """已有书架项补快照：只在本次带了内容、且内容确实变了时写盘。"""
    changed = False
    snapshot = _chapter_snapshot(chapters)
    if snapshot and snapshot != item.get("chapters"):
        item["chapters"] = snapshot
        changed = True
    data = series if isinstance(series, dict) else {}
    fields = _series_fields(data, item.get("uid", ""), item.get("source", ""))
    fresh = _summary_snapshot(
        {
            **fields,
            **(summary if isinstance(summary, dict) else {}),
            "uid": item.get("uid", ""),
        }
    )
    old = item.get("summary") if isinstance(item.get("summary"), dict) else {}
    if any(fresh.get(key) for key in ("description", "cover", "status", "tags")) and fresh != old:
        item["summary"] = fresh
        changed = True
    if changed:
        item["updated"] = _now()
    return changed


def refresh_item(
    shelf_id: str,
    uid: str,
    series: dict[str, Any],
    chapters: Any = None,
    summary: Any = None,
) -> dict[str, Any] | None:
    """打开详情页时把最新章节/简介写回书架快照。"""
    with _LOCK:
        store = load_store()
        item = _find_item(store, _text(shelf_id, 80), _text(uid, 200))
        if item is None:
            return None
        if _refresh_snapshot(item, series, chapters, summary):
            _write_store(store)
        return dict(item)


def pin_item(shelf_id: str, uid: str, pinned: bool = True) -> dict[str, Any] | None:
    """置顶/取消置顶。置顶项排在有 pin 的组里，pin 值大的更靠前。"""
    with _LOCK:
        store = load_store()
        item = _find_item(store, _text(shelf_id, 80), _text(uid, 200))
        if item is None:
            return None
        if pinned:
            current = max((_int(other.get("pin")) for other in store["items"]), default=0)
            item["pin"] = max(current + 1, _int(item.get("pin")) + 1, 1)
        else:
            item["pin"] = 0
        item["updated"] = _now()
        _write_store(store)
        return dict(item)


def bulk_update(
    shelf_id: str,
    uids: list[str],
    action: str,
    target_shelf_id: str = "",
) -> dict[str, Any]:
    """批量移出 / 移动 / 置顶。只动本地书架，不碰图源收藏。"""
    wanted = [_text(uid, 200) for uid in (uids or []) if _text(uid, 200)]
    if not wanted:
        raise ShelfError("没有选中任何作品")
    with _LOCK:
        store = load_store()
        shelf = _find_shelf(store, shelf_id)
        if shelf is None:
            raise ShelfError("书架不存在")
        picked = [item for item in store["items"] if item.get("uid") in wanted]
        touched = len(picked)
        if not touched:
            return {"ok": True, "count": 0, "action": action}
        if action == "remove":
            store["items"] = [item for item in store["items"] if item not in picked]
        elif action == "move":
            target = _find_shelf(store, target_shelf_id)
            if target is None:
                raise ShelfError("目标书架不存在")
            if target["id"] == shelf["id"]:
                raise ShelfError("已经在这个书架里了")
            keep: list[dict[str, Any]] = []
            for item in store["items"]:
                if item not in picked:
                    keep.append(item)
                    continue
                exists = _find_item(store, target["id"], str(item.get("uid") or ""))
                if exists is None:
                    item["shelfId"] = target["id"]
                    item["updated"] = _now()
                    keep.append(item)
            store["items"] = keep
            target["updated"] = _now()
        elif action in ("pin", "unpin"):
            pinned = action == "pin"
            current = max((_int(other.get("pin")) for other in store["items"]), default=0)
            for item in picked:
                if pinned:
                    current += 1
                    item["pin"] = current
                else:
                    item["pin"] = 0
                item["updated"] = _now()
        else:
            raise ShelfError(f"不认识的操作：{action}")
        shelf["updated"] = _now()
        _write_store(store)
        return {"ok": True, "count": touched, "action": action}


def find_item_any_shelf(uid: str) -> dict[str, Any] | None:
    """跨书架找作品（离线详情兜底用）。"""
    target = _text(uid, 200)
    if not target:
        return None
    store = load_store()
    for item in store["items"]:
        if item.get("uid") == target:
            return dict(item)
    return None


def find_item_by_source_title(source: str, title: str) -> dict[str, Any] | None:
    """历史里只有 uid 时兜底：同源同标题的已入架作品。"""
    target_source = _text(source, 40)
    target_title = _text(title, 200)
    if not target_title:
        return None
    store = load_store()
    for item in store["items"]:
        if item.get("title") != target_title:
            continue
        if target_source and item.get("source") != target_source:
            continue
        return dict(item)
    return None


def offline_detail(uid: str) -> dict[str, Any] | None:
    """书架快照拼出的详情页数据；源不可用或进程重启后靠它兜底。"""
    item = find_item_any_shelf(uid)
    if item is None:
        return None
    chapters = [dict(entry) for entry in item.get("chapters") or [] if isinstance(entry, dict)]
    summary = dict(item.get("summary") or {})
    series = {
        "uid": item.get("uid", ""),
        "title": item.get("title", ""),
        "author": item.get("author", "") or summary.get("author", ""),
        "cover": item.get("cover", "") or summary.get("cover", ""),
        "coverUrl": item.get("cover", ""),
        "description": summary.get("description", ""),
        "tags": _tags_of(item.get("tags")),
        "status": summary.get("status", ""),
        "url": item.get("url", ""),
        "nsfw": _bool(item.get("nsfw")),
        "providerName": item.get("providerName", ""),
        "source": item.get("source", ""),
        "key": item.get("key", ""),
    }
    if not chapters and not summary:
        return None
    return {"series": series, "chapters": chapters, "cached": False, "offline": True}


def refresh_all(uid: str, series: dict[str, Any], chapters: Any = None, summary: Any = None) -> int:
    """把一个作品在所有书架里的快照都刷新一遍，返回更新条数。"""
    target = _text(uid, 200)
    if not target:
        return 0
    updated = 0
    with _LOCK:
        store = load_store()
        changed = False
        for item in store["items"]:
            if item.get("uid") != target:
                continue
            if _refresh_snapshot(item, series, chapters, summary):
                updated += 1
                changed = True
        if changed:
            _write_store(store)
    return updated


def auto_add_recent(record: dict[str, Any], chapters: Any = None, summary: Any = None) -> dict[str, Any] | None:
    """阅读过的作品自动进默认书架（已在该架就只补快照）。"""
    data = record if isinstance(record, dict) else {}
    uid = _text(data.get("uid"), 200)
    if not uid:
        return None
    with _LOCK:
        store = load_store()
        shelf = _find_shelf(store, DEFAULT_SHELF_ID)
        if shelf is None:
            shelf = next((s for s in store["shelves"] if s.get("kind") == "local"), None)
        if shelf is None:
            return None
        existing = _find_item(store, shelf["id"], uid)
        if existing is not None:
            if _refresh_snapshot(existing, data, chapters, summary):
                _write_store(store)
            return dict(existing)
        item = _new_item(shelf["id"], data, remote="", chapters=chapters, summary=summary)
        item["uid"] = uid
        item["seq"] = max((_int(row.get("seq")) for row in store["items"]), default=0) + 1
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
        del store["history"][_history_limit():]
        _write_store(store)
    # 看过的作品自动进书架：重启/断网后历史里点进去仍然有章节可看
    try:
        if find_item_any_shelf(uid) is None:
            auto_add_recent(data, data.get("chapters"), data.get("summary"))
    except ShelfError:
        log.warning("自动入架失败：%s", uid)
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
    return rows[: min(count, _history_limit())]


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
