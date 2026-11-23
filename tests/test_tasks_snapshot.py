"""下载任务：快照兜底、队列落库、失败章节重试。

不真的下载：图源用假 adapter 顶住，任务只验证「解析出要下的章节」和
「历史记录能再排一次队」。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dumbcat.server import db, tasks
from dumbcat.server.registry import SourceError


class FakeAdapter:
    name = "假图源"
    kind = "plugin"


@pytest.fixture(autouse=True)
def fake_registry(monkeypatch):
    monkeypatch.setattr(tasks.registry, "get", lambda key: FakeAdapter() if key == "qimanga" else None)
    # 测试里不起真 worker：否则后台线程会改写任务状态、清掉 pending 行
    monkeypatch.setattr(tasks.TaskManager, "start_worker", lambda self: None)
    yield


def _snapshot(uid: str = "qimanga.t_task") -> dict:
    return {
        "provider": "qimanga",
        "providerName": "奇漫",
        "series": {"uid": uid, "title": "快照作品", "source": "qimanga", "url": f"https://example.test/s/{uid}"},
        "chapters": [
            {"uid": f"{uid}.c1", "title": "第 1 话", "number": 1, "index": 0, "locked": False},
            {"uid": f"{uid}.c2", "title": "第 2 话", "number": 2, "index": 1, "locked": False},
        ],
    }


def test_resolve_uses_snapshot_without_store() -> None:
    manager = tasks.TaskManager()
    record, chapters, provider_key, provider_name = manager._resolve(
        "qimanga.t_task",
        ["qimanga.t_task.c1", "qimanga.t_task.c2"],
        _snapshot(),
    )
    assert record.uid == "qimanga.t_task"
    assert provider_key == "qimanga"
    assert provider_name == "奇漫"
    assert [item["uid"] for item in chapters] == ["qimanga.t_task.c1", "qimanga.t_task.c2"]
    assert chapters[0]["number"] == 1
    assert chapters[0]["_provider"] == "qimanga"


def test_resolve_without_snapshot_or_store_errors() -> None:
    manager = tasks.TaskManager()
    with pytest.raises(SourceError) as info:
        manager._resolve("qimanga.missing", ["qimanga.missing.c1"], None)
    assert "过期" in str(info.value)


def test_retry_only_failed_chapters() -> None:
    manager = tasks.TaskManager()
    uid = "qimanga.t_retry"
    task = tasks.DownloadTask(
        id="task-retry-1",
        series_uid=uid,
        chapter_uids=[f"{uid}.c1", f"{uid}.c2"],
        output_dir="",
        title="快照作品",
        provider="qimanga",
        provider_name="奇漫",
        state="done",
        snapshot=_snapshot(uid),
    )
    task.chapter_states = [
        {"uid": f"{uid}.c1", "state": "done", "title": "第 1 话"},
        {"uid": f"{uid}.c2", "state": "failed", "title": "第 2 话"},
    ]
    manager._remember(task)

    record = manager._history_record(task.id)
    assert record is not None
    assert record["failedUids"] == [f"{uid}.c2"]
    assert record["seriesUid"] == uid

    submitted = manager.retry(task.id)
    assert submitted["chapters"] == 1, "只重排失败的那一章"
    queued = manager._tasks[submitted["id"]]
    assert queued.chapter_uids == [f"{uid}.c2"]


def test_retry_missing_task_errors() -> None:
    manager = tasks.TaskManager()
    with pytest.raises(SourceError) as info:
        manager.retry("task-does-not-exist")
    assert "找不到" in str(info.value)


def test_pending_queue_survives_restart() -> None:
    manager = tasks.TaskManager()
    uid = "qimanga.t_pending"
    task = tasks.DownloadTask(
        id="task-pending-1",
        series_uid=uid,
        chapter_uids=[f"{uid}.c1"],
        output_dir="",
        title="快照作品",
        provider="qimanga",
        provider_name="奇漫",
        state="queued",
        snapshot=_snapshot(uid),
    )
    manager._tasks[task.id] = task
    manager._order.append(task.id)
    manager._save_pending()

    # 换一个实例，模拟重启后恢复
    fresh = tasks.TaskManager()
    fresh._restore_pending()
    restored = [row for row in fresh.list() if row["id"] == "task-pending-1"]
    assert restored, "队列应该从库里恢复"
    assert restored[0]["state"] == "queued"

    # 收尾：别让恢复出来的任务真的跑起来
    fresh._tasks.clear()
    fresh._order.clear()
    manager._tasks.clear()
    manager._order.clear()
    manager._save_pending()
