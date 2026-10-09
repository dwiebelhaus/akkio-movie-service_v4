"""Per-process fan-out of Postgres notifications (DESIGN.md §7, §13).

One dedicated LISTEN connection per API process, however many SSE clients are connected.
Subscribers get an `asyncio.Event` that is set whenever their job changes; they re-read the
job row themselves, so bursts of notifications coalesce into one read. The hub also tracks
the current dataset version for cache keys.
"""

import asyncio
import contextlib
import logging
from collections import defaultdict
from collections.abc import Iterator

from psycopg import AsyncConnection

from app.services.importer import DATASET_CHANNEL
from app.services.jobs import PROGRESS_CHANNEL

logger = logging.getLogger(__name__)


class JobEventHub:
    def __init__(self, conninfo: str):
        self._conninfo = conninfo
        self._subscribers: dict[int, set[asyncio.Event]] = defaultdict(set)
        self._task: asyncio.Task | None = None
        self.dataset_version: int | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="job-event-hub")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    @contextlib.contextmanager
    def subscribe(self, job_id: int) -> Iterator[asyncio.Event]:
        event = asyncio.Event()
        self._subscribers[job_id].add(event)
        try:
            yield event
        finally:
            subs = self._subscribers.get(job_id)
            if subs is not None:
                subs.discard(event)
                if not subs:
                    del self._subscribers[job_id]

    def _wake(self, job_id: int) -> None:
        for event in self._subscribers.get(job_id, ()):
            event.set()

    def _wake_all(self) -> None:
        for subs in self._subscribers.values():
            for event in subs:
                event.set()

    def _set_version(self, version: int) -> None:
        if self.dataset_version is None or version > self.dataset_version:
            self.dataset_version = version

    async def _run(self) -> None:
        backoff = 0.5
        while True:
            try:
                async with await AsyncConnection.connect(self._conninfo, autocommit=True) as conn:
                    await conn.execute(f"listen {PROGRESS_CHANNEL}")
                    await conn.execute(f"listen {DATASET_CHANNEL}")
                    cur = await conn.execute("select version from dataset_state")
                    row = await cur.fetchone()
                    if row:
                        self._set_version(row[0])
                    # Anything may have changed while we were disconnected.
                    self._wake_all()
                    backoff = 0.5
                    async for note in conn.notifies():
                        try:
                            value = int(note.payload)
                        except ValueError:
                            continue
                        if note.channel == PROGRESS_CHANNEL:
                            self._wake(value)
                        elif note.channel == DATASET_CHANNEL:
                            self._set_version(value)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("notification listener lost (%s); reconnecting in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)
