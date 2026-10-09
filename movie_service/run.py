"""One-command runner for the movie service (DESIGN.md §11).

    uv run python run.py up [--seed] [--test]
                                           build and start the stack, wait until healthy;
                                           --seed imports movies.csv and shows progress
    uv run python run.py export [-o FILE]  download the whole database as a gzipped CSV
    uv run python run.py test [pytest args] run the test suite (e2e tests start their own isolated stack)
    uv run python run.py down [--volumes]   stop the stack; --volumes also wipes the data
    uv run python run.py logs               follow the service logs
"""

import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMPOSE = ["docker", "compose", "--project-directory", str(ROOT)]
PREFERRED_API_PORT = 8000


def compose(
    *args: str, check: bool = True, capture: bool = False, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*COMPOSE, *args], cwd=ROOT, check=check, text=True, capture_output=capture, env=env
    )


def published_api_port() -> int | None:
    """Host port of the running API container, if any."""
    out = compose("port", "api", "8000", check=False, capture=True).stdout.strip()
    port = out.rsplit(":", 1)[-1]
    return int(port) if port.isdigit() and port != "0" else None


def api_url() -> str:
    port = published_api_port()
    if port is None:
        raise SystemExit("The API is not running; start it with `uv run python run.py up`.")
    return f"http://localhost:{port}"


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def choose_api_port() -> str:
    """API_PORT if set; else keep the running stack's port; else 8000, or the next free port."""
    if configured := env_value("API_PORT", ""):
        return configured
    if (running := published_api_port()) is not None:
        return str(running)
    for port in range(PREFERRED_API_PORT, PREFERRED_API_PORT + 100):
        if port_is_free(port):
            if port != PREFERRED_API_PORT:
                print(f"Port {PREFERRED_API_PORT} is in use; using {port} for the API.")
            return str(port)
    return "0"  # let Docker pick


def env_value(name: str, default: str) -> str:
    """Read a setting the way Compose does: environment first, then `.env`."""
    if name in os.environ:
        return os.environ[name]
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == name:
                return value.strip().strip("'\"")
    return default


def follow_job(client, job_id: int) -> tuple[str | None, dict]:
    """Print a job's SSE progress until it finishes; returns the final (event, job)."""
    event, job = None, {}
    with client.stream("GET", f"/api/v1/jobs/{job_id}/events") as stream:
        for line in stream.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                job = json.loads(line.split(":", 1)[1])
                rows = job.get("processed_rows") or 0
                print(f"\r  job {job_id}: {job['status']:<9} {job['progress']:6.1%}  {rows:>9,} rows", end="", flush=True)
    print()
    return event, job


def _client(url: str):
    import httpx

    return httpx.Client(base_url=url, timeout=httpx.Timeout(30, read=None))


def seed(url: str, csv_path: Path) -> int:
    """Upload a CSV through the API and follow the job's SSE progress stream."""
    headers = {"X-API-Key": env_value("API_KEY", "dev-api-key")}
    print(f"Importing {csv_path.name} ({csv_path.stat().st_size / 1e6:.1f} MB)...")
    with _client(url) as client:
        with csv_path.open("rb") as f:
            resp = client.post("/api/v1/imports", headers=headers, files={"file": (csv_path.name, f, "text/csv")})
        if resp.status_code != 202:
            print(f"import rejected ({resp.status_code}): {resp.text}", file=sys.stderr)
            return 1
        event, job = follow_job(client, resp.json()["id"])
    if event != "succeeded":
        print(f"import failed: {job.get('error')}", file=sys.stderr)
        return 1
    result = {k: v for k, v in job["result"].items() if k != "rejected_samples"}
    print("  " + ", ".join(f"{k}={v}" for k, v in result.items()))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Request an export (or reuse a current one), follow progress, and download it."""
    headers = {"X-API-Key": env_value("API_KEY", "dev-api-key")}
    with _client(api_url()) as client:
        resp = client.post("/api/v1/exports", headers=headers)
        if resp.status_code not in (200, 202):
            print(f"export rejected ({resp.status_code}): {resp.text}", file=sys.stderr)
            return 1
        job_id = resp.json()["id"]
        print("Reusing current export" if resp.status_code == 200 else "Exporting...")
        event, job = follow_job(client, job_id)
        if event != "succeeded":
            print(f"export failed: {job.get('error')}", file=sys.stderr)
            return 1
        out = Path(args.output)
        with client.stream("GET", f"/api/v1/exports/{job_id}/file") as stream, out.open("wb") as f:
            stream.raise_for_status()
            for chunk in stream.iter_bytes():
                f.write(chunk)
    print(f"  saved {job['result']['rows']:,} movies to {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    compose("up", "-d", "--build", "--wait", env={**os.environ, "API_PORT": choose_api_port()})
    url = api_url()
    print(f"\nMovie API is up: {url}  (docs: {url}/docs)")
    if args.seed and (code := seed(url, ROOT / "movies.csv")):
        return code
    if args.test:
        return cmd_test(argparse.Namespace(pytest_args=[]))
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    return subprocess.run([sys.executable, "-m", "pytest", *args.pytest_args], cwd=ROOT).returncode


def cmd_down(args: argparse.Namespace) -> int:
    compose("down", *(["--volumes"] if args.volumes else []))
    return 0


def cmd_logs(_: argparse.Namespace) -> int:
    try:
        compose("logs", "-f", check=False)
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="build and start the stack")
    up.add_argument("--seed", action="store_true", help="import the sample movies.csv after startup")
    up.add_argument("--test", action="store_true", help="run the test suite after startup")
    up.set_defaults(func=cmd_up)

    export = sub.add_parser("export", help="download the database as a gzipped CSV")
    export.add_argument("-o", "--output", default="movies-export.csv.gz", help="output file")
    export.set_defaults(func=cmd_export)

    test = sub.add_parser("test", help="run the test suite; extra arguments go to pytest")
    test.set_defaults(func=cmd_test)

    down = sub.add_parser("down", help="stop the stack")
    down.add_argument("--volumes", action="store_true", help="also delete data volumes")
    down.set_defaults(func=cmd_down)

    logs = sub.add_parser("logs", help="follow service logs")
    logs.set_defaults(func=cmd_logs)

    args, extra = parser.parse_known_args()
    if args.command == "test":
        args.pytest_args = extra
    elif extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    try:
        return args.func(args)
    except subprocess.CalledProcessError as exc:
        print(f"command failed ({exc.returncode}): {' '.join(exc.cmd)}", file=sys.stderr)
        return exc.returncode


if __name__ == "__main__":
    sys.exit(main())
