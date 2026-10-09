"""Startup safety and recovery from infrastructure failures."""

import subprocess

import httpx
import pytest

from tests.e2e.conftest import API_KEY
from tests.e2e.helpers import import_and_wait, make_csv, unique_prefix


def _restart_count(compose, service: str) -> int:
    container = compose("ps", "-q", service).stdout.split()[0]
    out = subprocess.run(
        ["docker", "inspect", "-f", "{{.RestartCount}}", container], check=True, capture_output=True, text=True
    ).stdout
    return int(out)


def _healthy(client: httpx.Client) -> bool:
    try:
        return client.get("/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.parametrize("service", ["api", "worker"])
@pytest.mark.parametrize("key", ["", "dev-api-key"])
def test_services_refuse_to_start_without_a_strong_api_key(compose, api_url, service, key):
    """No built-in default key: a deployment that forgets API_KEY fails loudly instead of
    accepting writes with a well-known key. The rejected value is never logged."""
    proc = compose("run", "--rm", "--no-deps", "-e", f"API_KEY={key}", service, check=False, timeout=60)
    assert proc.returncode != 0
    output = proc.stdout + proc.stderr
    assert "api_key" in output
    if key:
        assert key not in output
    assert API_KEY not in output


def test_worker_and_api_survive_a_database_restart(client, auth_headers, compose, docker_services):
    """Pooled connections die with the database; the worker and API must recover in place
    (not crash and rely on a container restart) and keep processing jobs."""
    before = {service: _restart_count(compose, service) for service in ("api", "worker")}

    compose("restart", "postgres")
    docker_services.wait_until_responsive(check=lambda: _healthy(client), timeout=60, pause=0.5)

    p = unique_prefix()
    job = import_and_wait(client, make_csv([[f"{p}After restart", "2001", "Drama", "7.0"]]), auth_headers)
    assert job["result"]["inserted"] == 1
    for year in range(1990, 2005):  # distinct queries (no cache hits), more than the pool size
        assert client.get("/api/v1/movies", params={"limit": 1, "year_from": year}).status_code == 200
    assert {service: _restart_count(compose, service) for service in ("api", "worker")} == before


def test_redis_requires_a_password(client, compose):
    noauth = compose("exec", "-T", "redis", "redis-cli", "ping", check=False)
    assert "NOAUTH" in noauth.stdout + noauth.stderr
    assert client.get("/health").json()["cache"] == "ok"  # the API connects with the password

    proc = compose("run", "--rm", "--no-deps", "-e", "REDIS_PASSWORD=", "redis", check=False, timeout=60)
    assert proc.returncode != 0
    assert "REDIS_PASSWORD is required" in proc.stdout + proc.stderr
