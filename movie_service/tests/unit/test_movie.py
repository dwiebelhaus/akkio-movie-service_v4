import pytest

from app.domain import Movie


def row(**overrides) -> dict[str, str]:
    return {"movie_name": "Glass Onion", "year": "2022", "genres": "Comedy, Crime, Drama", "rating": "7.2"} | overrides


def test_parses_a_full_row():
    movie = Movie.from_csv_row(row())
    assert movie == Movie("Glass Onion", 2022, ("Comedy", "Crime", "Drama"), 7.2)
    assert movie.genre_key == "comedy|crime|drama"
    assert movie.title_key == "glass onion"


def test_missing_values_become_none():
    movie = Movie.from_csv_row(row(year="", rating="", genres=""))
    assert movie.year is None
    assert movie.rating is None
    assert movie.genres == ()
    assert movie.genre_key == ""


@pytest.mark.parametrize("year", ["I", "II", "IV", "XII"])
def test_imdb_disambiguator_is_unknown_year(year):
    assert Movie.from_csv_row(row(year=year)).year is None


def test_genres_are_trimmed_sorted_and_unique():
    movie = Movie.from_csv_row(row(genres=" Drama ,Action,,Drama "))
    assert movie.genres == ("Action", "Drama")


def test_title_whitespace_is_collapsed_and_key_case_folded():
    movie = Movie.from_csv_row(row(movie_name="  The   STRANGER "))
    assert movie.movie_name == "The STRANGER"
    assert movie.title_key == "the stranger"


def test_genres_match_case_insensitively_keeping_first_spelling():
    movie = Movie.from_csv_row(row(genres="sci-fi, Drama, SCI-FI, drama, Action"))
    assert movie.genres == ("Action", "Drama", "sci-fi")
    assert movie.genre_key == "action|drama|sci-fi"
    assert movie.genre_key == Movie.from_csv_row(row(genres="Sci-Fi,DRAMA,action")).genre_key


def test_title_is_unicode_normalized():
    composed = Movie.from_csv_row(row(movie_name="Am\u00e9lie"))
    decomposed = Movie.from_csv_row(row(movie_name="Ame\u0301lie"))
    assert decomposed.movie_name == composed.movie_name == "Am\u00e9lie"
    assert decomposed.title_key == composed.title_key == "am\u00e9lie"


def test_invisible_and_control_characters_are_removed():
    movie = Movie.from_csv_row(row(movie_name="\ufeffThe\u200b Matrix\x07", genres="Sci\u200b-Fi, \u00a0Action"))
    assert movie.movie_name == "The Matrix"
    assert movie.genres == ("Action", "Sci-Fi")


def test_unusual_whitespace_is_collapsed():
    movie = Movie.from_csv_row(row(movie_name="Glass\u00a0\u2003Onion\t"))
    assert movie.movie_name == "Glass Onion"
    assert movie.title_key == "glass onion"


def test_title_with_only_invisible_characters_is_missing():
    with pytest.raises(ValueError, match="^missing movie_name$"):
        Movie.from_csv_row(row(movie_name="\u200b\ufeff"))


def test_rating_is_rounded_to_one_decimal():
    assert Movie.from_csv_row(row(rating="7.25")).rating == 7.2
    assert Movie.from_csv_row(row(rating="10")).rating == 10.0


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"movie_name": "  "}, "missing movie_name"),
        ({"year": "19x9"}, "invalid year: '19x9'"),
        ({"year": "1700"}, "invalid year: '1700'"),
        ({"year": "-5"}, "invalid year: '-5'"),
        ({"rating": "great"}, "invalid rating: 'great'"),
        ({"rating": "11"}, "rating out of range: '11'"),
        ({"rating": "-1"}, "rating out of range: '-1'"),
        ({"genres": "Drama, Sci|Fi"}, "invalid genre name containing '|'"),
    ],
)
def test_malformed_rows_raise_with_reason(overrides, reason):
    with pytest.raises(ValueError, match=f"^{reason.replace('|', '[|]')}$"):
        Movie.from_csv_row(row(**overrides))


def test_every_sample_row_parses():
    """The bundled sample has no malformed rows."""
    import csv
    from pathlib import Path

    with (Path(__file__).resolve().parents[2] / "movies.csv").open(newline="") as f:
        for r in csv.DictReader(f):
            Movie.from_csv_row(r)


def test_nul_bytes_are_removed_from_names():
    # Postgres text can't hold NUL; one left in would fail the whole import.
    movie = Movie.from_csv_row(row(movie_name="Glass\x00 Onion", genres="Com\x00edy"))
    assert movie.movie_name == "Glass Onion"
    assert movie.genres == ("Comedy",)


@pytest.mark.parametrize("field", ["year", "rating"])
def test_nul_byte_in_a_number_is_rejected(field):
    with pytest.raises(ValueError, match=f"invalid {field}"):
        Movie.from_csv_row(row(**{field: "5\x00"}))


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"movie_name": "x" * 501}, "movie_name longer than 500 characters"),
        ({"genres": "g" * 65}, "genre name longer than 64 characters"),
        ({"genres": ",".join(f"G{i}" for i in range(21))}, "more than 20 genres"),
        # 500 four-byte characters plus genres: within the character limits, over the byte limit.
        ({"movie_name": "\U0001d538" * 500, "genres": "Drama"}, "movie_name and genres are too long together"),
    ],
)
def test_rows_too_large_for_the_identity_index_are_rejected(overrides, reason):
    with pytest.raises(ValueError, match=f"^{reason}$"):
        Movie.from_csv_row(row(**overrides))


def test_largest_allowed_row_parses():
    movie = Movie.from_csv_row(row(movie_name="x" * 500, genres=",".join(f"{i:02d}" + "g" * 62 for i in range(20))))
    assert len(movie.genres) == 20


@pytest.mark.parametrize("field", ["year", "rating"])
def test_rejection_reasons_quote_at_most_100_characters(field):
    with pytest.raises(ValueError) as exc:
        Movie.from_csv_row(row(**{field: "9" * 5000 if field == "year" else "x" * 5000}))
    assert len(str(exc.value)) < 130
    assert str(exc.value).endswith("...")


@pytest.mark.parametrize(
    ("raw", "imported"),
    [
        ("'=SUM(A1)", "=SUM(A1)"),
        ("'+1", "+1"),
        ("'-30-", "-30-"),
        ("'@home", "@home"),
        ("''=x", "'=x"),  # a real leading quote survives the round trip
        ("'Twas the Night", "'Twas the Night"),  # not an escaped formula
        ("=x", "=x"),
    ],
)
def test_export_formula_escaping_is_removed_on_import(raw, imported):
    movie = Movie.from_csv_row(row(movie_name=raw, genres=raw))
    assert movie.movie_name == imported
    assert movie.genres == (imported,)
