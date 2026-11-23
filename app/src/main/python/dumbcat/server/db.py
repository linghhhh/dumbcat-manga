"""SQLite 持久层：作品 / 章节 / 章节图片 / 书架 / 进度 / 下载任务。

在 P1 之前，``store.py`` 是一个纯内存 ``OrderedDict``，进程一重启（或者容量
淘汰、图源被关掉）书架上的条目、历史记录、章节列表就全丢了——这正是「连不
到图源就打不开」和「历史要重看一遍」的根因。

这里给所有需要活过进程重启的数据建表，``Store`` 退化成「DB 上面的读缓存」：

- 写：先进内存，再落库（写穿）。库写失败只记日志，不影响本次请求。
- 读：先查内存 LRU，未命中再查库并回填；库里的 TTL 仍按原来的语义生效。
- 单连接 + 全局 ``RLock``：数据量只有几千本，并发来自 uvicorn 的线程池和下载
  worker，WAL + 单连接足够，不引入连接池，避免 sqlite 的多线程坑。
- 所有 dict/list 一律以 JSON TEXT 存，字段结构与图源返回保持一致，以后加字
  段不用改表。

表结构版本写在 ``PRAGMA user_version`` 里，升级时按版本号补迁移。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("dumbcat.db")

#: 当前表结构版本（每次改表都 +1，并在 _migrate 里补对应分支）
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS series (
    uid           TEXT PRIMARY KEY,
    provider      TEXT NOT NULL,
    provider_name TEXT NOT NULL DEFAULT '',
    base_url      TEXT NOT NULL DEFAULT '',
    nsfw          INTEGER NOT NULL DEFAULT 0,
    raw           TEXT NOT NULL,
    public        TEXT NOT NULL,
    created_at    REAL NOT NULL,
    chapters_at   REAL NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_series_updated ON series(updated_at DESC);

CREATE TABLE IF NOT EXISTS chapters (
    series_uid TEXT NOT NULL,
    position   INTEGER NOT NULL,
    uid        TEXT NOT NULL,
    public     TEXT NOT NULL,
    raw        TEXT,
    PRIMARY KEY (series_uid, position)
);
CREATE INDEX IF NOT EXISTS idx_chapters_uid ON chapters(uid);

CREATE TABLE IF NOT EXISTS chapter_pages (
    chapter_uid TEXT PRIMARY KEY,
    series_uid  TEXT NOT NULL,
    referer     TEXT NOT NULL DEFAULT '',
    items       TEXT NOT NULL,
    at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS shelf (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'local',
    position   INTEGER NOT NULL DEFAULT 0,
    payload    TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS shelf_item (
    shelf_id TEXT NOT NULL,
    uid      TEXT NOT NULL,
    payload  TEXT NOT NULL,
    remote   TEXT NOT NULL DEFAULT '',
    added    REAL NOT NULL,
    updated  REAL NOT NULL,
    PRIMARY KEY (shelf_id, uid)
);
CREATE INDEX IF NOT EXISTS idx_shelf_item_uid ON shelf_item(uid);

CREATE TABLE IF NOT EXISTS progress (
    series_uid   TEXT PRIMARY KEY,
    chapter_uid  TEXT NOT NULL,
    page         INTEGER NOT NULL DEFAULT 0,
    offset_ratio REAL NOT NULL DEFAULT 0,
    updated_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS download_task (
    id         TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_download_state ON download_task(state, updated_at);
"""

_LOCK = threading.RLock()
_conn: sqlite3.Connection | None = None
#: 当前事务嵌套深度（0 = 不在事务里）
_depth = 0


# ---------------------------------------------------------------------------
# 序列化小工具
# ---------------------------------------------------------------------------
def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def loads(text: Any, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------
def db_path() -> Path:
    """库文件路径；延迟导入 config，避免模块级循环依赖。"""
    from dumbcat.server.config import DB_PATH

    path = Path(DB_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def lock() -> threading.RLock:
    return _LOCK


def conn() -> sqlite3.Connection:
    """取全局连接（首次调用时建库建表）。调用方必须持有 :func:`lock`。"""
    global _conn
    with _LOCK:
        if _conn is None:
            path = db_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                str(path),
                check_same_thread=False,
                timeout=30.0,
                isolation_level=None,  # 自己管事务
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            _migrate(connection)
            _conn = connection
            log.info("sqlite 就绪：%s", path)
        return _conn


def _migrate(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0] or 0)
    if version == 0:
        connection.executescript(_SCHEMA)
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        log.info("sqlite 建表完成，schema=%s", SCHEMA_VERSION)
        return
    if version < SCHEMA_VERSION:
        # 以后的迁移写在这里：if version < 2: ...
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        log.info("sqlite 升级 schema %s → %s", version, SCHEMA_VERSION)


def close() -> None:
    """测试用：关掉连接，下次 conn() 会重新建。"""
    global _conn
    with _LOCK:
        if _conn is not None:
            try:
                _conn.close()
            except sqlite3.Error:
                pass
            _conn = None


# ---------------------------------------------------------------------------
# 查询封装
# ---------------------------------------------------------------------------
def execute(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
    with _LOCK:
        return conn().execute(sql, params)


def executemany(sql: str, rows: list[tuple[Any, ...]]) -> None:
    with _LOCK:
        conn().executemany(sql, rows)


def query_all(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with _LOCK:
        return list(conn().execute(sql, params).fetchall())


def query_one(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    with _LOCK:
        return conn().execute(sql, params).fetchone()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """批量写。可嵌套（内层不再 BEGIN，交给最外层提交）。"""
    global _depth
    with _LOCK:
        connection = conn()
        outer = _depth == 0
        if outer:
            connection.execute("BEGIN IMMEDIATE")
        _depth += 1
        try:
            yield connection
        except BaseException:
            if outer:
                connection.execute("ROLLBACK")
            raise
        else:
            if outer:
                connection.execute("COMMIT")
        finally:
            _depth -= 1


def vacuum() -> None:
    with _LOCK:
        conn().execute("VACUUM")
