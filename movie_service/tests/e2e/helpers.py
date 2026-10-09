"""Shared helpers for end-to-end tests: build CSVs, upload them, follow job progress over SSE."""

import csv
import io
import json
import uuid

import httpx

HEADER = ["movie_name", "year", "genres", "rating"]


def unique_prefix() -> str:
    """Titles prefixed per test so tests sharing one database never collide."""
    return f"t{uuid.uuid4().hex[:8]} "


def make_csv(rows: list[list[str]], header: list[str] = HEADER) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode()


def upload(client: httpx.Client, content: bytes, headers: dict, filename: str = "movies.csv") -> httpx.Response:
    return client.post("/api/v1/imports", headers=headers, files={"file": (filename, content, "text/csv")})


def follow_events(client: httpx.Client, job_id: int, timeout: float = 120) -> list[tuple[str, dict]]:
    """Read the job's SSE stream until it closes; returns (event, JobRead) pairs."""
    events: list[tuple[str, dict]] = []
    event = None
    with client.stream("GET", f"/api/v1/jobs/{job_id}/events", timeout=timeout) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                events.append((event, json.loads(line.split(":", 1)[1])))
    return events


def import_and_wait(client: httpx.Client, content: bytes, headers: dict) -> dict:
    """Upload a CSV, wait for the job to finish, and return the final JobRead."""
    resp = upload(client, content, headers)
    assert resp.status_code == 202, resp.text
    events = follow_events(client, resp.json()["id"])
    final_event, job = events[-1]
    assert final_event == job["status"] == "succeeded", job
    return job
