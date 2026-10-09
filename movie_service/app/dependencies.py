import secrets
from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader
from psycopg_pool import AsyncConnectionPool

from app.config import Settings, get_settings
from app.errors import ApiError
from app.services.job_events import JobEventHub
from app.services.storage import Storage


def get_pool(request: Request) -> AsyncConnectionPool:
    return request.app.state.pool


def get_storage(request: Request) -> Storage:
    return request.app.state.storage


def get_hub(request: Request) -> JobEventHub:
    return request.app.state.hub


Pool = Annotated[AsyncConnectionPool, Depends(get_pool)]
StorageDep = Annotated[Storage, Depends(get_storage)]
Hub = Annotated[JobEventHub, Depends(get_hub)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    settings: SettingsDep, api_key: Annotated[str | None, Security(_api_key_header)] = None
) -> None:
    """Writes need `X-API-Key` matching `API_KEY` (constant-time comparison)."""
    expected = settings.api_key.get_secret_value().encode()
    if not api_key or not secrets.compare_digest(api_key.encode(), expected):
        raise ApiError(401, "unauthorized", "Missing or invalid API key")
