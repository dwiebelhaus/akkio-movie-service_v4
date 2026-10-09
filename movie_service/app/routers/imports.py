import asyncio

from fastapi import APIRouter, Depends, Request, Response

from app.dependencies import Pool, SettingsDep, StorageDep, require_api_key
from app.domain import JobType
from app.schemas import ErrorResponse, JobRead
from app.services import jobs
from app.services.uploads import FILE_FIELD, receive_csv_upload

router = APIRouter(tags=["imports"])

_errors = {code: {"model": ErrorResponse} for code in (401, 413, 415, 422)}
_upload_body = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": [FILE_FIELD],
                    "properties": {FILE_FIELD: {"type": "string", "format": "binary"}},
                }
            },
            "text/csv": {"schema": {"type": "string", "format": "binary"}},
        },
    }
}


@router.post(
    "/imports",
    status_code=202,
    dependencies=[Depends(require_api_key)],
    responses=_errors,
    openapi_extra=_upload_body,
)
async def create_import(
    request: Request, response: Response, pool: Pool, storage: StorageDep, settings: SettingsDep
) -> JobRead:
    """Upload a movies CSV (`movie_name,year,genres,rating`) and queue an import job.

    The body is streamed to storage; follow progress at `/jobs/{id}/events`.
    """
    upload = await receive_csv_upload(request, storage, settings.max_upload_bytes)
    params = {"upload_key": upload.key, "file_size": upload.size, "warnings": upload.header.warnings}
    try:
        job = await jobs.create_job(pool, JobType.IMPORT, params)
    except BaseException:
        await asyncio.to_thread(storage.delete, upload.key)
        raise
    response.headers["Location"] = f"{request.scope.get('root_path', '')}/api/v1/jobs/{job['id']}"
    return JobRead.model_validate(job)
