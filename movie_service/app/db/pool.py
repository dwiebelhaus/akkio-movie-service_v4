from psycopg_pool import AsyncConnectionPool

from app.config import Settings


def create_pool(settings: Settings) -> AsyncConnectionPool:
    """Create (but don't open) the shared async connection pool."""
    return AsyncConnectionPool(
        settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        kwargs={"options": f"-c statement_timeout={settings.statement_timeout_ms}"},
        open=False,
    )
