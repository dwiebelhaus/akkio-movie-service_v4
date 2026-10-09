import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db.migrate import migrate
from app.db.pool import create_pool
from app.errors import register_error_handlers
from app.routers import health, movies

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    pool = create_pool(settings)
    await pool.open(wait=True, timeout=30)
    async with pool.connection() as conn:
        await migrate(conn)
    app.state.pool = pool
    try:
        yield
    finally:
        await pool.close()


app = FastAPI(
    title="Movie API",
    version="1.0.0",
    lifespan=lifespan,
)

register_error_handlers(app)

app.include_router(health.router)
app.include_router(movies.router, prefix=API_PREFIX)
