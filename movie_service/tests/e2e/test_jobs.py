"""R5: job status and real-time progress over server-sent events."""

import time

from tests.e2e.helpers import follow_events, import_and_wait, make_csv, unique_prefix, upload


def test_sse_streams_progress_then_final_event(client, auth_headers):
    p = unique_prefix()
    content = make_csv([[f"{p}Movie {i}", str(1950 + i % 70), "Drama, Comedy", "6.1"] for i in range(60_000)])
    job_id = upload(client, content, auth_headers).json()["id"]

    events = follow_events(client, job_id)

    names = [name for name, _ in events]
    assert names[-1] == "succeeded"
    assert set(names[:-1]) <= {"progress"}
    progress = [job["progress"] for _, job in events]
    assert progress == sorted(progress), "progress never goes backwards"
    final = events[-1][1]
    assert final["progress"] == 1.0
    assert final["processed_rows"] == final["total_rows"] == 60_000
    assert final["finished_at"] is not None


def test_sse_for_finished_job_sends_one_final_event(client, auth_headers):
    p = unique_prefix()
    job_id = upload(client, make_csv([[f"{p}Done", "2000", "Drama", "5"]]), auth_headers).json()["id"]
    follow_events(client, job_id)

    events = follow_events(client, job_id)
    assert [name for name, _ in events] == ["succeeded"]


def test_job_snapshot(client, auth_headers):
    p = unique_prefix()
    job_id = upload(client, make_csv([[f"{p}Snap", "2000", "Drama", "5"]]), auth_headers).json()["id"]
    follow_events(client, job_id)

    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["status"] == "succeeded"
    assert job["result"]["inserted"] == 1
    assert "params" not in job  # internal input isn't exposed


def test_unknown_job_is_404(client):
    for path in ("/api/v1/jobs/999999999", "/api/v1/jobs/999999999/events"):
        resp = client.get(path)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "job_not_found"


def test_invalid_job_id_is_422(client):
    assert client.get("/api/v1/jobs/abc").status_code == 422


def test_job_with_dead_worker_is_failed_and_stream_closes(client, db):
    """A running job whose heartbeat stopped is failed by the sweep; SSE clients get a final event."""
    job_id = db.execute(
        "insert into jobs (type, status, started_at, heartbeat_at)"
        " values ('import', 'running', now() - interval '1 hour', now() - interval '1 hour')"
        " returning id"
    ).fetchone()[0]

    start = time.monotonic()
    events = follow_events(client, job_id, timeout=30)
    assert time.monotonic() - start < 20
    name, job = events[-1]
    assert name == "failed"
    assert job["error"] == "Worker stopped responding"


def test_job_failed_while_running_is_not_committed(client, auth_headers, db):
    """If the stale-job sweep fails a job that is in fact still running (e.g. its heartbeats were
    delayed), the worker must not flip it to succeeded later, nor commit its data."""
    p = unique_prefix()
    content = make_csv([[f"{p}Movie {i}", str(1950 + i % 70), "Drama", "6.1"] for i in range(300_000)])
    job_id = upload(client, content, auth_headers).json()["id"]

    deadline = time.monotonic() + 30
    while db.execute("select status from jobs where id = %s", (job_id,)).fetchone()[0] != "running":
        assert time.monotonic() < deadline, "job never started"
        time.sleep(0.02)
    # Exactly what the sweep does to a job it believes is dead.
    swept = db.execute(
        "update jobs set status = 'failed', error = 'Worker stopped responding', finished_at = now()"
        " where id = %s and status = 'running'",
        (job_id,),
    ).rowcount
    assert swept == 1

    # One worker runs jobs in order, so once a later job is done, the swept one has finished too.
    import_and_wait(client, make_csv([[f"{p}Sentinel", "2000", "Drama", "5"]]), auth_headers)

    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["status"] == "failed"
    assert job["error"] == "Worker stopped responding"
    assert job["result"] is None
    inserted = db.execute("select count(*) from movies where title like %s", (f"{p}Movie %",)).fetchone()[0]
    assert inserted == 0
