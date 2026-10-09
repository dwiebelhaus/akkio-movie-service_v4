from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


MIN_API_KEY_LENGTH = 16


class Settings(BaseSettings):
    """Service configuration, read from environment variables (and `.env` if present)."""

    # hide_input_in_errors: a rejected API_KEY must not be echoed into the logs.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    database_url: str = "postgresql://movies:movies@localhost:5432/movies"
    db_pool_min_size: int = Field(default=1, ge=1)
    db_pool_max_size: int = Field(default=10, ge=1)
    # Server-side cap on any single API statement, so slow queries fail cleanly instead of hanging.
    statement_timeout_ms: int = Field(default=5000, ge=0)
    # Workers run bulk merges and exports, which legitimately take longer.
    worker_statement_timeout_ms: int = Field(default=30 * 60 * 1000, ge=0)

    # Required, with no default, so a deployment can't silently run with a well-known key.
    api_key: SecretStr = Field(min_length=MIN_API_KEY_LENGTH)
    # Sized for ~750k movies (DESIGN.md §8).
    max_upload_bytes: int = Field(default=64 * 1024 * 1024, gt=0)
    data_dir: str = "/data"

    import_batch_rows: int = Field(default=10_000, gt=0)
    progress_interval_seconds: float = Field(default=0.25, gt=0)
    job_heartbeat_seconds: float = Field(default=5, gt=0)
    job_stale_seconds: float = Field(default=60, gt=0)
    job_sweep_interval_seconds: float = Field(default=15, gt=0)

    # Caching (DESIGN.md §13). An empty REDIS_URL disables the shared cache.
    redis_url: str = ""
    cache_ttl_seconds: int = Field(default=3600, gt=0)
    cache_max_age_seconds: int = Field(default=3600, ge=0)
    # After a Redis error, skip Redis for this long before trying again (fail-open).
    cache_retry_seconds: float = Field(default=10, gt=0)

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
