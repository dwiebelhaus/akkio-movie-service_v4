"""End-to-end fixtures: a real, isolated Docker Compose stack driven over HTTP with httpx.

Each test session gets its own Compose project name and random host ports, so runs from
parallel worktrees never share containers or databases. The stack is torn down (with volumes)
when the session ends.
"""

import os
import subprocess
import uuid
from pathlib import Path

import httpx
import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]
API_KEY = "test-api-key"

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # fits the 16.6 MB sample; small enough for quick 413 tests

# Compose reads these when pytest-docker runs `docker compose up`; they override any `.env`.
os.environ.update(
    API_PORT="0",
    POSTGRES_PORT="0",
    API_KEY=API_KEY,
    MAX_UPLOAD_BYTES=str(MAX_UPLOAD_BYTES),
    # Fast crash detection so the stale-job sweep can be tested in seconds.
    JOB_HEARTBEAT_SECONDS="1",
    JOB_STALE_SECONDS="4",
    JOB_SWEEP_INTERVAL_SECONDS="1",
    # Retry Redis quickly after an outage so the fail-open test can watch it recover.
    CACHE_RETRY_SECONDS="1",
    # Small enough to hit the event-stream cap in a test.
    MAX_EVENT_STREAMS="8",
    # Notice a lost dataset-change notification within about a second.
    DATASET_VERSION_REFRESH_SECONDS="1",
)


@pytest.fixture(scope="session")
def docker_compose_file() -> str:
    return str(ROOT / "docker-compose.yml")


@pytest.fixture(scope="session")
def docker_compose_project_name() -> str:
    return f"movie-test-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="session")
def docker_setup() -> list[str]:
    return ["up --build -d --wait"]


@pytest.fixture(scope="session")
def docker_cleanup() -> list[str]:
    # E2E_KEEP_STACK=1 leaves the stack running for debugging (CI sets it to collect logs on
    # failure; its runners are discarded anyway).
    return [] if os.environ.get("E2E_KEEP_STACK") == "1" else ["down -v"]


@pytest.fixture(scope="session")
def api_url(docker_ip: str, docker_services) -> str:
    url = f"http://{docker_ip}:{docker_services.port_for('api', 8000)}"

    def healthy() -> bool:
        try:
            return httpx.get(f"{url}/health", timeout=2).status_code == 200
        except httpx.HTTPError:
            return False

    docker_services.wait_until_responsive(check=healthy, timeout=60, pause=0.5)
    return url


@pytest.fixture(scope="session")
def client(api_url: str):
    with httpx.Client(base_url=api_url, timeout=30) as c:
        yield c


@pytest.fixture(scope="session")
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


@pytest.fixture(scope="session")
def db_url(docker_ip: str, docker_services, api_url: str) -> str:
    """Direct database access, for setting up states the API can't produce (e.g. a dead worker)
    and for integrity checks. `api_url` is required so migrations have run."""
    port = docker_services.port_for("postgres", 5432)
    return f"postgresql://movies:movies@{docker_ip}:{port}/movies"


@pytest.fixture
def db(db_url: str):
    with psycopg.connect(db_url, autocommit=True) as conn:
        yield conn


@pytest.fixture(scope="session")
def compose(docker_compose_file: str, docker_compose_project_name: str):
    """Run a `docker compose` subcommand against the test stack, e.g. `compose("stop", "redis")`."""

    def run(*args: str) -> None:
        subprocess.run(
            ["docker", "compose", "-p", docker_compose_project_name, "-f", docker_compose_file, *args],
            check=True,
            capture_output=True,
        )

    return run
