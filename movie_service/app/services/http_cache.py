"""Versioned HTTP caching for read endpoints: ETag/304, Cache-Control, and the shared cache."""

import hashlib
import json
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from psycopg_pool import AsyncConnectionPool

from app.services.cache import ResponseCache
from app.services.job_events import JobEventHub


async def dataset_version(hub: JobEventHub, pool: AsyncConnectionPool) -> int:
    """Current dataset version: from memory (kept fresh by NOTIFY), else from the database."""
    if hub.dataset_version is not None:
        return hub.dataset_version
    async with pool.connection() as conn:
        cur = await conn.execute("select version from dataset_state")
        return (await cur.fetchone())[0]


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    # Weak comparison (RFC 9110 §13.1.2): ignore W/ prefixes.
    tags = {t.strip().removeprefix("W/") for t in if_none_match.split(",")}
    return etag in tags


async def cached_json(
    request: Request,
    *,
    scope: str,
    params: dict,
    version: int,
    cache: ResponseCache,
    max_age: int,
    produce: Callable[[], Awaitable[bytes]],
) -> Response:
    """Serve a JSON body identified by (scope, params) at a dataset version.

    Order: a matching If-None-Match gets 304 with no work; then the shared cache; then
    `produce()` (the database), whose result is stored in the cache.
    """
    digest = hashlib.sha256(json.dumps([scope, params], sort_keys=True).encode()).hexdigest()[:24]
    etag = f'"v{version}-{digest}"'
    headers = {"ETag": etag, "Cache-Control": f"public, max-age={max_age}"}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)

    key = f"movies:v{version}:{digest}"
    body = await cache.get(key)
    if body is None:
        body = await produce()
        await cache.set(key, body)
        headers["X-Cache"] = "MISS"
    else:
        headers["X-Cache"] = "HIT"
    return Response(content=body, media_type="application/json", headers=headers)
