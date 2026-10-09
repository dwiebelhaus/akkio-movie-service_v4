from typing import Annotated
from pydantic import BaseModel, Field


class MoviesQuery(BaseModel):
    """
    This class is only served as a base example for query
    Please update this model to best optimize the usecase
    """

    start_year: Annotated[str, Field(description="Start year of the movie, inclusive")]
    end_year: Annotated[str, Field(description="End year of the movie, inclusive")]
    genre: Annotated[str, Field(description="genre to include")]

    # What if we want to support genre items
    # genres: Annotated[list[str], Field(description="genre to include")]
