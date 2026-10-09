import logging

from fastapi import APIRouter

from app.dependencies import Cache, Pool
from app.errors import ApiError
from app.schemas import ErrorResponse, Health

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health", responses={503: {"model": ErrorResponse}})
async def health(pool: Pool, cache: Cache) -> Health:
    """Liveness plus a database round trip. Cache status is reported but never fails the check."""
    try:
        async with pool.connection(timeout=2) as conn:
            await conn.execute("select 1")
    except Exception as exc:
        logger.warning("health check failed: %s", exc)
        raise ApiError(503, "database_unavailable", "Database is unavailable") from exc
    return Health(status="ok", database="ok", cache=await cache.status())
