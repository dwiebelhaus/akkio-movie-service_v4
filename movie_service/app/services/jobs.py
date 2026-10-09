"""Job queue operations on the `jobs` table (DESIGN.md §3, §7, §10).

Every state change issues `NOTIFY job_progress, '<id>'` so SSE streams update immediately.
Notifications sent inside a transaction are delivered when it commits.
"""

import json
import time
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.domain import JobType

JOB_COLUMNS = (
    "id, type, status, progress, processed_rows, total_rows, params, result, error, "
    "created_at, started_at, finished_at"
)
QUEUE_CHANNEL = "job_queued"
PROGRESS_CHANNEL = "job_progress"


async def _notify(conn: AsyncConnection, channel: str, payload: Any) -> None:
    await conn.execute("select pg_notify(%s, %s)", (channel, str(payload)))


async def insert_job(conn: AsyncConnection, type_: JobType, params: dict) -> dict:
    """Queue a job on the caller's connection, so it can share the caller's transaction."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        f"insert into jobs (type, params) values (%s, %s) returning {JOB_COLUMNS}",
        (type_.value, json.dumps(params)),
    )
    job = await cur.fetchone()
    await _notify(conn, QUEUE_CHANNEL, job["id"])
    return job


async def create_job(pool: AsyncConnectionPool, type_: JobType, params: dict) -> dict:
    async with pool.connection() as conn:
        return await insert_job(conn, type_, params)


async def get_job(pool: AsyncConnectionPool, job_id: int) -> dict | None:
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(f"select {JOB_COLUMNS} from jobs where id = %s", (job_id,))
        return await cur.fetchone()


async def claim_next_job(pool: AsyncConnectionPool) -> dict | None:
    """Atomically move the oldest queued job to `running`; safe across many workers."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            f"""
            update jobs
               set status = 'running', started_at = now(), heartbeat_at = now()
             where id = (select id from jobs
                          where status = 'queued'
                          order by created_at, id
                          for update skip locked
                          limit 1)
            returning {JOB_COLUMNS}
            """
        )
        job = await cur.fetchone()
        if job:
            await _notify(conn, PROGRESS_CHANNEL, job["id"])
    return job


async def report_progress(
    pool: AsyncConnectionPool, job_id: int, progress: float, processed_rows: int | None
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "update jobs set progress = %s, processed_rows = %s, heartbeat_at = now()"
            " where id = %s and status = 'running'",
            (min(max(progress, 0.0), 1.0), processed_rows, job_id),
        )
        await _notify(conn, PROGRESS_CHANNEL, job_id)


async def heartbeat(pool: AsyncConnectionPool, job_id: int) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "update jobs set heartbeat_at = now() where id = %s and status = 'running'", (job_id,)
        )


async def mark_succeeded(
    conn: AsyncConnection, job_id: int, result: dict, processed_rows: int | None
) -> None:
    """Finish a job; pass the connection so it can share the caller's transaction."""
    await conn.execute(
        """
        update jobs
           set status = 'succeeded', progress = 1, processed_rows = %s, total_rows = %s,
               result = %s, finished_at = clock_timestamp()
         where id = %s
        """,
        (processed_rows, processed_rows, json.dumps(result), job_id),
    )
    await _notify(conn, PROGRESS_CHANNEL, job_id)


async def mark_failed(pool: AsyncConnectionPool, job_id: int, error: str, result: dict | None = None) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "update jobs set status = 'failed', error = %s, result = %s, finished_at = now()"
            " where id = %s",
            (error, json.dumps(result) if result is not None else None, job_id),
        )
        await _notify(conn, PROGRESS_CHANNEL, job_id)


async def requeue(pool: AsyncConnectionPool, job_id: int) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "update jobs set status = 'queued', progress = 0, processed_rows = null,"
            " started_at = null, heartbeat_at = null where id = %s and status = 'running'",
            (job_id,),
        )
        await _notify(conn, PROGRESS_CHANNEL, job_id)
        await _notify(conn, QUEUE_CHANNEL, job_id)


async def fail_stale_jobs(pool: AsyncConnectionPool, stale_seconds: float) -> list[dict]:
    """Fail running jobs whose worker stopped heartbeating. Returns the failed jobs."""
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            update jobs
               set status = 'failed', error = 'Worker stopped responding', finished_at = now()
             where status = 'running'
               and heartbeat_at < now() - make_interval(secs => %s)
            returning id, params
            """,
            (stale_seconds,),
        )
        stale = await cur.fetchall()
        for job in stale:
            await _notify(conn, PROGRESS_CHANNEL, job["id"])
    return stale


class ProgressReporter:
    """Throttled progress writes (at most one per interval) on their own connection,
    so they are visible while the job's own transaction is still open."""

    def __init__(self, pool: AsyncConnectionPool, job_id: int, interval: float):
        self._pool = pool
        self._job_id = job_id
        self._interval = interval
        self._last = 0.0

    async def update(self, progress: float, processed_rows: int, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last < self._interval:
            return
        self._last = now
        await report_progress(self._pool, self._job_id, progress, processed_rows)
