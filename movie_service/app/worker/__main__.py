"""Job worker: `python -m app.worker`.

Claims queued jobs one at a time (`SKIP LOCKED`, so any number of workers can run), keeps a
heartbeat while a job runs, and periodically fails jobs whose worker died (DESIGN.md §10).
On SIGTERM/SIGINT the current job is cancelled and re-queued for another worker.
"""

import asyncio
import contextlib
import logging
import signal

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from app.config import Settings, get_settings
from app.db.migrate import migrate
from app.db.pool import create_pool
from app.domain import JobType
from app.services import jobs
from app.services.csv_import import CsvFileError
from app.services.importer import run_import
from app.services.storage import LocalStorage, Storage

logger = logging.getLogger("app.worker")

_IDLE_POLL_SECONDS = 5


class Worker:
    def __init__(self, settings: Settings, pool: AsyncConnectionPool, storage: Storage):
        self.settings = settings
        self.pool = pool
        self.storage = storage
        self.stopping = asyncio.Event()
        self.job_available = asyncio.Event()

    async def run(self) -> None:
        background = [
            asyncio.create_task(self._listen_for_jobs(), name="listen"),
            asyncio.create_task(self._sweep_stale_jobs(), name="sweep"),
        ]
        try:
            while not self.stopping.is_set():
                job = await jobs.claim_next_job(self.pool)
                if job is None:
                    await self._wait_for_work()
                    continue
                await self._run_job(job)
        finally:
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)

    async def _wait_for_work(self) -> None:
        self.job_available.clear()
        waiters = [asyncio.create_task(self.job_available.wait()), asyncio.create_task(self.stopping.wait())]
        await asyncio.wait(waiters, timeout=_IDLE_POLL_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        for w in waiters:
            w.cancel()

    async def _run_job(self, job: dict) -> None:
        job_id = job["id"]
        logger.info("running %s job %s", job["type"], job_id)
        work = asyncio.create_task(self._execute(job))
        beat = asyncio.create_task(self._heartbeat(job_id))
        stop = asyncio.create_task(self.stopping.wait())
        try:
            await asyncio.wait([work, stop], return_when=asyncio.FIRST_COMPLETED)
            if not work.done():
                logger.info("shutting down; re-queuing job %s", job_id)
                work.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await work
                await jobs.requeue(self.pool, job_id)
                return
            await work
        finally:
            beat.cancel()
            stop.cancel()

    async def _execute(self, job: dict) -> None:
        job_id = job["id"]
        try:
            if job["type"] == JobType.IMPORT:
                await run_import(self.pool, self.storage, self.settings, job)
            else:
                raise ValueError(f"unsupported job type {job['type']!r}")
        except asyncio.CancelledError:
            raise
        except CsvFileError as exc:
            logger.info("job %s failed: %s", job_id, exc)
            await jobs.mark_failed(self.pool, job_id, str(exc))
        except Exception:
            logger.exception("job %s failed", job_id)
            await jobs.mark_failed(self.pool, job_id, "Job failed due to an internal error")
        else:
            return
        finally:
            if not asyncio.current_task().cancelling():
                self._cleanup(job)

    def _cleanup(self, job: dict) -> None:
        if key := (job.get("params") or {}).get("upload_key"):
            self.storage.delete(key)

    async def _heartbeat(self, job_id: int) -> None:
        while True:
            await asyncio.sleep(self.settings.job_heartbeat_seconds)
            try:
                await jobs.heartbeat(self.pool, job_id)
            except Exception as exc:
                logger.warning("heartbeat for job %s failed: %s", job_id, exc)

    async def _sweep_stale_jobs(self) -> None:
        while True:
            try:
                for job in await jobs.fail_stale_jobs(self.pool, self.settings.job_stale_seconds):
                    logger.warning("job %s had no heartbeat; marked failed", job["id"])
                    self._cleanup(job)
            except Exception as exc:
                logger.warning("stale job sweep failed: %s", exc)
            await asyncio.sleep(self.settings.job_sweep_interval_seconds)

    async def _listen_for_jobs(self) -> None:
        backoff = 0.5
        while True:
            try:
                async with await AsyncConnection.connect(self.settings.database_url, autocommit=True) as conn:
                    await conn.execute(f"listen {jobs.QUEUE_CHANNEL}")
                    backoff = 0.5
                    self.job_available.set()
                    async for _ in conn.notifies():
                        self.job_available.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("job listener lost (%s); reconnecting in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    pool = create_pool(settings, statement_timeout_ms=settings.worker_statement_timeout_ms)
    await pool.open(wait=True, timeout=30)
    try:
        async with pool.connection() as conn:
            await migrate(conn)
        worker = Worker(settings, pool, LocalStorage(settings.data_dir))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, worker.stopping.set)
        logger.info("worker started")
        await worker.run()
    finally:
        await pool.close()
        logger.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
