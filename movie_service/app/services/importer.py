"""Import job: stream the uploaded CSV into a temp table with COPY, then merge set-based (DESIGN.md §5, §8)."""

import asyncio
import logging
import time
from dataclasses import asdict

from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.services import jobs
from app.services.csv_import import Batch, CsvBatchReader
from app.services.storage import Storage

logger = logging.getLogger(__name__)

IMPORT_LOCK_ID = 0x696D7074  # serializes merges across workers
DATASET_CHANNEL = "dataset_changed"
# Progress bar shares, roughly matching measured time on a fresh database: reading/staging,
# then inserting movies, then genre links (dominated by per-row FK checks).
STAGING_SHARE = 0.25
MOVIES_SHARE = 0.10
LINK_CHUNK_MOVIES = 20_000

# Each import stages into its own temp table: not WAL-logged, dropped at commit, and a
# crashed import leaves nothing behind.
_CREATE_STAGING = """
create temp table import_rows (
    line_no    bigint       not null,
    title      text         not null,
    title_key  text collate "C" not null,
    year       smallint,
    genre_key  text collate "C" not null,
    rating     numeric(3,1)
) on commit drop
"""

_COPY_STAGING = "copy import_rows (line_no, title, title_key, year, genre_key, rating) from stdin"

# Collapse duplicates within the file: last title spelling, last non-null rating.
_COLLAPSE = """
create temp table import_movies on commit drop as
select title_key, year, genre_key,
       (array_agg(title order by line_no desc))[1] as title,
       (array_agg(rating order by line_no desc) filter (where rating is not null))[1] as rating,
       min(line_no) as first_line
  from import_rows
 group by title_key, year, genre_key
"""

_INSERT_NEW_GENRES = """
insert into genres (name)
select distinct gn.name
  from (select distinct genre_key from import_movies) k
 cross join lateral unnest(string_to_array(k.genre_key, '|')) as gn(name)
 where not exists (select 1 from genres g where g.name = gn.name)
on conflict (name) do nothing
"""

# Merges run under IMPORT_LOCK_ID, so plain UPDATE + INSERT ... WHERE NOT EXISTS is safe and
# much cheaper than INSERT ... ON CONFLICT; the unique constraint remains as a backstop.
# Matched movies are only rewritten when their rating actually changes (latest non-null wins).
_UPDATE_EXISTING = """
update movies m
   set rating = b.rating, last_import_id = %(job_id)s, updated_at = now()
  from import_movies b
 where m.title_key = b.title_key
   and m.genre_key = b.genre_key
   and m.year is not distinct from b.year
   and b.rating is not null
   and m.rating is distinct from b.rating
"""

# Insert new movies in file order. Under the import lock nothing else inserts movies, so the
# new ids form the range [first_id, last_id] and genre links can be added in chunks by id.
_INSERT_NEW = """
with new_movies as (
    insert into movies (title, title_key, year, genre_key, rating, first_import_id, last_import_id)
    select b.title, b.title_key, b.year, b.genre_key, b.rating, %(job_id)s, %(job_id)s
      from import_movies b
     where not exists (select 1 from movies m
                        where m.title_key = b.title_key
                          and m.genre_key = b.genre_key
                          and m.year is not distinct from b.year)
     order by b.first_line
    returning id
)
select count(*), min(id), max(id) from new_movies
"""

# Resolve each distinct genre_key to genre ids once.
_GENRE_SETS = """
create temp table import_genre_sets on commit drop as
select k.genre_key, array_agg(g.id) as genre_ids
  from (select distinct genre_key from import_movies) k
 cross join lateral unnest(string_to_array(k.genre_key, '|')) as gn(name)
  join genres g on g.name = gn.name
 group by k.genre_key
"""

_LINK_GENRES = """
insert into movie_genres (movie_id, genre_id)
select m.id, unnest(gs.genre_ids)
  from movies m
  join import_genre_sets gs using (genre_key)
 where m.id between %(lo)s and %(hi)s
   and m.first_import_id = %(job_id)s
"""


_COPY_ESCAPES = str.maketrans({"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"})


def _copy_field(value: object) -> str:
    if value is None:
        return "\\N"
    return str(value).translate(_COPY_ESCAPES)


def encode_copy_rows(batch: Batch) -> bytes:
    """Encode a batch in COPY text format, so it goes to Postgres in one write."""
    return "".join(
        "\t".join(map(_copy_field, (line, m.movie_name, m.title_key, m.year, m.genre_key, m.rating)))
        + "\n"
        for line, m in batch.movies
    ).encode()


def _read_encoded(reader: CsvBatchReader) -> tuple[Batch, bytes] | None:
    batch = reader.read_batch()
    return None if batch is None else (batch, encode_copy_rows(batch))


class ProgressReporter:
    """Throttled progress writes (at most one per interval) on their own connection,
    so they are visible while the import transaction is still open."""

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
        await jobs.report_progress(self._pool, self._job_id, progress, processed_rows)


async def run_import(pool: AsyncConnectionPool, storage: Storage, settings: Settings, job: dict) -> dict:
    """Run one import job to completion and mark it succeeded. Raises on failure."""
    job_id = job["id"]
    params = job["params"]
    upload_key = params["upload_key"]
    file_size = max(await asyncio.to_thread(storage.size, upload_key), 1)
    progress = ProgressReporter(pool, job_id, settings.progress_interval_seconds)

    file = await asyncio.to_thread(storage.open_read, upload_key)
    try:
        reader = await asyncio.to_thread(CsvBatchReader, file, settings.import_batch_rows)
        processed = rejected = 0
        started = time.monotonic()

        async with pool.connection() as conn, conn.transaction():
            await conn.execute("set local work_mem = '256MB'")
            await conn.execute(_CREATE_STAGING)
            cur = conn.cursor()
            async with cur.copy(_COPY_STAGING) as copy:
                # Parse and encode in a thread, one batch ahead, so parsing overlaps the COPY.
                next_item = asyncio.ensure_future(asyncio.to_thread(_read_encoded, reader))
                while (item := await next_item) is not None:
                    next_item = asyncio.ensure_future(asyncio.to_thread(_read_encoded, reader))
                    batch, payload = item
                    await copy.write(payload)
                    processed += len(batch.movies) + batch.rejected
                    rejected += batch.rejected
                    await progress.update(STAGING_SHARE * batch.bytes_read / file_size, processed)
            await progress.update(STAGING_SHARE, processed, force=True)
            staged_at = time.monotonic()

            cur = await conn.execute(_COLLAPSE)
            distinct_movies = cur.rowcount
            await conn.execute("analyze import_movies")
            await conn.execute("select pg_advisory_xact_lock(%s)", (IMPORT_LOCK_ID,))
            await conn.execute(_INSERT_NEW_GENRES)
            cur = await conn.execute(_UPDATE_EXISTING, {"job_id": job_id})
            updated = cur.rowcount
            cur = await conn.execute(_INSERT_NEW, {"job_id": job_id})
            inserted, first_id, last_id = await cur.fetchone()
            await progress.update(STAGING_SHARE + MOVIES_SHARE, processed, force=True)

            if inserted:
                await conn.execute(_GENRE_SETS)
                link_share = 1 - STAGING_SHARE - MOVIES_SHARE
                for lo in range(first_id, last_id + 1, LINK_CHUNK_MOVIES):
                    hi = min(lo + LINK_CHUNK_MOVIES - 1, last_id)
                    await conn.execute(_LINK_GENRES, {"lo": lo, "hi": hi, "job_id": job_id})
                    done = (hi - first_id + 1) / (last_id - first_id + 1)
                    await progress.update(STAGING_SHARE + MOVIES_SHARE + link_share * done, processed)
            merged_at = time.monotonic()

            if inserted or updated:
                cur = await conn.execute(
                    "update dataset_state set version = version + 1 returning version"
                )
                (version,) = await cur.fetchone()
                await conn.execute("select pg_notify(%s, %s)", (DATASET_CHANNEL, str(version)))

            result = {
                "inserted": inserted,
                "updated": updated,
                "unchanged": distinct_movies - inserted - updated,
                "duplicates_in_file": processed - rejected - distinct_movies,
                "rejected": rejected,
                "rejected_samples": [asdict(r) for r in reader.rejected_samples],
                "warnings": params.get("warnings", []),
            }
            await jobs.mark_succeeded(conn, job_id, result, processed)
    finally:
        await asyncio.to_thread(file.close)

    logger.info(
        "import job %s finished in %.1fs (staging %.1fs, merge %.1fs): %s",
        job_id,
        time.monotonic() - started,
        staged_at - started,
        merged_at - staged_at,
        {k: v for k, v in result.items() if k != "rejected_samples"},
    )
    return result
