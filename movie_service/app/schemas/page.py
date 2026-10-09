from pydantic import BaseModel


class Page[T](BaseModel):
    """One page of results. Pass `next_cursor` back as `cursor` for the next page; null means the end."""

    items: list[T]
    next_cursor: str | None
