"""R4: export the whole database as a gzipped CSV, download it, and reuse unchanged exports."""

import csv
import gzip
import io

import pytest

from tests.e2e.helpers import follow_events, import_and_wait, make_csv, unique_prefix, upload


def export_and_wait(client, auth_headers) -> dict:
    resp = client.post("/api/v1/exports", headers=auth_headers)
    assert resp.status_code in (200, 202), resp.text
    assert resp.headers["location"] == f"/api/v1/jobs/{resp.json()['id']}"
    name, job = follow_events(client, resp.json()["id"])[-1]
    assert name == "succeeded", job
    return job


def download(client, job_id: int) -> bytes:
    resp = client.get(f"/api/v1/exports/{job_id}/file")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/gzip"
    assert resp.headers["content-disposition"].startswith('attachment; filename="movies-v')
    assert int(resp.headers["content-length"]) == len(resp.content)
    return resp.content


def make_change(client, auth_headers) -> None:
    """An import that inserts a movie, so the dataset version moves on."""
    import_and_wait(client, make_csv([[f"{unique_prefix()}Change", "2000", "Drama", "5"]]), auth_headers)


def test_export_requires_api_key(client):
    assert client.post("/api/v1/exports").status_code == 401
    assert client.post("/api/v1/exports", headers={"X-API-Key": "wrong"}).status_code == 401


def test_export_contains_every_movie_in_import_format(client, auth_headers, db):
    make_change(client, auth_headers)  # make sure this test gets a fresh export
    job = export_and_wait(client, auth_headers)
    movie_count = db.execute("select count(*) from movies").fetchone()[0]
    assert job["result"]["rows"] == job["processed_rows"] == movie_count
    assert job["result"]["download_url"] == f"/api/v1/exports/{job['id']}/file"

    rows = list(csv.reader(io.StringIO(gzip.decompress(download(client, job["id"])).decode())))
    assert rows[0] == ["movie_name", "year", "genres", "rating"]
    assert len(rows) - 1 == movie_count


def test_export_round_trips_through_import(client, auth_headers):
    p = unique_prefix()
    import_and_wait(
        client,
        make_csv(
            [
                [f'{p}Comma, "Quoted" Title', "1999", "Drama, Action", "7.5"],
                [f"{p}No year or rating", "", "Comedy", ""],
                [f"{p}No genres", "2001", "", "6.0"],
            ]
        ),
        auth_headers,
    )
    job = export_and_wait(client, auth_headers)
    content = gzip.decompress(download(client, job["id"]))
    lines = content.decode().splitlines()
    assert f'"{p}Comma, ""Quoted"" Title",1999,"Action, Drama",7.5' in lines
    assert f"{p}No year or rating,,Comedy," in lines
    assert f"{p}No genres,2001,,6.0" in lines

    reimported = import_and_wait(client, content, auth_headers)["result"]
    assert reimported["inserted"] == reimported["updated"] == reimported["rejected"] == 0
    assert reimported["unchanged"] == job["result"]["rows"]


def test_unchanged_data_reuses_export_and_changes_replace_it(client, auth_headers):
    make_change(client, auth_headers)
    first = export_and_wait(client, auth_headers)

    again = client.post("/api/v1/exports", headers=auth_headers)
    assert again.status_code == 200
    assert again.json()["id"] == first["id"]

    make_change(client, auth_headers)
    newer = client.post("/api/v1/exports", headers=auth_headers)
    assert newer.status_code == 202
    assert newer.json()["id"] != first["id"]
    follow_events(client, newer.json()["id"])

    expired = client.get(f"/api/v1/exports/{first['id']}/file")
    assert expired.status_code == 410
    assert expired.json()["error"]["code"] == "export_expired"


def test_concurrent_export_requests_share_one_job(client, auth_headers):
    import concurrent.futures

    make_change(client, auth_headers)
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        responses = list(pool.map(lambda _: client.post("/api/v1/exports", headers=auth_headers), range(4)))
    ids = {r.json()["id"] for r in responses}
    assert len(ids) == 1
    assert sorted(r.status_code for r in responses) == [200, 200, 200, 202]
    follow_events(client, ids.pop())


@pytest.mark.parametrize(
    ("status", "code"),
    [("running", "export_not_ready"), ("failed", "export_failed")],
)
def test_download_unfinished_or_failed_export_is_409(client, db, status, code):
    # A job row no worker will touch: not queued, heartbeat in the future, old dataset version.
    job_id = db.execute(
        "insert into jobs (type, status, params, heartbeat_at, error)"
        " values ('export', %s, '{\"dataset_version\": -1}', now() + interval '1 hour', 'boom')"
        " returning id",
        (status,),
    ).fetchone()[0]
    try:
        resp = client.get(f"/api/v1/exports/{job_id}/file")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == code
    finally:
        db.execute("update jobs set status = 'failed' where id = %s", (job_id,))


def test_download_of_non_export_or_unknown_job_is_404(client, auth_headers):
    import_job = upload(client, make_csv([[f"{unique_prefix()}X", "2000", "Drama", "5"]]), auth_headers).json()
    follow_events(client, import_job["id"])
    for job_id in (import_job["id"], 999_999_999):
        resp = client.get(f"/api/v1/exports/{job_id}/file")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "export_not_found"


def test_export_escapes_formulas_and_still_round_trips(client, auth_headers, db):
    """CSV injection: names starting with = + - @ are exported with a leading `'`, which the
    importer removes again."""
    p = unique_prefix()
    genre = f"-{p.strip()}"
    rows = [
        [f"={p}Sum", "2001", genre, ""],
        [f"''+{p}Quoted", "2001", "Drama", ""],  # an escaped real leading quote: stored as '+...
        [f"{p}-Inner", "2001", "Drama", ""],  # only a leading character matters
    ]
    import_and_wait(client, make_csv(rows), auth_headers)
    titles = {r[0] for r in db.execute("select title from movies where title like %s", (f"%{p}%",))}
    assert titles == {f"={p}Sum", f"'+{p}Quoted", f"{p}-Inner"}

    job = export_and_wait(client, auth_headers)
    content = gzip.decompress(download(client, job["id"])).decode()
    exported = [r for r in csv.reader(io.StringIO(content)) if p in r[0]]
    assert sorted(exported) == sorted(
        [
            [f"'={p}Sum", "2001", f"'{genre}", ""],
            [f"''+{p}Quoted", "2001", "Drama", ""],
            [f"{p}-Inner", "2001", "Drama", ""],
        ]
    )
    for field in (f for r in exported for f in r):
        assert not field.startswith(("=", "+", "-", "@"))

    again = import_and_wait(client, make_csv(exported), auth_headers)["result"]
    assert again["inserted"] == again["updated"] == 0
    assert again["unchanged"] == 3
