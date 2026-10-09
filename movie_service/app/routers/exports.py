import asyncio

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse

from app.dependencies import Hub, IdPath, Pool, SettingsDep, StorageDep, require_api_key
from app.domain import JobStatus, JobType
from app.errors import ApiError
from app.schemas import ErrorResponse, JobRead
from app.services import jobs
from app.services.exporter import export_key, request_export
from app.services.http_cache import dataset_version

router = APIRouter(tags=["exports"])

_CHUNK = 1024 * 1024


@router.post(
    "/exports",
    status_code=202,
    dependencies=[Depends(require_api_key)],
    responses={
        200: {"model": JobRead, "description": "An export of the current data already exists or is running"},
        401: {"model": ErrorResponse},
    },
)
async def create_export(request: Request, response: Response, pool: Pool, hub: Hub, storage: StorageDep) -> JobRead:
    """Start a gzipped CSV export of the whole database, in the import format.

    If the data hasn't changed since a finished or running export, that job is returned (`200`)
    instead of starting a new one (`202`). Follow progress at `/jobs/{id}/events`, then download
    from `/exports/{id}/file`.
    """
    version = await dataset_version(hub, pool)
    job, created = await request_export(pool, storage, version)
    response.status_code = 202 if created else 200
    response.headers["Location"] = f"{request.scope.get('root_path', '')}/api/v1/jobs/{job['id']}"
    return JobRead.model_validate(job)


@router.get(
    "/exports/{job_id}/file",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"application/gzip": {}}, "description": "Gzipped CSV"},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "Export not finished, or failed"},
        410: {"model": ErrorResponse, "description": "Replaced by a newer export"},
    },
)
async def download_export(job_id: IdPath, pool: Pool, storage: StorageDep, settings: SettingsDep) -> StreamingResponse:
    """Download a finished export (`movie_name,year,genres,rating`, gzip-compressed)."""
    job = await jobs.get_job(pool, job_id)
    if job is None or job["type"] != JobType.EXPORT:
        raise ApiError(404, "export_not_found", f"Export {job_id} not found")
    status = JobStatus(job["status"])
    if status == JobStatus.FAILED:
        raise ApiError(409, "export_failed", "Export failed", {"error": job["error"]})
    if status != JobStatus.SUCCEEDED:
        raise ApiError(409, "export_not_ready", "Export is not finished yet", {"status": status.value})

    key = export_key(job_id)
    expired = ApiError(410, "export_expired", "This export was replaced by a newer one; request a new export")
    try:
        file = await asyncio.to_thread(storage.open_read, key)
    except FileNotFoundError:
        raise expired from None
    try:
        size = await asyncio.to_thread(storage.size, key)
    except BaseException as exc:
        # Deleted just after it was opened (a newer export finished).
        await asyncio.to_thread(file.close)
        if isinstance(exc, FileNotFoundError):
            raise expired from None
        raise

    async def body():
        try:
            while chunk := await asyncio.to_thread(file.read, _CHUNK):
                yield chunk
        finally:
            await asyncio.to_thread(file.close)

    version = (job["result"] or {}).get("dataset_version")
    return StreamingResponse(
        body(),
        media_type="application/gzip",
        headers={
            "Content-Length": str(size),
            "Content-Disposition": f'attachment; filename="movies-v{version}.csv.gz"',
            # An export's content never changes; it can only disappear (410).
            "Cache-Control": f"private, max-age={settings.cache_max_age_seconds}",
        },
    )
