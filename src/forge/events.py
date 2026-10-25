"""In-process event bus. The daemon fans events out to TUI clients over WebSocket; a ring buffer backs /events."""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
from typing import Any

log = logging.getLogger("forge.events")


class EventBus:
    def __init__(self, maxlen: int = 2000):
        self.buffer: collections.deque[dict] = collections.deque(maxlen=maxlen)
        self._subs: set[asyncio.Queue] = set()
        self._seq = 0
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def emit(self, kind: str, /, **data: Any) -> dict:
        self._seq += 1
        ev = {"seq": self._seq, "ts": time.time(), **{k: v for k, v in data.items() if v is not None}, "kind": kind}
        self.buffer.append(ev)
        log.debug("event %s", json.dumps(ev, default=str)[:300])
        for q in list(self._subs):
            self._put(q, ev)
        return ev

    def _put(self, q: asyncio.Queue, ev: dict) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if self._loop and running is not self._loop:
            self._loop.call_soon_threadsafe(self._put_nowait, q, ev)  # emitted from a worker thread
        else:
            self._put_nowait(q, ev)

    @staticmethod
    def _put_nowait(q: asyncio.Queue, ev: dict) -> None:
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(ev)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def since(self, seq: int) -> list[dict]:
        return [e for e in self.buffer if e["seq"] > seq]
