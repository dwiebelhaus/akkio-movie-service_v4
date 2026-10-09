"""R1/R2: importing CSVs, deduplication and merging, error handling, all over real HTTP."""

import concurrent.futures
from pathlib import Path

import pytest

from tests.e2e.conftest import MAX_UPLOAD_BYTES
from tests.e2e.helpers import follow_events, import_and_wait, make_csv, unique_prefix, upload

SAMPLE = Path(__file__).resolve().parents[2] / "movies.csv"


def test_import_sample_movies_csv(client, auth_headers, db):
    """The full 367k-row sample: exact duplicates collapse, every movie gets its genre links."""
    job = import_and_wait(client, SAMPLE.read_bytes(), auth_headers)

    assert job["processed_rows"] == 367_314
    result = job["result"]
    assert result["inserted"] == 242_442
    assert result["duplicates_in_file"] == 124_872
    assert result["rejected"] == 0
    # Every movie has exactly one link per genre in its key.
    mismatched = db.execute(
        """
        select count(*) from movies m
         where cardinality(string_to_array(m.genre_key, '|'))
               <> (select count(*) from movie_genres mg where mg.movie_id = m.id)
        """
    ).fetchone()[0]
    assert mismatched == 0


def test_import_returns_202_with_location(client, auth_headers):
    p = unique_prefix()
    resp = upload(client, make_csv([[f"{p}Alpha", "2001", "Drama", "7.0"]]), auth_headers)
    assert resp.status_code == 202
    job = resp.json()
    assert job["type"] == "import"
    assert job["status"] in ("queued", "running", "succeeded")
    assert resp.headers["location"] == f"/api/v1/jobs/{job['id']}"
    assert client.get(resp.headers["location"]).status_code == 200
    follow_events(client, job["id"])


def test_dedup_within_file_and_across_imports(client, auth_headers):
    p = unique_prefix()
    rows = [
        [f"{p}Same", "2020", "Action, Drama", "6.0"],
        [f"{p}Same", "2020", "Drama,Action", ""],  # same genres, different order: duplicate
        [f"{p}  same ", "2020", "Action, Drama", "6.5"],  # case/whitespace-insensitive title
        [f"{p}Same", "2020", "Horror", "5.0"],  # different genres: a different film
        [f"{p}Same", "", "Action, Drama", "4.0"],  # unknown year: a different film
        [f"{p}Same", "II", "Action, Drama", ""],  # disambiguator = unknown year: duplicate of above
    ]
    first = import_and_wait(client, make_csv(rows), auth_headers)["result"]
    assert first["inserted"] == 3
    assert first["duplicates_in_file"] == 3
    assert first["updated"] == first["unchanged"] == 0

    again = import_and_wait(client, make_csv(rows), auth_headers)["result"]
    assert again["inserted"] == 0
    assert again["updated"] == 0
    assert again["unchanged"] == 3


def test_names_are_normalized(client, auth_headers):
    """Genres match case-insensitively (first spelling wins); titles are Unicode-normalized."""
    p = unique_prefix()
    genre = f"G{p.strip()}"
    rows = [
        [f"{p}Am\u00e9lie", "2001", f"{genre}, Comedy", "8.3"],
        [f"{p}Ame\u0301lie\u200b", "2001", f"{genre.upper()},comedy", ""],  # duplicate
        [f"{p}Other", "2001", genre.lower(), ""],
    ]
    result = import_and_wait(client, make_csv(rows), auth_headers)["result"]
    assert result["inserted"] == 2
    assert result["duplicates_in_file"] == 1

    genres = client.get("/api/v1/genres").json()
    assert genre in genres
    assert genre.lower() not in genres and genre.upper() not in genres

    # A later import in different case links to the existing genre.
    later = import_and_wait(client, make_csv([[f"{p}Later", "2002", genre.upper(), ""]]), auth_headers)
    assert later["result"]["inserted"] == 1
    assert client.get("/api/v1/genres").json() == genres

    items = client.get("/api/v1/movies", params={"genre": genre.lower()}).json()["items"]
    assert sorted(m["title"] for m in items) == sorted([f"{p}Am\u00e9lie", f"{p}Other", f"{p}Later"])
    assert all(genre in m["genres"] for m in items)


def test_merge_latest_non_null_rating_wins(client, auth_headers, db):
    p = unique_prefix()
    import_and_wait(client, make_csv([[f"{p}Rated", "2021", "Comedy", "6.5"]]), auth_headers)

    newer = import_and_wait(client, make_csv([[f"{p}Rated", "2021", "Comedy", "6.6"]]), auth_headers)
    assert newer["result"]["updated"] == 1

    blank = import_and_wait(client, make_csv([[f"{p}Rated", "2021", "Comedy", ""]]), auth_headers)
    assert blank["result"]["updated"] == 0
    assert blank["result"]["unchanged"] == 1

    rating = db.execute(
        "select rating from movies where title = %s", (f"{p}Rated",)
    ).fetchone()[0]
    assert float(rating) == 6.6


def test_bad_rows_are_skipped_and_reported(client, auth_headers):
    p = unique_prefix()
    content = make_csv(
        [
            [f"{p}Good", "1999", "Drama", "8.0"],
            [f"{p}Bad rating", "1999", "Drama", "great"],
            ["", "1999", "Drama", "5.0"],
            [f"{p}Bad year", "19x9", "Drama", "5.0"],
            [f"{p}Out of range", "1999", "Drama", "11"],
            [f"{p}Too many", "1999", "Drama", "5.0", "extra"],
            [f"{p}No rating", "2000", "", ""],
        ]
    )
    job = import_and_wait(client, content, auth_headers)
    result = job["result"]
    assert result["inserted"] == 2
    assert result["rejected"] == 5
    reasons = {s["line"]: s["reason"] for s in result["rejected_samples"]}
    assert reasons[3].startswith("invalid rating")
    assert reasons[4] == "missing movie_name"
    assert reasons[5].startswith("invalid year")
    assert reasons[6].startswith("rating out of range")
    assert reasons[7] == "expected 4 fields, got 5"


def test_columns_in_any_order_and_extras_ignored(client, auth_headers):
    p = unique_prefix()
    content = make_csv(
        [["8.1", "Sci-Fi", f"{p}Reordered", "2010", "ignored"]],
        header=["rating", "genres", "movie_name", "year", "director"],
    )
    result = import_and_wait(client, content, auth_headers)["result"]
    assert result["inserted"] == 1
    assert result["warnings"] == ["Ignored extra column: director"]


def test_raw_text_csv_body_is_accepted(client, auth_headers):
    p = unique_prefix()
    resp = client.post(
        "/api/v1/imports",
        headers={**auth_headers, "Content-Type": "text/csv"},
        content=make_csv([[f"{p}Raw", "2005", "Drama", "7.7"]]),
    )
    assert resp.status_code == 202
    assert follow_events(client, resp.json()["id"])[-1][0] == "succeeded"


def test_concurrent_imports_do_not_duplicate(client, auth_headers):
    p = unique_prefix()
    a = make_csv([[f"{p}Shared {i}", "2000", "Drama", "5.0"] for i in range(500)])
    b = make_csv([[f"{p}Shared {i}", "2000", "Drama", "5.0"] for i in range(250, 750)])
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = [f.result()["result"] for f in [pool.submit(import_and_wait, client, c, auth_headers) for c in (a, b)]]
    assert sum(r["inserted"] for r in results) == 750
    assert sum(r["unchanged"] for r in results) == 250


@pytest.mark.parametrize(
    ("content", "code", "message"),
    [
        (b"movie_name,year,genres\nX,2000,Drama\n", "invalid_csv", "Missing required column(s): rating"),
        (b"", "invalid_csv", "File is empty or has no header row"),
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00", "invalid_csv", "File is not valid UTF-8 text"),
        (b"movie_name,year,year,genres,rating\n", "invalid_csv", "Duplicate column(s): year"),
    ],
)
def test_invalid_files_are_rejected_with_422(client, auth_headers, content, code, message):
    resp = upload(client, content, auth_headers)
    assert resp.status_code == 422
    assert resp.json()["error"] == {"code": code, "message": message, "details": None}


def test_missing_file_field_is_422(client, auth_headers):
    resp = client.post("/api/v1/imports", headers=auth_headers, files={"other": ("x.csv", b"a")})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "missing_file"


def test_unsupported_media_type_is_415(client, auth_headers):
    resp = client.post("/api/v1/imports", headers=auth_headers, json={"movie_name": "x"})
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "unsupported_media_type"


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
def test_import_requires_api_key(client, headers):
    resp = upload(client, make_csv([["X", "2000", "Drama", "5"]]), headers)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def _oversized() -> bytes:
    row = b"Some Long Movie Title,2000,\"Action, Drama\",5.0\n"
    return make_csv([]) + row * (MAX_UPLOAD_BYTES // len(row) + 20_000)


def test_oversized_upload_is_413(client, auth_headers):
    resp = upload(client, _oversized(), auth_headers)
    assert resp.status_code == 413
    assert resp.json()["error"]["details"] == {"max_bytes": MAX_UPLOAD_BYTES}


def test_oversized_chunked_upload_is_413(client, auth_headers):
    """No Content-Length: the limit is enforced while streaming."""
    body = _oversized()

    def chunks():
        for i in range(0, len(body), 256 * 1024):
            yield body[i : i + 256 * 1024]

    resp = client.post(
        "/api/v1/imports", headers={**auth_headers, "Content-Type": "text/csv"}, content=chunks()
    )
    assert resp.status_code == 413


_BOUNDARY = "e2e-boundary"


def _multipart(*parts: tuple[str, bytes], chunk: int = 1024 * 1024):
    """A chunked (no Content-Length) multipart body: `(headers, data)` parts, then the end."""
    for headers, data in parts:
        yield f"--{_BOUNDARY}\r\n{headers}\r\n\r\n".encode()
        for i in range(0, len(data), chunk):
            yield data[i : i + chunk]
        yield b"\r\n"
    yield f"--{_BOUNDARY}--\r\n".encode()


def _post_multipart(client, auth_headers, *parts):
    return client.post(
        "/api/v1/imports",
        headers={**auth_headers, "Content-Type": f"multipart/form-data; boundary={_BOUNDARY}"},
        content=_multipart(*parts),
    )


def test_oversized_non_file_multipart_field_is_413(client, auth_headers):
    """The size limit covers the whole body, not just the `file` part."""
    junk = ('Content-Disposition: form-data; name="junk"', b"x" * (MAX_UPLOAD_BYTES + 1024 * 1024))
    file = (
        'Content-Disposition: form-data; name="file"; filename="m.csv"\r\nContent-Type: text/csv',
        make_csv([["Tiny", "2000", "Drama", "5"]]),
    )
    resp = _post_multipart(client, auth_headers, junk, file)
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"


@pytest.mark.parametrize(
    "headers",
    [
        'Content-Disposition: form-data; name="file"; filename="m.csv"\r\nX-Junk: ' + "a" * 20_000,
        'Content-Disposition: form-data; name="file"; filename="m.csv"'
        + "".join(f"\r\nX-Junk-{i}: a" for i in range(100)),
    ],
    ids=["long-header", "many-headers"],
)
def test_oversized_multipart_part_headers_are_422(client, auth_headers, headers):
    resp = _post_multipart(client, auth_headers, (headers, make_csv([["Tiny", "2000", "Drama", "5"]])))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_multipart"
