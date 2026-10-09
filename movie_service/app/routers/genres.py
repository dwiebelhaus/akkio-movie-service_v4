import json

from fastapi import APIRouter, Request, Response

from app.dependencies import Cache, Catalog, Hub, Pool, SettingsDep
from app.services.http_cache import cached_json, dataset_version

router = APIRouter(tags=["genres"])


@router.get(
    "/genres",
    response_model=list[str],
    responses={304: {"description": "Not modified (matching `If-None-Match`)"}},
)
async def list_genres(
    request: Request, pool: Pool, hub: Hub, cache: Cache, catalog: Catalog, settings: SettingsDep
) -> Response:
    """All genre names, sorted. Valid values for the `genre` filter on `GET /movies`."""
    version = await dataset_version(hub, pool)

    async def produce() -> bytes:
        return json.dumps(await catalog.names(pool, version)).encode()

    return await cached_json(
        request,
        scope="genres",
        params={},
        version=version,
        cache=cache,
        max_age=settings.cache_max_age_seconds,
        produce=produce,
    )
