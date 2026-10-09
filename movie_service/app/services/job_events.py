"""Per-process fan-out of Postgres notifications (DESIGN.md §7, §13).

One dedicated LISTEN connection per API process, however many SSE clients are connected.
For each watched job the hub keeps the latest snapshot: a change notification triggers one
database read, shared by every subscriber of that job, and bursts of notifications coalesce
into as few reads as possible. Subscribers get an `asyncio.Event` that is set whenever the
snapshot is refreshed.

The hub also tracks the current dataset version for cache keys. The version is `None`
whenever the listener is down (callers then read it from the database), and it is
re-read periodically, so a lost notification can't keep serving stale caches for long.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from app.services import jobs
from app.services.importer import DATASET_CHANNEL
from app.services.jobs import PROGRESS_CHANNEL

logger = logging.getLogger(__name__)


@dataclass
class _Watch:
    """Shared state for one job with at least one subscriber."""

    job: dict
    events: set[asyncio.Event] = field(default_factory=set)
    refreshed_at: float = 0.0
    task: asyncio.Task | None = None
    stale: bool = False  # a change arrived while a refresh was already running


class JobEventHub:
    def __init__(self, conninfo: str, pool: AsyncConnectionPool, version_refresh_seconds: float):
        self._conninfo = conninfo
        self._pool = pool
        self._version_refresh = version_refresh_seconds
        self._watches: dict[int, _Watch] = {}
        self._task: asyncio.Task | None = None
        self.dataset_version: int | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="job-event-hub")

    async def stop(self) -> None:
        tasks = [t for t in (self._task, *(w.task for w in self._watches.values())) if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def subscriber_count(self) -> int:
        return sum(len(w.events) for w in self._watches.values())

    @contextlib.contextmanager
    def subscribe(self, job_id: int, initial: dict) -> Iterator[asyncio.Event]:
        """Watch a job. `initial` is a snapshot the caller just read; it seeds a new watch,
        which is then refreshed once in case the job changed before the watch existed."""
        watch = self._watches.get(job_id)
        if watch is None:
            watch = self._watches[job_id] = _Watch(job=initial)
            self.refresh(job_id)
        event = asyncio.Event()
        watch.events.add(event)
        try:
            yield event
        finally:
            watch.events.discard(event)
            if not watch.events and self._watches.get(job_id) is watch:
                del self._watches[job_id]

    def snapshot(self, job_id: int) -> dict | None:
        """The latest snapshot of a watched job."""
        watch = self._watches.get(job_id)
        return watch.job if watch else None

    def refresh(self, job_id: int, *, min_age: float = 0.0) -> None:
        """Re-read a watched job (unless it was read less than `min_age` seconds ago), then
        wake its subscribers. At most one read per job runs at a time."""
        watch = self._watches.get(job_id)
        if watch is None or time.monotonic() - watch.refreshed_at < min_age:
            return
        if watch.task is not None and not watch.task.done():
            watch.stale = True
            return
        watch.task = asyncio.create_task(self._refresh(job_id, watch), name=f"job-refresh-{job_id}")

    async def _refresh(self, job_id: int, watch: _Watch) -> None:
        while True:
            watch.stale = False
            try:
                job = await jobs.get_job(self._pool, job_id)
            except Exception as exc:
                # Subscribers keep the last snapshot; their fallback poll retries.
                logger.warning("could not refresh job %s: %s", job_id, exc)
                return
            if job is not None:
                watch.job = job
            watch.refreshed_at = time.monotonic()
            for event in watch.events:
                event.set()
            if not watch.stale:
                return

    def _refresh_all(self) -> None:
        for job_id in list(self._watches):
            self.refresh(job_id)

    def _set_version(self, version: int) -> None:
        if self.dataset_version is None or version > self.dataset_version:
            self.dataset_version = version

    async def _read_version(self, conn: AsyncConnection) -> None:
        cur = await conn.execute("select version from dataset_state")
        row = await cur.fetchone()
        if row:
            self._set_version(row[0])

    async def _run(self) -> None:
        backoff = 0.5
        while True:
            try:
                async with await AsyncConnection.connect(self._conninfo, autocommit=True) as conn:
                    await conn.execute(f"listen {PROGRESS_CHANNEL}")
                    await conn.execute(f"listen {DATASET_CHANNEL}")
                    await self._read_version(conn)
                    # Anything may have changed while we were disconnected.
                    self._refresh_all()
                    backoff = 0.5
                    while True:
                        async for note in conn.notifies(timeout=self._version_refresh):
                            try:
                                value = int(note.payload)
                            except ValueError:
                                continue
                            if note.channel == PROGRESS_CHANNEL:
                                self.refresh(value)
                            elif note.channel == DATASET_CHANNEL:
                                self._set_version(value)
                        # Periodic safety net: catches a lost notification, and a dead
                        # connection fails here instead of waiting silently forever.
                        await self._read_version(conn)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Without notifications the in-memory version can go stale; until we're back,
                # callers read it from the database.
                self.dataset_version = None
                logger.warning("notification listener lost (%s); reconnecting in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)
