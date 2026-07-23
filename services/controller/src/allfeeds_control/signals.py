from __future__ import annotations

import asyncio
import logging
import threading

import psycopg

from .db import validate_schema
from .settings import Settings

logger = logging.getLogger(__name__)


class TaskSignal:
    """LISTEN/NOTIFY bridge using bounded wake tokens instead of broadcast events."""

    def __init__(self, max_tokens: int = 256):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tokens: asyncio.Queue[None] | None = None
        self._max_tokens = max_tokens
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, loop: asyncio.AbstractEventLoop, settings: Settings) -> None:
        self._loop = loop
        self._tokens = asyncio.Queue(maxsize=self._max_tokens)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._listen,
            args=(settings,),
            name="allfeeds-task-listener",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def notify_local(self, count: int = 1) -> None:
        if not self._loop:
            return
        self._loop.call_soon_threadsafe(self._add_tokens, count)

    def _add_tokens(self, count: int = 1) -> None:
        if not self._tokens:
            return
        for _ in range(max(1, min(count, self._max_tokens))):
            try:
                self._tokens.put_nowait(None)
            except asyncio.QueueFull:
                break

    async def wait(self, timeout: float) -> None:
        if not self._tokens:
            await asyncio.sleep(min(timeout, 1))
            return
        try:
            await asyncio.wait_for(self._tokens.get(), timeout=max(0.05, timeout))
        except TimeoutError:
            pass

    def _listen(self, settings: Settings) -> None:
        schema = validate_schema(settings.database_schema)
        while not self._stop.is_set():
            try:
                with psycopg.connect(settings.database_url, autocommit=True) as conn:
                    conn.execute(f'SET search_path TO "{schema}", public')
                    conn.execute("LISTEN allfeeds_tasks")
                    while not self._stop.is_set():
                        for notify in conn.notifies(timeout=2, stop_after=100):
                            count = 1
                            try:
                                count = max(1, int(notify.payload or "1"))
                            except ValueError:
                                pass
                            self.notify_local(count)
            except Exception:
                logger.exception("task signal listener disconnected")
                self._stop.wait(2)
