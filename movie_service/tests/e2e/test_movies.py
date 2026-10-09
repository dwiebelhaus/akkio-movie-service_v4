"""R3: search by year range and genres, pagination, and caching (ETag/304, Redis), over real HTTP.

Each test creates its own uniquely named genres, so filters isolate its movies from
everything else in the shared test database.
"""

import time
import uuid

import pytest

from tests.e2e.helpers import import_and_wait, make_csv


def genre_name() -> str:
    return f"G{uuid.uuid4().hex[:10]}"


def search(client, **params):
    resp = client.get("/api/v1/movies", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def titles(page) -> list[str]:
    return [m["title"] for m in page["items"]]


@pytest.fixture
def catalog(client, auth_headers):
    """Five movies across two private genres and a few years."""
    a, b = genre_name(), genre_name()
    rows = [
        ["Old A", "1950", a, "6.0"],
        ["Mid AB", "1990", f"{a}, {b}", "7.5"],
        ["Mid B", "1995", b, ""],
        ["New A", "2010", a, "8.0"],
        ["Unknown year AB", "", f"{a}, {b}", "5.5"],
    ]
    import_and_wait(client, make_csv(rows), auth_headers)
    return a, b


def test_filter_by_single_genre(client, catalog):
    a, _ = catalog
    page = search(client, genre=a)
    assert titles(page) == ["Old A", "Mid AB", "New A", "Unknown year AB"]
    assert page["next_cursor"] is None


def test_movie_shape(client, catalog):
    a, b = catalog
    (movie,) = search(client, genre=b, year_from=1990, year_to=1990)["items"]
    assert set(movie) == {"id", "title", "year", "genres", "rating"}
    assert movie["title"] == "Mid AB"
    assert movie["year"] == 1990
    assert movie["genres"] == sorted([a, b])
    assert movie["rating"] == 7.5


def test_year_range_is_inclusive_and_excludes_unknown_years(client, catalog):
    a, _ = catalog
    assert titles(search(client, genre=a, year_from=1950, year_to=1990)) == ["Old A", "Mid AB"]
    assert titles(search(client, genre=a, year_from=1991)) == ["New A"]
    assert titles(search(client, genre=a, year_to=1950)) == ["Old A"]


def test_genre_match_any_and_all(client, catalog):
    a, b = catalog
    assert titles(search(client, genre=[a, b])) == ["Old A", "Mid AB", "Mid B", "New A", "Unknown year AB"]
    assert titles(search(client, genre=[a, b], genre_match="all")) == ["Mid AB", "Unknown year AB"]
    assert titles(search(client, genre=[a, b], genre_match="all", year_from=1900)) == ["Mid AB"]


def test_genre_names_are_case_insensitive(client, catalog):
    a, _ = catalog
    assert titles(search(client, genre=a.lower())) == titles(search(client, genre=a))


def test_cursor_pagination_walks_all_results(client, auth_headers):
    g = genre_name()
    import_and_wait(client, make_csv([[f"Paged {i:02}", "2001", g, "5"] for i in range(23)]), auth_headers)

    seen, cursor, pages = [], None, 0
    while True:
        params = {"genre": g, "limit": 10} | ({"cursor": cursor} if cursor else {})
        page = search(client, **params)
        seen += titles(page)
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert seen == [f"Paged {i:02}" for i in range(23)]


def test_get_movie_by_id(client, catalog):
    a, _ = catalog
    movie = search(client, genre=a, limit=1)["items"][0]
    resp = client.get(f"/api/v1/movies/{movie['id']}")
    assert resp.status_code == 200
    assert resp.json() == movie


def test_unknown_movie_is_404(client):
    resp = client.get("/api/v1/movies/999999999")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "movie_not_found"


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"year_from": 2000, "year_to": 1990}, "validation_error"),
        ({"year_from": "abc"}, "validation_error"),
        ({"year_from": 1000}, "validation_error"),
        ({"limit": 0}, "validation_error"),
        ({"limit": 1001}, "validation_error"),
        ({"genre_match": "some"}, "validation_error"),
        ({"genres": "Drama"}, "validation_error"),  # unknown parameter, likely a typo
        ({"cursor": "not-a-cursor!"}, "invalid_cursor"),
        ({"genre": "No Such Genre"}, "unknown_genre"),
    ],
)
def test_invalid_search_parameters_are_422(client, params, code):
    resp = client.get("/api/v1/movies", params=params)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == code


def test_etag_304_and_cache_control(client, catalog):
    a, _ = catalog
    first = client.get("/api/v1/movies", params={"genre": a})
    etag = first.headers["etag"]
    assert first.headers["cache-control"] == "public, max-age=3600"

    revalidated = client.get("/api/v1/movies", params={"genre": a}, headers={"If-None-Match": etag})
    assert revalidated.status_code == 304
    assert revalidated.content == b""
    assert revalidated.headers["etag"] == etag

    other = client.get("/api/v1/movies", params={"genre": a, "limit": 2}, headers={"If-None-Match": etag})
    assert other.status_code == 200, "a different query has a different ETag"


def test_shared_cache_hit_on_repeat(client, catalog):
    a, _ = catalog
    params = {"genre": a, "limit": 3}
    first = client.get("/api/v1/movies", params=params)
    second = client.get("/api/v1/movies", params=params)
    assert first.headers["x-cache"] == "MISS"
    assert second.headers["x-cache"] == "HIT"
    assert second.json() == first.json()


def test_import_that_changes_data_invalidates_caches(client, auth_headers, catalog):
    a, _ = catalog
    before = client.get("/api/v1/movies", params={"genre": a})
    etag = before.headers["etag"]

    import_and_wait(client, make_csv([["Newer A", "2020", a, "9.0"]]), auth_headers)

    deadline = time.monotonic() + 5
    while True:  # the new dataset version reaches the API via NOTIFY, within milliseconds
        after = client.get("/api/v1/movies", params={"genre": a}, headers={"If-None-Match": etag})
        if after.status_code == 200 or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    assert after.status_code == 200
    assert after.headers["etag"] != etag
    assert after.headers["x-cache"] == "MISS"
    assert "Newer A" in titles(after.json())


def test_import_without_changes_keeps_caches(client, auth_headers, catalog):
    a, _ = catalog
    etag = client.get("/api/v1/movies", params={"genre": a}).headers["etag"]
    import_and_wait(client, make_csv([["Old A", "1950", a, "6.0"]]), auth_headers)  # unchanged
    resp = client.get("/api/v1/movies", params={"genre": a}, headers={"If-None-Match": etag})
    assert resp.status_code == 304


def test_search_works_while_redis_is_down(client, catalog, compose, docker_services):
    a, _ = catalog
    expected = titles(search(client, genre=a))
    compose("stop", "redis")
    try:
        resp = client.get("/api/v1/movies", params={"genre": a, "limit": 4})
        assert resp.status_code == 200
        assert resp.headers["x-cache"] == "MISS"
        assert titles(resp.json()) == expected[:4]
        assert client.get("/health").json()["cache"] == "unavailable"
    finally:
        compose("start", "redis")
    docker_services.wait_until_responsive(
        check=lambda: client.get("/health").json()["cache"] == "ok", timeout=30, pause=0.5
    )


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"year_from": 1990, "year_to": 1999},
        {"genre": "Drama"},
        {"genre": ["Drama", "Romance"], "genre_match": "all", "year_from": 1990, "year_to": 1999},
        {"genre": "Short"},
        {"limit": 1000},
    ],
)
def test_search_is_fast_on_the_sample(client, params):
    """Well under the 2 s budget on the full sample (imported by test_imports)."""
    start = time.monotonic()
    resp = client.get("/api/v1/movies", params=params)
    elapsed = time.monotonic() - start
    if resp.status_code == 422:  # sample genres not present (test run in isolation)
        pytest.skip("sample data not imported")
    assert resp.status_code == 200
    assert elapsed < 1.0


def test_list_genres(client, catalog):
    resp = client.get("/api/v1/genres")
    assert resp.status_code == 200
    genres = resp.json()
    assert set(catalog) <= set(genres)
    assert genres == sorted(genres)
    assert client.get("/api/v1/genres", headers={"If-None-Match": resp.headers["ETag"]}).status_code == 304


def test_openapi_enumerates_genres(client, catalog):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    params = resp.json()["paths"]["/api/v1/movies"]["get"]["parameters"]
    (genre,) = [p for p in params if p["name"] == "genre"]
    assert set(catalog) <= set(genre["schema"]["items"]["enum"])
    assert client.get("/docs").status_code == 200
