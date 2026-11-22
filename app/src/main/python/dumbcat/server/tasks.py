"""下载任务管理：串行队列 + 进度事件 + 历史记录。

界面提交的是「作品 uid + 章节 uid 列表 + 输出目录」，原始数据从 :mod:`store`
里取，所以用户选中多少章就下载多少章。任务串行执行——同时打多个站点既慢又
容易被风控，用户想快可以调工作线程数。
"""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dumbcat.loader import Downloader
from dumbcat.naming import sanitize_path_component
from dumbcat.server.config import WEB_RUNTIME, load_settings
from dumbcat.server.events import bus
from dumbcat.server.provider_shim import PluginProviderShim
from dumbcat.server.registry import SourceError, registry
from dumbcat.server.store import SeriesRecord, series_of_chapter, store

HISTORY_PATH = WEB_RUNTIME / "downloads.json"
MAX_HISTORY = 200


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

    # ---------- 生命周期 ----------
    def start_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop.clear()
            self._worker = threading.Thread(
                target=self._run, name="download-worker", daemon=True
            )
            self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)
        with self._lock:
            for task in self._tasks.values():
                task.cancel_event.set()

    # ---------- 提交 ----------
    def _resolve(self, series_uid: str, chapter_uids: list[str]) -> tuple[
        SeriesRecord, list[dict[str, Any]], str, str
    ]:
        record = store.get_series(series_uid)
        if record is None:
            raise SourceError("作品数据已过期，请返回列表重新点开")
        adapter = registry.get(record.provider)
        if adapter is None:
            raise SourceError(f"图源不可用：{record.provider}")
        chapters: list[dict[str, Any]] = []
        for chapter_uid in chapter_uids:
            raw = store.find_chapter(record.uid, chapter_uid)
            public = store.find_chapter_public(record.uid, chapter_uid)
            if raw is None:
                continue
            item = dict(raw)
            if public:
                # 归一化字段回填，让旧下载器的目录命名 / 付费判断都能认出来
                item.setdefault("number", public.get("number") or (public.get("index", 0) + 1))
                if not item.get("title") and public.get("title"):
                    item["title"] = public["title"]
                item["is_locked"] = bool(public.get("locked"))
                item["locked"] = bool(public.get("locked"))
            item["_provider"] = record.provider
            item["_source_label"] = record.provider_name
            chapters.append(item)
        if not chapters:
            raise SourceError("没有可下载的章节（可能没选中，或数据已过期）")
        return record, chapters, adapter.key, adapter.name

    def submit(
        self,
        series_uid: str,
        chapter_uids: list[str],
        output_dir: str = "",
        workers: int | None = None,
    ) -> dict[str, Any]:
        record, chapters, provider_key, provider_name = self._resolve(series_uid, chapter_uids)
        settings = load_settings()
        out = Path(output_dir or settings.output_dir).expanduser()
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SourceError(f"输出目录不可写：{exc}") from exc
        task = DownloadTask(
            id=uuid.uuid4().hex[:12],
            series_uid=series_uid,
            chapter_uids=list(chapter_uids),
            output_dir=str(out),
            title=str(record.public.get("title") or ""),
            provider=provider_key,
            provider_name=provider_name,
        )
        with self._lock:
            self._tasks[task.id] = task
            self._order.append(task.id)
            if len(self._order) > 400:
                for stale in self._order[:-400]:
                    if self._tasks.get(stale) and self._tasks[stale].state in {"done", "error", "cancelled"}:
                        self._tasks.pop(stale, None)
                self._order = self._order[-400:]
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
            try:
                self._execute(task)
            except Exception as exc:  # noqa: BLE001 - 任务失败不能打死 worker
                task.state = "error"
                task.message = f"{type(exc).__name__}: {exc}"
                task.finished_at = time.time()
                self._publish(task)
                self._remember(task)
            finally:
                self._current = ""
        self._current = ""

    def _execute(self, task: DownloadTask) -> None:
        settings = load_settings()
        record, chapters, provider_key, _ = self._resolve(task.series_uid, task.chapter_uids)
        task.state = "running"
        task.started_at = time.time()
        task.message = "准备下载…"
        self._publish(task)

        adapter = registry.get(provider_key)
        if adapter is None:
            raise SourceError(f"图源不可用：{provider_key}")
        if adapter.info.kind == "plugin":
            provider: Any = PluginProviderShim(adapter)
            series = dict(record.raw)
            series["_provider"] = provider_key
            series["_source_label"] = adapter.name
            if not series.get("title"):
                series["title"] = record.public.get("title", "")
        else:
            provider = adapter.provider  # type: ignore[attr-defined]
            series = dict(record.raw)
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
        downloader.download(
            series,
            chapters,
            output_root,
            workers=int(settings.workers),
        )
        task.finished_at = time.time()
        if task.cancel_event.is_set():
            task.state = "cancelled"
            task.message = "已停止"
        elif task.failed > 0 and task.downloaded == 0 and task.skipped == 0:
            task.state = "error"
            task.message = f"全部失败（{task.failed} 张）"
        else:
            task.state = "done"
            task.value = task.total
            task.message = (
                f"完成：{task.downloaded} 张，跳过 {task.skipped}，失败 {task.failed}"
            )
        self._publish(task)
        self._remember(task)

    # ---------- 历史 ----------
    def _remember(self, task: DownloadTask) -> None:
        try:
            HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            try:
                items = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
                if not isinstance(items, list):
                    items = []
            except (OSError, ValueError):
                items = []
            items = [item for item in items if item.get("id") != task.id]
            items.insert(0, {
                "id": task.id,
                "title": task.title,
                "provider": task.provider,
                "providerName": task.provider_name,
                "state": task.state,
                "downloaded": task.downloaded,
                "failed": task.failed,
                "skipped": task.skipped,
                "chapters": len(task.chapter_uids),
                "chapterList": task.chapter_states[:600],
                "failedChapters": [
                    item.get("label") or ""
                    for item in task.chapter_states
                    if item.get("state") in ("failed", "partial")
                ][:20],
                "finalDir": task.final_dir,
                "finishedAt": task.finished_at or time.time(),
            })
            HISTORY_PATH.write_text(
                json.dumps(items[:MAX_HISTORY], ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except OSError:
            pass

    @staticmethod
    def history(limit: int = 50) -> list[dict[str, Any]]:
        try:
            items = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return items[:limit] if isinstance(items, list) else []


tasks = TaskManager()
