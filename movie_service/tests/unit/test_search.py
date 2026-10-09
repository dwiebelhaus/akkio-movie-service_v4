import pytest
from pydantic import ValidationError

from app.errors import ApiError
from app.schemas import MovieFilter
from app.services.http_cache import _etag_matches
from app.services.movie_search import decode_cursor, encode_cursor


@pytest.mark.parametrize("movie_id", [1, 42, 9_999_999_999])
def test_cursor_round_trip(movie_id):
    cursor = encode_cursor(movie_id)
    assert "=" not in cursor
    assert decode_cursor(cursor) == movie_id


def test_missing_cursor_starts_at_beginning():
    assert decode_cursor(None) == 0
    assert decode_cursor("") == 0


@pytest.mark.parametrize("bad", ["!!!", "bm90LWEtbnVtYmVy", "LTE"])  # garbage, "not-a-number", "-1"
def test_invalid_cursor(bad):
    with pytest.raises(ApiError) as exc:
        decode_cursor(bad)
    assert exc.value.code == "invalid_cursor"


def test_filter_defaults():
    flt = MovieFilter()
    assert (flt.genre, flt.genre_match, flt.limit) == ([], "any", 50)


def test_filter_rejects_inverted_year_range():
    with pytest.raises(ValidationError, match="year_from must be less than or equal to year_to"):
        MovieFilter(year_from=2001, year_to=2000)


def test_filter_allows_single_year():
    assert MovieFilter(year_from=2000, year_to=2000).year_to == 2000


def test_filter_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        MovieFilter(genres=["Drama"])


@pytest.mark.parametrize(
    ("header", "matches"),
    [
        (None, False),
        ('"v1-abc"', True),
        ('W/"v1-abc"', True),
        ('"v0-xyz", "v1-abc"', True),
        ("*", False),  # handled by cached_json, once the resource is known to exist
        ('"v2-abc"', False),
    ],
)
def test_etag_matching(header, matches):
    assert _etag_matches(header, '"v1-abc"') is matches


@pytest.mark.parametrize("movie_id", [2**63, 10**30])
def test_cursor_beyond_bigint_is_invalid(movie_id):
    with pytest.raises(ApiError) as exc:
        decode_cursor(encode_cursor(movie_id))
    assert exc.value.code == "invalid_cursor"


def test_cursor_at_bigint_max_is_valid():
    assert decode_cursor(encode_cursor(2**63 - 1)) == 2**63 - 1
