from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Movie:
    """A movie record from the dataset."""

    movie_name: str
    year: int | None
    genres: tuple[str, ...]
    rating: float | None = None

    @classmethod
    def from_csv_row(cls, row: dict[str, str]) -> "Movie":
        """Build a Movie from a `movies.csv` row (`movie_name,year,genres,rating`).

        Raises ValueError if the row is malformed.
        """
        year = (row.get("year") or "").strip()
        rating = (row.get("rating") or "").strip()
        return cls(
            movie_name=row["movie_name"].strip(),
            # Some rows hold an IMDb disambiguator ("I", "II", ...) instead of a year.
            year=int(year) if year.isdigit() else None,
            genres=tuple(g.strip() for g in (row.get("genres") or "").split(",") if g.strip()),
            rating=float(rating) if rating else None,
        )
