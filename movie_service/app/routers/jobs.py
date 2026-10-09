import asyncio
import contextlib

from fastapi import APIRouter
from sse_starlette import EventSourceResponse, ServerSentEvent

from app.dependencies import Hub, Pool
from app.errors import ApiError
from app.schemas import ErrorResponse, JobRead
from app.services import jobs

router = APIRouter(tags=["jobs"])

# Re-read the job at least this often even without a notification (belt and braces).
_FALLBACK_POLL_SECONDS = 5
_KEEPALIVE_SECONDS = 15


async def _load(pool, job_id: int) -> JobRead:
    job = await jobs.get_job(pool, job_id)
    if job is None:
        raise ApiError(404, "job_not_found", f"Job {job_id} not found")
    return JobRead.model_validate(job)


@router.get("/jobs/{job_id}", responses={404: {"model": ErrorResponse}})
async def get_job(job_id: int, pool: Pool) -> JobRead:
    """Current status snapshot of a job."""
    return await _load(pool, job_id)


@router.get(
    "/jobs/{job_id}/events",
    response_class=EventSourceResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "Server-sent JobRead events"},
        404: {"model": ErrorResponse},
    },
)
async def job_events(job_id: int, pool: Pool, hub: Hub) -> EventSourceResponse:
    """Live progress as server-sent events.

    Sends the current state immediately, then an event on every change: `progress` while
    queued or running, then a final `succeeded` or `failed` event, after which the stream closes.
    Each event's data is a `JobRead` JSON object.
    """
    await _load(pool, job_id)  # 404 before the stream starts

    async def stream():
        with hub.subscribe(job_id) as changed:
            last: JobRead | None = None
            while True:
                changed.clear()
                job = await _load(pool, job_id)
                if job != last:
                    terminal = job.status.is_terminal
                    yield ServerSentEvent(
                        data=job.model_dump_json(),
                        event=job.status.value if terminal else "progress",
                    )
                    if terminal:
                        return
                    last = job
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(changed.wait(), _FALLBACK_POLL_SECONDS)

    return EventSourceResponse(stream(), ping=_KEEPALIVE_SECONDS)
