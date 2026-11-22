"""服务端事件总线：把后台线程里的进度推到浏览器（SSE）。

后台下载线程不能直接 await，因此 :meth:`EventBus.publish` 是同步的，内部用
``loop.call_soon_threadsafe`` 往订阅队列里塞消息。每个订阅者一个队列，慢客户端
只丢自己的消息，不会拖住其他页面。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from typing import Any


class Subscription:
    """一个 SSE 订阅。``queue`` 里是已序列化好的 ``(event, data)``。"""

    def __init__(self, bus: "EventBus", topics: set[str], maxsize: int = 512) -> None:
        self.bus = bus
        self.topics = topics
        self.queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=maxsize)
        self.closed = False

    def put(self, event: str, payload: str) -> None:
        if self.closed:
            return
        try:
            self.queue.put_nowait((event, payload))
        except asyncio.QueueFull:
            # 丢掉最旧的一条，保证界面始终拿到最新进度
            try:
                self.queue.get_nowait()
                self.queue.put_nowait((event, payload))
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    def close(self) -> None:
        self.closed = True
        self.bus.unsubscribe(self)

    async def stream(self) -> Any:
        """异步生成器，产出 SSE 文本块。"""
        # 先发一条 hello，让前端知道连上了
        yield _sse("hello", json.dumps({"time": time.time()}))
        try:
            while not self.closed:
                try:
                    event, payload = await asyncio.wait_for(self.queue.get(), 20.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _sse(event, payload)
        finally:
            self.close()


def _sse(event: str, payload: str) -> str:
    # 单行 JSON，SSE 的 data 不能含裸换行
    return f"event: {event}\ndata: {payload}\n\n"


class EventBus:
    def __init__(self, history: int = 200) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: set[Subscription] = set()
        self._lock = threading.RLock()
        self._history: deque[tuple[float, str, str]] = deque(maxlen=history)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ---------- 订阅 ----------
    def subscribe(self, topics: list[str] | None = None) -> Subscription:
        sub = Subscription(self, set(topics or ["*"]))
        with self._lock:
            self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            self._subs.discard(sub)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._history)[-limit:]
        return [
            {"time": stamp, "event": event, "data": json.loads(payload)}
            for stamp, event, payload in items
        ]

    # ---------- 发布 ----------
    def publish(self, event: str, data: dict[str, Any] | None = None,
                topic: str = "") -> None:
        payload = json.dumps(data or {}, ensure_ascii=False)
        with self._lock:
            self._history.append((time.time(), event, payload))
            subs = list(self._subs)
        if not subs:
            return
        loop = self._loop
        for sub in subs:
            if not (sub.topics & {"*", topic or event}):
                continue
            if loop is not None and loop.is_running():
                try:
                    loop.call_soon_threadsafe(sub.put, event, payload)
                    continue
                except RuntimeError:
                    pass
            # 没有 loop（单测 / 启动前）：直接塞，也许没人消费
            sub.put(event, payload)


#: 全局单例；进度、插件热重载、图源状态都走它
bus = EventBus()
