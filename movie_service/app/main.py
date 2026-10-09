import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db.migrate import migrate
from app.db.pool import create_pool
from app.errors import register_error_handlers
from app.routers import docs, exports, genres, health, imports, jobs, movies
from app.services.cache import ResponseCache
from app.services.job_events import JobEventHub
from app.services.movie_search import GenreCatalog
from app.services.storage import LocalStorage

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    pool = create_pool(settings)
    await pool.open(wait=True, timeout=30)
    async with pool.connection() as conn:
        await migrate(conn)
    hub = JobEventHub(settings.database_url)
    await hub.start()
    app.state.pool = pool
    app.state.hub = hub
    app.state.storage = LocalStorage(settings.data_dir)
    app.state.cache = ResponseCache(settings.redis_url, settings.cache_ttl_seconds, settings.cache_retry_seconds)
    app.state.catalog = GenreCatalog()
    try:
        yield
    finally:
        await app.state.cache.close()
        await hub.stop()
        await pool.close()


app = FastAPI(
    title="Movie API",
    version="1.0.0",
    lifespan=lifespan,
    # Served by `app.routers.docs`, which adds live genre names to the spec.
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)

register_error_handlers(app)

app.include_router(docs.router)
app.include_router(health.router)
app.include_router(movies.router, prefix=API_PREFIX)
app.include_router(genres.router, prefix=API_PREFIX)
app.include_router(imports.router, prefix=API_PREFIX)
app.include_router(exports.router, prefix=API_PREFIX)
app.include_router(jobs.router, prefix=API_PREFIX)
