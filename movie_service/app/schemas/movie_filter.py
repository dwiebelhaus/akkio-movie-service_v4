from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.movie import MAX_YEAR, MIN_YEAR


class MovieFilter(BaseModel):
    """Query parameters for `GET /movies`."""

    # Reject unknown parameters, so a typo like `genres=` isn't silently ignored.
    model_config = ConfigDict(extra="forbid")

    year_from: int | None = Field(None, ge=MIN_YEAR, le=MAX_YEAR, description="Inclusive lower bound")
    year_to: int | None = Field(None, ge=MIN_YEAR, le=MAX_YEAR, description="Inclusive upper bound")
    genre: list[str] = Field(
        default_factory=list,
        max_length=30,
        description="Repeatable: `?genre=Action&genre=Drama`. Case-insensitive.",
    )
    genre_match: Literal["any", "all"] = Field("any", description="Match any or all of the genres")
    limit: int = Field(50, ge=1, le=1000)
    cursor: str | None = Field(None, description="`next_cursor` from the previous page")

    @model_validator(mode="after")
    def _check_year_range(self) -> Self:
        if self.year_from is not None and self.year_to is not None and self.year_from > self.year_to:
            raise ValueError("year_from must be less than or equal to year_to")
        return self
