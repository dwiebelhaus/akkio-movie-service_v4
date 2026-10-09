from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service configuration, read from environment variables (and `.env` if present)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://movies:movies@localhost:5432/movies"
    db_pool_min_size: int = Field(default=1, ge=1)
    db_pool_max_size: int = Field(default=10, ge=1)
    # Server-side cap on any single statement, so slow queries fail cleanly instead of hanging.
    statement_timeout_ms: int = Field(default=5000, ge=0)

    api_key: SecretStr = SecretStr("dev-api-key")
    # Sized for ~750k movies (DESIGN.md §8).
    max_upload_bytes: int = Field(default=64 * 1024 * 1024, gt=0)
    data_dir: str = "/data"

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
