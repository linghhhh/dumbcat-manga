"""下载任务管理：串行队列 + 进度事件 + 历史记录。

界面提交的是「作品 uid + 章节 uid 列表 + 输出目录」，原始数据从 :mod:`store`
里取，所以用户选中多少章就下载多少章。任务串行执行——同时打多个站点既慢又
容易被风控，用户想快可以调工作线程数。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dumbcat.download_paths import (
    DownloadPathError,
    probe_writable,
    resolve_output_dir,
)
from dumbcat.loader import Downloader
from dumbcat.naming import sanitize_path_component
from dumbcat.server.config import WEB_RUNTIME, load_settings
from dumbcat.server.events import bus
from dumbcat.server.provider_shim import PluginProviderShim
from dumbcat.server.registry import SourceError, registry
from dumbcat.server.store import SeriesRecord, series_of_chapter, store

log = logging.getLogger("dumbcat.tasks")

from dumbcat import notify

HISTORY_PATH = WEB_RUNTIME / "downloads.json"
PENDING_PATH = WEB_RUNTIME / "pending-tasks.json"
MAX_HISTORY = 200


def _database():
    """按需打开 SQLite；不可用时返回 None（调用方退回 JSON 文件）。"""
    try:
        from dumbcat.server import db as dbmod

        with dbmod.lock():
            dbmod.conn()
        return dbmod
    except Exception as exc:  # pragma: no cover - 极端环境
        log.warning("下载数据不能用数据库，退回 JSON：%s", exc)
        return None


@dataclass
class DownloadTask:
    id: str
    series_uid: str
    chapter_uids: list[str]
    output_dir: str
    title: str = ""
    provider: str = ""
    provider_name: str = ""
    state: str = "queued"          # queued | running | done | cancelled | error
    message: str = ""
    total: int = 1
    value: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    locked: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    log: list[str] = field(default_factory=list, repr=False)
    final_dir: str = ""
    #: 逐章状态（下载页用来显示"哪一章失败/跳过/付费"）
    chapter_states: list[dict[str, Any]] = field(default_factory=list, repr=False)
    #: 提交时冻结的作品/章节数据：进程重启后不再依赖内存仓库
    snapshot: dict[str, Any] = field(default_factory=dict, repr=False)
    #: 目录回落说明（原目录不可写、已改用别的目录时非空）
    output_note: str = ""

    def to_pending(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "seriesUid": self.series_uid,
            "chapterUids": list(self.chapter_uids),
            "outputDir": self.output_dir,
            "title": self.title,
            "provider": self.provider,
            "providerName": self.provider_name,
            "total": self.total,
            "value": self.value,
            "createdAt": self.created_at,
            "outputNote": self.output_note,
            "snapshot": self.snapshot,
        }

    @classmethod
    def from_pending(cls, raw: dict[str, Any]) -> "DownloadTask | None":
        try:
            task = cls(
                id=str(raw["id"]),
                series_uid=str(raw["seriesUid"]),
                chapter_uids=[str(uid) for uid in raw.get("chapterUids") or []],
                output_dir=str(raw.get("outputDir") or ""),
                title=str(raw.get("title") or ""),
                provider=str(raw.get("provider") or ""),
                provider_name=str(raw.get("providerName") or ""),
                total=max(1, int(raw.get("total") or 1)),
                value=int(raw.get("value") or 0),
                created_at=float(raw.get("createdAt") or time.time()),
            )
        except (KeyError, TypeError, ValueError):
            return None
        snapshot = raw.get("snapshot")
        task.snapshot = snapshot if isinstance(snapshot, dict) else {}
        task.output_note = str(raw.get("outputNote") or "")
        task.state = "queued"
        task.message = "已恢复上次未完成的下载"
        return task

    def public(self) -> dict[str, Any]:
        percent = 0
        if self.total > 0:
            percent = max(0, min(100, round(self.value * 100 / self.total)))
        if self.state == "done":
            percent = 100
        return {
            "id": self.id,
            "seriesUid": self.series_uid,
            "title": self.title,
            "provider": self.provider,
            "providerName": self.provider_name,
            "state": self.state,
            "message": self.message,
            "total": self.total,
            "value": self.value,
            "percent": percent,
            "downloaded": self.downloaded,
            "skipped": self.skipped,
            "failed": self.failed,
            "locked": self.locked,
            "outputDir": self.output_dir,
            "outputNote": self.output_note,
            "finalDir": self.final_dir,
            "chapters": len(self.chapter_uids),
            "chapterList": self.chapter_states[:600],
            "createdAt": self.created_at,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "log": self.log[-60:],
        }


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, DownloadTask] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._current: str = ""
        self._migrate_history()

    def _migrate_history(self) -> None:
        """旧版 ``downloads.json`` 里的历史搬进 SQLite，成功后留 .bak。"""
        dbmod = _database()
        if dbmod is None:
            return
        try:
            rows = dbmod.query_all("SELECT COUNT(*) AS n FROM download_task WHERE state='history'")
            if int(rows[0]["n"] or 0) > 0:
                return
            raw = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            if not isinstance(raw, list) or not raw:
                return
            with dbmod.lock(), dbmod.transaction() as connection:
                for index, item in enumerate(raw[:MAX_HISTORY]):
                    if not isinstance(item, dict) or not item.get("id"):
                        continue
                    connection.execute(
                        "INSERT OR REPLACE INTO download_task (id, state, payload, updated_at)"
                        " VALUES (?,?,?,?)",
                        (str(item["id"]), "history", dbmod.dumps(item), time.time() - index * 0.001),
                    )
            HISTORY_PATH.replace(HISTORY_PATH.with_name(HISTORY_PATH.name + ".bak"))
            log.info("旧版下载历史已迁移到 SQLite：%d 条", len(raw))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("迁移下载历史失败：%s", exc)

    # ---------- 生命周期 ----------
    def start_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop.clear()
            self._restore_pending()
            self._worker = threading.Thread(
                target=self._run, name="download-worker", daemon=True
            )
            self._worker.start()

    # ---------- 队列落盘 ----------
    def _save_pending(self) -> None:
        """把还没跑完的任务连同快照写盘，进程被杀后能接着跑。"""
        with self._lock:
            items = [
                task.to_pending()
                for task in self._tasks.values()
                if task.state in {"queued", "running"}
            ]
        dbmod = _database()
        if dbmod is not None:
            try:
                with dbmod.lock(), dbmod.transaction() as connection:
                    connection.execute("DELETE FROM download_task WHERE state='pending'")
                    connection.executemany(
                        "INSERT OR REPLACE INTO download_task (id, state, payload, updated_at)"
                        " VALUES (?,?,?,?)",
                        [
                            (str(item.get("id") or ""), "pending", dbmod.dumps(item), time.time())
                            for item in items
                            if item.get("id")
                        ],
                    )
                return
            except Exception as exc:  # pragma: no cover - 坏盘
                log.warning("写入下载队列失败，退回 JSON：%s", exc)
        try:
            PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
            if not items:
                PENDING_PATH.unlink(missing_ok=True)
                return
            tmp = PENDING_PATH.with_name(PENDING_PATH.name + ".tmp")
            tmp.write_text(
                json.dumps(items, ensure_ascii=False, default=str), encoding="utf-8"
            )
            os.replace(tmp, PENDING_PATH)
        except OSError:
            pass

    def _restore_pending(self) -> None:
        raw: Any = []
        dbmod = _database()
        if dbmod is not None:
            try:
                raw = [
                    dbmod.loads(row["payload"], None)
                    for row in dbmod.query_all(
                        "SELECT payload FROM download_task WHERE state='pending'"
                        " ORDER BY updated_at DESC"
                    )
                ]
            except Exception as exc:  # pragma: no cover
                log.warning("读取下载队列失败：%s", exc)
                raw = []
        if not raw:
            try:
                raw = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = []
        if not isinstance(raw, list):
            return
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            task = DownloadTask.from_pending(entry)
            if task is None:
                continue
            with self._lock:
                if task.id in self._tasks:
                    continue
                self._tasks[task.id] = task
                self._order.append(task.id)
            self._queue.put(task.id)
            try:
                # 恢复的任务重新广播一次，界面刷新就能看到
                self._publish(task)
            except Exception:  # noqa: BLE001 - 广播失败不影响恢复
                pass

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)
        with self._lock:
            for task in self._tasks.values():
                task.cancel_event.set()

    # ---------- 提交 ----------
    def _resolve(
        self,
        series_uid: str,
        chapter_uids: list[str],
        snapshot: dict[str, Any] | None = None,
    ) -> tuple[SeriesRecord, list[dict[str, Any]], str, str]:
        snap = snapshot if isinstance(snapshot, dict) else {}
        record = store.get_series(series_uid)
        snap_series = snap.get("series") if isinstance(snap.get("series"), dict) else {}
        if record is not None:
            provider_key = record.provider
            provider_name = record.provider_name
            raw_series = dict(record.raw)
            public = dict(record.public)
        elif snap_series:
            raw_series = dict(snap_series)
            provider_key = str(
                snap.get("provider")
                or raw_series.get("_provider")
                or raw_series.get("source")
                or ""
            )
            provider_name = str(snap.get("providerName") or "")
            public = dict(raw_series)
        else:
            raise SourceError("作品数据已过期，请返回列表重新点开")
        adapter = registry.get(provider_key)
        if adapter is None:
            raise SourceError(f"图源不可用：{provider_key}")
        snap_chapters = snap.get("chapters") if isinstance(snap.get("chapters"), list) else []
        chapters: list[dict[str, Any]] = []
        for chapter_uid in chapter_uids:
            raw = store.find_chapter(series_uid, chapter_uid) if record is not None else None
            pub = (
                store.find_chapter_public(series_uid, chapter_uid)
                if record is not None
                else None
            )
            if raw is None:
                # 内存里没有：用提交时冻结的章节快照顶上（重启/掉线也能续下）
                pub = next(
                    (c for c in snap_chapters if isinstance(c, dict) and c.get("uid") == chapter_uid),
                    None,
                )
                if pub is None:
                    continue
                raw = dict(pub)
            item = dict(raw)
            if pub:
                # 归一化字段回填，让旧下载器的目录命名 / 付费判断都能认出来
                item.setdefault("number", pub.get("number") or (pub.get("index", 0) + 1))
                if not item.get("title") and pub.get("title"):
                    item["title"] = pub["title"]
                item["is_locked"] = bool(pub.get("locked"))
                item["locked"] = bool(pub.get("locked"))
            item["_provider"] = provider_key
            item["_source_label"] = provider_name or adapter.name
            chapters.append(item)
        if not chapters:
            raise SourceError("没有可下载的章节（可能没选中，或数据已过期）")
        if record is None:
            record = SeriesRecord(
                uid=series_uid,
                provider=provider_key,
                provider_name=provider_name or adapter.name,
                raw=raw_series,
                public=public,
            )
        return record, chapters, provider_key, provider_name or adapter.name

    def submit(
        self,
        series_uid: str,
        chapter_uids: list[str],
        output_dir: str = "",
        workers: int | None = None,
        series_snapshot: dict[str, Any] | None = None,
        chapters_snapshot: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        snap = {
            "series": dict(series_snapshot or {}),
            "chapters": [dict(c) for c in (chapters_snapshot or []) if isinstance(c, dict)],
            "provider": (series_snapshot or {}).get("source") if series_snapshot else "",
            "providerName": (series_snapshot or {}).get("providerName") if series_snapshot else "",
        }
        record, chapters, provider_key, provider_name = self._resolve(
            series_uid, chapter_uids, snap
        )
        settings = load_settings()
        try:
            resolved = resolve_output_dir(output_dir, settings)
        except DownloadPathError as exc:
            raise SourceError(str(exc)) from exc
        out = resolved.path
        task = DownloadTask(
            id=uuid.uuid4().hex[:12],
            series_uid=series_uid,
            chapter_uids=list(chapter_uids),
            output_dir=str(out),
            title=str(record.public.get("title") or ""),
            provider=provider_key,
            provider_name=provider_name,
        )
        task.output_note = resolved.note
        # 冻结成任务快照：排队期间进程重启 / 内存仓库淘汰都不影响下载
        task.snapshot = {
            "provider": provider_key,
            "providerName": provider_name,
            "series": dict(record.raw),
            "chapters": [dict(item) for item in chapters],
        }
        with self._lock:
            self._tasks[task.id] = task
            self._order.append(task.id)
            if len(self._order) > 400:
                for stale in self._order[:-400]:
                    if self._tasks.get(stale) and self._tasks[stale].state in {"done", "error", "cancelled"}:
                        self._tasks.pop(stale, None)
                self._order = self._order[-400:]
        self._save_pending()
        self._queue.put(task.id)
        self._publish(task)
        self.start_worker()
        return task.public()

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            raise SourceError("任务不存在")
        if task.state in {"done", "error", "cancelled"}:
            return task.public()
        task.cancel_event.set()
        task.message = "正在停止…"
        self._publish(task)
        return task.public()

    def clear_finished(self) -> int:
        with self._lock:
            finished = [
                key for key, task in self._tasks.items()
                if task.state in {"done", "error", "cancelled"}
            ]
            for key in finished:
                self._tasks.pop(key, None)
            self._order = [key for key in self._order if key in self._tasks]
        return len(finished)

    # ---------- 查询 ----------
    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            items = [self._tasks[key] for key in self._order if key in self._tasks]
        items.sort(key=lambda task: task.created_at, reverse=True)
        return [task.public() for task in items[:limit]]

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
        return task.public() if task else None

    def stats(self) -> dict[str, Any]:
        with self._lock:
            running = sum(1 for t in self._tasks.values() if t.state == "running")
            queued = sum(1 for t in self._tasks.values() if t.state == "queued")
        return {"running": running, "queued": queued, "current": self._current}

    # ---------- 执行 ----------
    def _publish(self, task: DownloadTask, event: str = "download") -> None:
        bus.publish(event, task.public(), topic="downloads")
        if task.state == "running":
            notify.progress(task.value, task.total, task.message or task.title)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                task_id = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if task_id is None:
                break
            with self._lock:
                task = self._tasks.get(task_id)
            if task is None or task.cancel_event.is_set():
                continue
            self._current = task.id
            notify.start()
            try:
                self._execute(task)
            except Exception as exc:  # noqa: BLE001 - 任务失败不能打死 worker
                task.state = "error"
                task.message = f"{type(exc).__name__}: {exc}"
                task.finished_at = time.time()
                self._publish(task)
                self._remember(task)
                self._save_pending()
            finally:
                self._current = ""
                with self._lock:
                    idle = not any(
                        item.state in ("queued", "running") for item in self._tasks.values()
                    )
                if idle:
                    notify.stop()
        self._current = ""
        notify.stop()

    def _execute(self, task: DownloadTask) -> None:
        settings = load_settings()
        snapshot = task.snapshot if isinstance(task.snapshot, dict) else {}
        if snapshot.get("series") and snapshot.get("chapters"):
            # 提交时冻结的快照优先：不再依赖内存仓库（重启/淘汰都无妨）
            provider_key = str(snapshot.get("provider") or task.provider)
            series = dict(snapshot["series"])
            chapters = [dict(item) for item in snapshot["chapters"] if isinstance(item, dict)]
        else:
            record, chapters, provider_key, _ = self._resolve(task.series_uid, task.chapter_uids)
            series = dict(record.raw)
        task.state = "running"
        task.started_at = time.time()
        task.message = "准备下载…"
        self._publish(task)

        # 队列里等了一会儿才轮到：目录可能已经不可写（权限被撤、存储卡拔了），
        # 这时按启动时的规则重新回落一次，别让整单任务白跑。
        target = Path(task.output_dir)
        ok, reason = probe_writable(target)
        if not ok:
            try:
                resolved = resolve_output_dir("", settings)
            except DownloadPathError as exc:
                raise SourceError(
                    f"下载目录不可用：{task.output_dir}（{reason}）；{exc}"
                ) from exc
            target = resolved.path
            task.output_dir = str(target)
            task.output_note = resolved.note or (
                f"原下载目录不可用（{reason}），已改用 {target}"
            )
        if task.output_note:
            task.log.append(f"下载目录：{task.output_dir}")
            task.log.append(task.output_note)
            task.message = task.output_note
            self._publish(task)

        adapter = registry.get(provider_key)
        if adapter is None:
            raise SourceError(f"图源不可用：{provider_key}")
        if adapter.info.kind == "plugin":
            provider: Any = PluginProviderShim(adapter)
            series["_provider"] = provider_key
            series["_source_label"] = adapter.name
            if not series.get("title"):
                series["title"] = task.title or "未命名作品"
        else:
            provider = adapter.provider  # type: ignore[attr-defined]
            series["_provider"] = provider_key

        def emit(event: str, **payload: Any) -> None:
            if event == "progress":
                try:
                    task.value = int(payload.get("value") or 0)
                    task.total = max(1, int(payload.get("maximum") or 1))
                except (TypeError, ValueError):
                    pass
                task.message = str(payload.get("message") or task.message)
            elif event == "log":
                text = str(payload.get("message") or "")
                if text:
                    task.log.append(text)
                    if len(task.log) > 300:
                        del task.log[:-300]
                task.message = text or task.message
            elif event == "download_status":
                task.message = str(payload.get("message") or task.message)
            elif event == "chapters":
                items = payload.get("items")
                if isinstance(items, list):
                    task.chapter_states = [
                        dict(item) for item in items if isinstance(item, dict)
                    ][:600]
            elif event == "download_done":
                items = payload.get("chapters")
                if isinstance(items, list) and items:
                    task.chapter_states = [
                        dict(item) for item in items if isinstance(item, dict)
                    ][:600]
                task.downloaded = int(payload.get("downloaded") or 0)
                task.skipped = int(payload.get("skipped") or 0)
                task.failed = int(payload.get("failed") or 0)
                task.locked = int(payload.get("locked") or 0)
                task.total = max(task.total, task.downloaded + task.failed
                                 + task.skipped + task.locked)
            self._publish(task)

        downloader = Downloader(
            providers={provider_key: provider},
            emit=emit,
            cancel_event=task.cancel_event,
            request_id=abs(hash(task.id)) % 10_000_000,
        )
        output_root = Path(task.output_dir)
        series_name = sanitize_path_component(
            series.get("title") or "未命名作品", "未命名作品", 90
        )
        try:
            provider_root = Path(provider.output_root(output_root, series))
        except Exception:
            provider_root = output_root
        task.final_dir = str(provider_root / series_name)
        try:
            downloader.download(
                series,
                chapters,
                output_root,
                workers=int(settings.workers),
                max_images=int(settings.max_images_per_chapter or 0),
            )
        except (OSError, PermissionError) as exc:
            # 写盘失败（权限被撤、目录被删、空间满）：别只丢个 OSError 给界面
            task.state = "error"
            task.finished_at = time.time()
            task.message = f"写入下载目录失败：{task.output_dir}（{exc}）"
            self._publish(task)
            self._remember(task)
            self._save_pending()
            return
        task.finished_at = time.time()
        states = [str(item.get("state") or "") for item in task.chapter_states]
        skipped_chapters = states.count("skipped")
        empty_chapters = states.count("empty")
        locked_chapters = states.count("locked") or task.locked
        chapter_count = max(len(task.chapter_uids), len(states), 1)
        if task.cancel_event.is_set():
            task.state = "cancelled"
            task.message = "已停止"
        elif task.downloaded == 0 and task.failed == 0 and task.skipped == 0:
            # 一张都没下、也没报失败：以前直接显示「完成：0 张」，用户以为下好了。
            if skipped_chapters:
                task.state = "done"
                task.value = task.total
                task.message = f"已存在：{skipped_chapters} 章之前已经下完，没有新增图片"
            elif locked_chapters and locked_chapters >= chapter_count:
                task.state = "error"
                task.message = (
                    f"所选 {locked_chapters} 章都是付费/未解锁章节，请先在详情页解锁或登录"
                )
            elif empty_chapters:
                task.state = "error"
                task.message = (
                    f"没有取到任何图片：{empty_chapters} 章都没有可用图片"
                    "（可能需要登录，或图源接口暂时不可用）"
                )
            else:
                task.state = "error"
                task.message = "没有取到任何图片，请重试（图源可能限制下载或需要登录）"
        elif task.failed > 0 and task.downloaded == 0 and task.skipped == 0:
            task.state = "error"
            reason = next(
                (
                    line
                    for line in task.log
                    if "下载失败" in line or "失败：" in line or "HTTP" in line
                ),
                "",
            )
            task.message = f"全部失败（{task.failed} 张）"
            task.message += f"：{reason}" if reason else "，请检查网络、或回详情页重新登录图源"
        else:
            task.state = "done"
            task.value = task.total
            task.message = (
                f"完成：{task.downloaded} 张，跳过 {task.skipped}，失败 {task.failed}"
            )
            if skipped_chapters:
                task.message += f"；{skipped_chapters} 章已存在"
        self._publish(task)
        self._remember(task)
        self._save_pending()

    # ---------- 历史 ----------
    def _remember(self, task: DownloadTask) -> None:
        # 快照要留在历史里，重试才不用回头找图源；章节太多时只留失败的那几章
        snapshot = dict(task.snapshot or {})
        chapters = snapshot.get("chapters")
        if isinstance(chapters, list) and len(chapters) > 600:
            failed = {
                item.get("uid")
                for item in task.chapter_states
                if item.get("state") in ("failed", "partial")
            }
            snapshot["chapters"] = [
                item for item in chapters if isinstance(item, dict) and item.get("uid") in failed
            ][:600]
        record = {
            "id": task.id,
            "seriesUid": task.series_uid,
            "title": task.title,
            "provider": task.provider,
            "providerName": task.provider_name,
            "state": task.state,
            "downloaded": task.downloaded,
            "failed": task.failed,
            "skipped": task.skipped,
            "chapters": len(task.chapter_uids),
            "chapterUids": list(task.chapter_uids),
            "chapterList": task.chapter_states[:600],
            "failedChapters": [
                item.get("label") or ""
                for item in task.chapter_states
                if item.get("state") in ("failed", "partial")
            ][:20],
            # 失败/不完整的章节 uid：下载页的「重试失败章节」靠它
            "failedUids": [
                item.get("uid") or ""
                for item in task.chapter_states
                if item.get("state") in ("failed", "partial") and item.get("uid")
            ],
            "outputDir": task.output_dir,
            "outputNote": task.output_note,
            "snapshot": snapshot,
            "finalDir": task.final_dir,
            "finishedAt": task.finished_at or time.time(),
        }
        dbmod = _database()
        if dbmod is not None:
            try:
                with dbmod.lock(), dbmod.transaction() as connection:
                    # 同一个 id 可能还留着 state='pending' 的排队记录（submit 时写的），
                    # 只删 history 会撞主键，历史就一条都写不进去。
                    connection.execute(
                        "DELETE FROM download_task WHERE id=?", (task.id,)
                    )
                    connection.execute(
                        "INSERT OR REPLACE INTO download_task (id, state, payload, updated_at)"
                        " VALUES (?,?,?,?)",
                        (task.id, "history", dbmod.dumps(record), time.time()),
                    )
                    connection.execute(
                        "DELETE FROM download_task WHERE state='history' AND id NOT IN"
                        " (SELECT id FROM download_task WHERE state='history'"
                        "  ORDER BY updated_at DESC LIMIT ?)",
                        (MAX_HISTORY,),
                    )
                return
            except Exception as exc:  # pragma: no cover - 坏盘
                log.warning("写入下载历史失败，退回 JSON：%s", exc)
        try:
            HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            try:
                items = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
                if not isinstance(items, list):
                    items = []
            except (OSError, ValueError):
                items = []
            items = [item for item in items if item.get("id") != task.id]
            items.insert(0, record)
            HISTORY_PATH.write_text(
                json.dumps(items[:MAX_HISTORY], ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except OSError:
            pass

    # ---------- 重试 ----------

    @staticmethod
    def _history_record(task_id: str) -> dict[str, Any] | None:
        """从下载历史里取一条完整记录（含提交时冻结的快照）。"""
        dbmod = _database()
        if dbmod is not None:
            try:
                row = dbmod.query_one(
                    "SELECT payload FROM download_task WHERE id=? AND state='history'",
                    (task_id,),
                )
                if row:
                    record = dbmod.loads(row["payload"], None)
                    if isinstance(record, dict):
                        return record
            except Exception as exc:  # pragma: no cover - 坏盘
                log.warning("读取下载记录失败：%s", exc)
        try:
            items = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(items, list):
            return None
        for item in items:
            if isinstance(item, dict) and item.get("id") == task_id:
                return item
        return None

    def retry(self, task_id: str) -> dict[str, Any]:
        """只把失败/不完整的章节重新排进队列，已经下好的图会被跳过。"""
        record = self._history_record(task_id)
        if not record:
            raise SourceError("找不到这条下载记录")
        uids = [str(uid) for uid in record.get("failedUids") or [] if uid]
        if not uids:
            uids = [str(uid) for uid in record.get("chapterUids") or [] if uid]
        if not uids:
            raise SourceError("这条记录里没有可重试的章节")
        snapshot = record.get("snapshot")
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        chapter_map = {
            str(item.get("uid")): item
            for item in snapshot.get("chapters") or []
            if isinstance(item, dict) and item.get("uid")
        }
        chapters = [chapter_map[uid] for uid in uids if uid in chapter_map]
        series = snapshot.get("series") if isinstance(snapshot.get("series"), dict) else None
        if series is None:
            raise SourceError("这条记录缺少作品信息，请在作品页重新下载")
        return self.submit(
            str(record.get("seriesUid") or series.get("uid") or ""),
            uids,
            str(record.get("outputDir") or ""),
            None,
            series,
            chapters,
        )

    @staticmethod
    def history(limit: int = 50) -> list[dict[str, Any]]:
        dbmod = _database()
        if dbmod is not None:
            try:
                rows = dbmod.query_all(
                    "SELECT payload FROM download_task WHERE state='history'"
                    " ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                )
                items = [
                    item
                    for item in (dbmod.loads(row["payload"], None) for row in rows)
                    if isinstance(item, dict)
                ]
                if items:
                    return items
                # 库是空的：旧版本写库失败时把历史留在 JSON 里，这里兜底读一次
            except Exception as exc:  # pragma: no cover
                log.warning("读取下载历史失败：%s", exc)
        try:
            items = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return items[:limit] if isinstance(items, list) else []


tasks = TaskManager()
