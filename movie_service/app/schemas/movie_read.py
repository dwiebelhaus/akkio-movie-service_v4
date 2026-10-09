from pydantic import BaseModel


class MovieRead(BaseModel):
    id: int
    title: str
    year: int | None
    genres: list[str]
    rating: float | None
