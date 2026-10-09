"""R5: job status and real-time progress over server-sent events."""

import time

from tests.e2e.helpers import follow_events, make_csv, unique_prefix, upload


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
