from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.domain import JobStatus, JobType


class JobRead(BaseModel):
    """Status snapshot of an import or export job."""

    id: int
    type: JobType
    status: JobStatus
    progress: float = Field(ge=0, le=1)
    processed_rows: int | None
    total_rows: int | None
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
