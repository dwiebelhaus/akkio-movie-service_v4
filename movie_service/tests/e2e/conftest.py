"""End-to-end fixtures: a real, isolated Docker Compose stack driven over HTTP with httpx.

Each test session gets its own Compose project name and random host ports, so runs from
parallel worktrees never share containers or databases. The stack is torn down (with volumes)
when the session ends.
"""

import os
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
API_KEY = "test-api-key"

# Compose reads these when pytest-docker runs `docker compose up`; they override any `.env`.
os.environ.update(API_PORT="0", POSTGRES_PORT="0", API_KEY=API_KEY)


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
