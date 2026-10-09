import re
import unicodedata
from dataclasses import dataclass

MIN_YEAR = 1870
MAX_YEAR = 2100
# IMDb disambiguators ("I", "II", ...) that some rows hold instead of a year.
_DISAMBIGUATOR = re.compile(r"^[IVXLC]+$")
GENRE_SEPARATOR = "|"


def clean_name(name: str) -> str:
    """Display form of a title or genre name: Unicode NFC, control and invisible format
    characters (zero-width spaces, BOMs, ...) removed, whitespace trimmed and collapsed."""
    name = unicodedata.normalize("NFC", name)
    name = "".join(c if c.isspace() or unicodedata.category(c) not in ("Cc", "Cf") else "" for c in name)
    return " ".join(name.split())


def name_key(name: str) -> str:
    """Identity form of a title or genre name: cleaned and case-folded."""
    return unicodedata.normalize("NFC", clean_name(name).casefold())


def normalize_title(title: str) -> str:
    """Identity form of a title."""
    return name_key(title)


@dataclass(frozen=True, slots=True)
class Movie:
    """A movie record from the dataset."""

    movie_name: str
    year: int | None
    genres: tuple[str, ...]  # display names, sorted and unique by genre key
    rating: float | None = None

    @property
    def title_key(self) -> str:
        return normalize_title(self.movie_name)

    @property
    def genre_key(self) -> str:
        """Genre keys (case-folded names) in the same order as `genres`, joined with '|'."""
        return GENRE_SEPARATOR.join(map(name_key, self.genres))

    @classmethod
    def from_csv_row(cls, row: dict[str, str]) -> "Movie":
        """Build a Movie from a `movies.csv` row (`movie_name,year,genres,rating`).

        Missing year or rating become None. Raises ValueError with a short reason if the
        row is malformed.
        """
        title = clean_name(row.get("movie_name") or "")
        if not title:
            raise ValueError("missing movie_name")

        year_raw = (row.get("year") or "").strip()
        if not year_raw or _DISAMBIGUATOR.match(year_raw):
            year = None
        elif year_raw.isdigit() and MIN_YEAR <= int(year_raw) <= MAX_YEAR:
            year = int(year_raw)
        else:
            raise ValueError(f"invalid year: {year_raw!r}")

        # Genres match case-insensitively; the first spelling in the row is kept.
        by_key: dict[str, str] = {}
        for raw in (row.get("genres") or "").split(","):
            if name := clean_name(raw):
                by_key.setdefault(name_key(name), name)
        genres = [by_key[k] for k in sorted(by_key)]
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
