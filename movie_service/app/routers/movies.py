from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from app.dependencies import Cache, Catalog, Hub, IdPath, Pool, SettingsDep
from app.errors import ApiError
from app.schemas import ErrorResponse, MovieFilter, MovieRead, Page
from app.services.http_cache import cached_json, dataset_version
from app.services.movie_search import decode_cursor, get_movie, search_movies

router = APIRouter(tags=["movies"])

_cached = {304: {"description": "Not modified (matching `If-None-Match`)"}}


@router.get(
    "/movies",
    response_model=Page[MovieRead],
    responses={**_cached, 422: {"model": ErrorResponse}},
)
async def list_movies(
    request: Request,
    flt: Annotated[MovieFilter, Query()],
    pool: Pool,
    hub: Hub,
    cache: Cache,
    catalog: Catalog,
    settings: SettingsDep,
) -> Response:
    """Search movies by year range and genres, ordered by id, with cursor pagination.

    Movies with an unknown year are excluded when either year bound is set. Responses carry an
    `ETag` and `Cache-Control`; send `If-None-Match` to get `304` when nothing changed.
    """
    version = await dataset_version(hub, pool)
    genre_ids = await catalog.resolve(pool, flt.genre, version)
    after_id = decode_cursor(flt.cursor)

    async def produce() -> bytes:
        rows, next_cursor = await search_movies(pool, flt, genre_ids, after_id)
        return Page[MovieRead](items=rows, next_cursor=next_cursor).model_dump_json().encode()

    params = flt.model_dump(exclude={"genre", "cursor"}) | {"genre_ids": genre_ids, "after": after_id}
    return await cached_json(
        request,
        scope="search",
        params=params,
        version=version,
        cache=cache,
        max_age=settings.cache_max_age_seconds,
        produce=produce,
    )


@router.get(
    "/movies/{movie_id}",
    response_model=MovieRead,
    responses={**_cached, 404: {"model": ErrorResponse}},
)
async def read_movie(
    request: Request, movie_id: IdPath, pool: Pool, hub: Hub, cache: Cache, settings: SettingsDep
) -> Response:
    """One movie by id."""
    version = await dataset_version(hub, pool)

    async def produce() -> bytes:
        row = await get_movie(pool, movie_id)
        if row is None:
            raise ApiError(404, "movie_not_found", f"Movie {movie_id} not found")
        return MovieRead.model_validate(row).model_dump_json().encode()

    return await cached_json(
        request,
        scope="movie",
        params={"id": movie_id},
        version=version,
        cache=cache,
        max_age=settings.cache_max_age_seconds,
        produce=produce,
    )
