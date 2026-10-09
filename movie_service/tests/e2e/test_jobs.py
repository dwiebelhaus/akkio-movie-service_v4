"""R5: job status and real-time progress over server-sent events."""

import contextlib
import json
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


def _insert_running_job(db) -> int:
    """A job that stays `running` (no worker owns it; a future heartbeat keeps the sweep off)."""
    return db.execute(
        "insert into jobs (type, status, started_at, heartbeat_at)"
        " values ('import', 'running', now(), now() + interval '1 hour') returning id"
    ).fetchone()[0]


def _finish(db, job_id: int) -> None:
    db.execute("update jobs set status = 'failed', error = 'test done', finished_at = now() where id = %s", (job_id,))
    db.execute("select pg_notify('job_progress', %s)", (str(job_id),))


def _open_stream(stack: contextlib.ExitStack, client, job_id: int):
    """Open an SSE stream and read its first event, so the server has subscribed.

    Keep the returned iterator referenced: when it's garbage-collected, httpx closes the
    connection."""
    resp = stack.enter_context(client.stream("GET", f"/api/v1/jobs/{job_id}/events", timeout=30))
    assert resp.status_code == 200
    lines = resp.iter_lines()
    next(line for line in lines if line.startswith("data:"))
    return lines


def _job_pkey_scans(db) -> int:
    return db.execute(
        "select idx_scan from pg_stat_user_indexes where indexrelname = 'jobs_pkey'"
    ).fetchone()[0]


# Backends flush table statistics at most every 10s while idle (PGSTAT_IDLE_INTERVAL).
_STATS_FLUSH_SECONDS = 11


def test_streams_of_one_job_share_one_read_per_change(client, db):
    """N clients watching a job cost about one job read per change, not N."""
    job_id = _insert_running_job(db)
    streams, changes = 6, 20
    with contextlib.ExitStack() as stack:
        open_streams = [_open_stream(stack, client, job_id) for _ in range(streams)]
        time.sleep(_STATS_FLUSH_SECONDS)
        before = _job_pkey_scans(db)
        for i in range(changes):
            db.execute("update jobs set progress = %s where id = %s", ((i + 1) / 100, job_id))
            db.execute("select pg_notify('job_progress', %s)", (str(job_id),))
            time.sleep(0.05)
        time.sleep(_STATS_FLUSH_SECONDS)
        scans = _job_pkey_scans(db) - before
        _finish(db, job_id)
        # Every stream stayed connected and gets the final event.
        assert all(any(line == "event: failed" for line in lines) for lines in open_streams)
    # Our own updates use the index too (one per change), plus a little worker noise. Reading
    # once per stream per change would be about (streams + 1) * changes = 140.
    assert scans < 4 * changes, scans


def test_event_streams_are_capped_per_process(client, db):
    job_id = _insert_running_job(db)
    try:
        with contextlib.ExitStack() as stack:
            open_streams = [_open_stream(stack, client, job_id) for _ in range(8)]  # MAX_EVENT_STREAMS
            # Streamed, so a wrongly accepted stream fails the test instead of hanging it.
            with client.stream("GET", f"/api/v1/jobs/{job_id}/events", timeout=10) as resp:
                assert resp.status_code == 503
                assert resp.headers["retry-after"] == "5"
                assert json.loads(resp.read())["error"]["code"] == "too_many_streams"
            assert client.get(f"/api/v1/jobs/{job_id}").status_code == 200  # polling still works
            assert len(open_streams) == 8
        # Closed streams free their slots (the server notices the disconnects shortly).
        deadline = time.monotonic() + 10
        while True:
            with client.stream("GET", f"/api/v1/jobs/{job_id}/events", timeout=30) as resp:
                if resp.status_code == 200:
                    break
            assert time.monotonic() < deadline, "stream slots were never released"
            time.sleep(0.2)
    finally:
        _finish(db, job_id)
