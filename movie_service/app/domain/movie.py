import re
import unicodedata
from dataclasses import dataclass

MIN_YEAR = 1870
MAX_YEAR = 2100
# IMDb disambiguators ("I", "II", ...) that some rows hold instead of a year.
_DISAMBIGUATOR = re.compile(r"^[IVXLC]+$")
GENRE_SEPARATOR = "|"
MAX_TITLE_CHARS = 500
MAX_GENRE_CHARS = 64
MAX_GENRES = 20
# Postgres can't index a btree entry over 2704 bytes, and (title_key, year, genre_key) is the
# unique identity index; keep the keys well under it, whatever the UTF-8 width.
MAX_IDENTITY_BYTES = 2000
# Longest raw value quoted in a rejection reason; reasons are stored in the job result.
MAX_REASON_VALUE_CHARS = 100
# Exports put `'` before a field that starts with one of these (after any `'`s), so
# spreadsheets don't run it as a formula; imports remove one such `'` again (exporter.py).
_FORMULA_ESCAPED = re.compile(r"^'+[-=+@]")


def clean_name(name: str) -> str:
    """Display form of a title or genre name: Unicode NFC, control and invisible format
    characters (zero-width spaces, BOMs, ...) removed, whitespace trimmed and collapsed."""
    name = unicodedata.normalize("NFC", name)
    name = "".join(c if c.isspace() or unicodedata.category(c) not in ("Cc", "Cf") else "" for c in name)
    return " ".join(name.split())


def name_key(name: str) -> str:
    """Identity form of a title or genre name: cleaned and case-folded."""
    return unicodedata.normalize("NFC", clean_name(name).casefold())


def unescape_formula(name: str) -> str:
    """Undo the export's formula escaping: `'=x` -> `=x`, `''=x` -> `'=x`."""
    return name[1:] if _FORMULA_ESCAPED.match(name) else name


def _shown(raw: str) -> str:
    """`raw` quoted for a rejection reason, truncated so reasons stay small."""
    if len(raw) > MAX_REASON_VALUE_CHARS:
        return repr(raw[:MAX_REASON_VALUE_CHARS]) + "..."
    return repr(raw)


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
        title = unescape_formula(clean_name(row.get("movie_name") or ""))
        if not title:
            raise ValueError("missing movie_name")
        if len(title) > MAX_TITLE_CHARS:
            raise ValueError(f"movie_name longer than {MAX_TITLE_CHARS} characters")

        year_raw = (row.get("year") or "").strip()
        if not year_raw or _DISAMBIGUATOR.match(year_raw):
            year = None
        elif year_raw.isdigit() and len(year_raw) <= 4 and MIN_YEAR <= int(year_raw) <= MAX_YEAR:
            year = int(year_raw)
        else:
            raise ValueError(f"invalid year: {_shown(year_raw)}")

        # Genres match case-insensitively; the first spelling in the row is kept.
        by_key: dict[str, str] = {}
        for raw in (row.get("genres") or "").split(","):
            if name := unescape_formula(clean_name(raw)):
                if len(name) > MAX_GENRE_CHARS:
                    raise ValueError(f"genre name longer than {MAX_GENRE_CHARS} characters")
                by_key.setdefault(name_key(name), name)
        if len(by_key) > MAX_GENRES:
            raise ValueError(f"more than {MAX_GENRES} genres")
        genres = [by_key[k] for k in sorted(by_key)]
        if any(GENRE_SEPARATOR in g for g in genres):
            raise ValueError(f"invalid genre name containing {GENRE_SEPARATOR!r}")

        rating_raw = (row.get("rating") or "").strip()
        if rating_raw:
            try:
                rating = round(float(rating_raw), 1)
            except ValueError:
                raise ValueError(f"invalid rating: {_shown(rating_raw)}") from None
            if not 0 <= rating <= 10:
                raise ValueError(f"rating out of range: {_shown(rating_raw)}")
        else:
            rating = None

        movie = cls(movie_name=title, year=year, genres=tuple(genres), rating=rating)
        if len(movie.title_key.encode()) + len(movie.genre_key.encode()) > MAX_IDENTITY_BYTES:
            raise ValueError("movie_name and genres are too long together")
        return movie
