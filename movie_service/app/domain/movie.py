import re
from dataclasses import dataclass

MIN_YEAR = 1870
MAX_YEAR = 2100
# IMDb disambiguators ("I", "II", ...) that some rows hold instead of a year.
_DISAMBIGUATOR = re.compile(r"^[IVXLC]+$")
GENRE_SEPARATOR = "|"


def normalize_title(title: str) -> str:
    """Identity form of a title: whitespace collapsed, case-folded."""
    return " ".join(title.split()).casefold()


@dataclass(frozen=True, slots=True)
class Movie:
    """A movie record from the dataset."""

    movie_name: str
    year: int | None
    genres: tuple[str, ...]  # sorted, unique
    rating: float | None = None

    @property
    def title_key(self) -> str:
        return normalize_title(self.movie_name)

    @property
    def genre_key(self) -> str:
        return GENRE_SEPARATOR.join(self.genres)

    @classmethod
    def from_csv_row(cls, row: dict[str, str]) -> "Movie":
        """Build a Movie from a `movies.csv` row (`movie_name,year,genres,rating`).

        Missing year or rating become None. Raises ValueError with a short reason if the
        row is malformed.
        """
        # Postgres text can't hold NUL; letting one through would fail the whole import.
        if any("\x00" in value for value in row.values() if value):
            raise ValueError("field contains a NUL byte")

        title = " ".join((row.get("movie_name") or "").split())
        if not title:
            raise ValueError("missing movie_name")

        year_raw = (row.get("year") or "").strip()
        if not year_raw or _DISAMBIGUATOR.match(year_raw):
            year = None
        elif year_raw.isdigit() and MIN_YEAR <= int(year_raw) <= MAX_YEAR:
            year = int(year_raw)
        else:
            raise ValueError(f"invalid year: {year_raw!r}")

        genres = sorted({g.strip() for g in (row.get("genres") or "").split(",") if g.strip()})
        if any(GENRE_SEPARATOR in g for g in genres):
            raise ValueError(f"invalid genre name containing {GENRE_SEPARATOR!r}")

        rating_raw = (row.get("rating") or "").strip()
        if rating_raw:
            try:
                rating = round(float(rating_raw), 1)
            except ValueError:
                raise ValueError(f"invalid rating: {rating_raw!r}") from None
            if not 0 <= rating <= 10:
                raise ValueError(f"rating out of range: {rating_raw!r}")
        else:
            rating = None

        return cls(movie_name=title, year=year, genres=tuple(genres), rating=rating)
