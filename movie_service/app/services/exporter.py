"""Export job: stream the whole database as a gzipped CSV in the import format (DESIGN.md §9, §13).

The export reads one consistent snapshot (REPEATABLE READ), streams `COPY ... TO STDOUT`
through gzip into storage with constant memory, and publishes the file atomically. Exports are
reused while the dataset version is unchanged.
"""

import asyncio
import gzip
import logging
import time

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.domain import JobType
from app.services import jobs
from app.services.storage import Storage

logger = logging.getLogger(__name__)

_REQUEST_LOCK_ID = 0x6578706F  # serializes "reuse or create" decisions for exports
_WRITE_CHUNK = 1024 * 1024

# Same columns as the import format, so an export can be re-imported unchanged.
_COPY_EXPORT = """
copy (
    select m.title as movie_name, m.year, mg.genres, m.rating  -- null genres -> empty field
      from movies m
      left join (select x.movie_id, string_agg(g.name, ', ' order by g.name) as genres
                   from movie_genres x
                   join genres g on g.id = x.genre_id
                  group by x.movie_id) mg on mg.movie_id = m.id
     order by m.id
) to stdout with (format csv, header)
"""

_FIND_REUSABLE = f"""
select {jobs.JOB_COLUMNS}
  from jobs
 where type = 'export'
   and ((status = 'succeeded' and (result->>'dataset_version')::bigint = %(version)s)
        or (status in ('queued', 'running') and (params->>'dataset_version')::bigint = %(version)s))
 order by id desc
"""


def export_key(job_id: int) -> str:
    return f"exports/{job_id}.csv.gz"


def download_path(job_id: int) -> str:
    return f"/api/v1/exports/{job_id}/file"


async def request_export(pool: AsyncConnectionPool, storage: Storage, version: int) -> tuple[dict, bool]:
    """Return (job, created): an export for this dataset version that is finished (with its
    file present) or in progress, or else a newly queued one."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("select pg_advisory_xact_lock(%s)", (_REQUEST_LOCK_ID,))
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(_FIND_REUSABLE, {"version": version})
        for job in await cur.fetchall():
            if job["status"] != "succeeded" or await asyncio.to_thread(storage.exists, export_key(job["id"])):
                return job, False
        return await jobs.insert_job(conn, JobType.EXPORT, {"dataset_version": version}), True


async def run_export(pool: AsyncConnectionPool, storage: Storage, settings: Settings, job: dict) -> dict:
    """Run one export job to completion and mark it succeeded. Raises on failure."""
    job_id = job["id"]
    final_key = export_key(job_id)
    part_key = f"{final_key}.part"
    progress = jobs.ProgressReporter(pool, job_id, settings.progress_interval_seconds)
    started = time.monotonic()

    raw = await asyncio.to_thread(storage.open_write, part_key)
    gz = gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6)
    rows = 0
    try:
        async with pool.connection() as conn, conn.transaction():
            # One consistent snapshot for the count, the version and the rows.
            await conn.execute("set transaction isolation level repeatable read, read only")
            cur = await conn.execute(
                "select (select version from dataset_state), (select count(*) from movies)"
            )
            version, total = await cur.fetchone()
            await progress.update(0, 0, force=True)

            buffer: list[bytes] = []
            buffered = 0
            async with conn.cursor().copy(_COPY_EXPORT) as copy:
                async for data in copy:  # one CSV line per chunk, header first
                    buffer.append(bytes(data))
                    buffered += len(data)
                    rows += 1
                    if buffered >= _WRITE_CHUNK:
                        await asyncio.to_thread(gz.write, b"".join(buffer))
                        buffer.clear()
                        buffered = 0
                        written = rows - 1
                        await progress.update(written / max(total, 1), written)
            if buffer:
                await asyncio.to_thread(gz.write, b"".join(buffer))
        await asyncio.to_thread(gz.close)
        await asyncio.to_thread(raw.close)
        await asyncio.to_thread(storage.move, part_key, final_key)
    except BaseException:
        await asyncio.to_thread(gz.close)
        await asyncio.to_thread(raw.close)
        await asyncio.to_thread(storage.delete, part_key)
        raise

    movies = rows - 1  # minus the header line
    result = {
        "rows": movies,
        "bytes": await asyncio.to_thread(storage.size, final_key),
        "dataset_version": version,
        "download_url": download_path(job_id),
    }
    async with pool.connection() as conn:
        await jobs.mark_succeeded(conn, job_id, result, movies)
    await _delete_older_exports(pool, storage, job_id)
    logger.info("export job %s finished in %.1fs: %s", job_id, time.monotonic() - started, result)
    return result


async def _delete_older_exports(pool: AsyncConnectionPool, storage: Storage, job_id: int) -> None:
    """Keep only the newest export file; older downloads then return 410 Gone."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select id from jobs where type = 'export' and status = 'succeeded' and id < %s", (job_id,)
        )
        older = [row[0] for row in await cur.fetchall()]
    for old_id in older:
        await asyncio.to_thread(storage.delete, export_key(old_id))
