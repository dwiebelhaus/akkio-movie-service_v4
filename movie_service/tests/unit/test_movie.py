import pytest

from app.domain import Movie


def row(**overrides) -> dict[str, str]:
    return {"movie_name": "Glass Onion", "year": "2022", "genres": "Comedy, Crime, Drama", "rating": "7.2"} | overrides


def test_parses_a_full_row():
    movie = Movie.from_csv_row(row())
    assert movie == Movie("Glass Onion", 2022, ("Comedy", "Crime", "Drama"), 7.2)
    assert movie.genre_key == "Comedy|Crime|Drama"
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


@pytest.mark.parametrize("field", ["movie_name", "year", "genres", "rating"])
def test_nul_byte_in_any_field_is_rejected(field):
    with pytest.raises(ValueError, match="NUL byte"):
        Movie.from_csv_row(row(**{field: "a\x00b"}))
