"""One-command runner for the movie service (DESIGN.md §11).

    uv run python run.py up [--seed] [--test]
                                           build and start the stack, wait until healthy;
                                           --seed imports movies.csv and shows progress
    uv run python run.py test [pytest args] run the test suite (e2e tests start their own isolated stack)
    uv run python run.py down [--volumes]   stop the stack; --volumes also wipes the data
    uv run python run.py logs               follow the service logs
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMPOSE = ["docker", "compose", "--project-directory", str(ROOT)]


def compose(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*COMPOSE, *args], cwd=ROOT, check=check, text=True, capture_output=capture
    )


def api_url() -> str:
    out = compose("port", "api", "8000", capture=True).stdout.strip()
    port = out.rsplit(":", 1)[-1]
    return f"http://localhost:{port}"


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


def seed(url: str, csv_path: Path) -> int:
    """Upload a CSV through the API and follow the job's SSE progress stream."""
    import httpx

    headers = {"X-API-Key": env_value("API_KEY", "dev-api-key")}
    print(f"Importing {csv_path.name} ({csv_path.stat().st_size / 1e6:.1f} MB)...")
    with httpx.Client(base_url=url, timeout=httpx.Timeout(30, read=None)) as client:
        with csv_path.open("rb") as f:
            resp = client.post("/api/v1/imports", headers=headers, files={"file": (csv_path.name, f, "text/csv")})
        if resp.status_code != 202:
            print(f"import rejected ({resp.status_code}): {resp.text}", file=sys.stderr)
            return 1
        job_id = resp.json()["id"]
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
    if event != "succeeded":
        print(f"import failed: {job.get('error')}", file=sys.stderr)
        return 1
    result = {k: v for k, v in job["result"].items() if k != "rejected_samples"}
    print("  " + ", ".join(f"{k}={v}" for k, v in result.items()))
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    compose("up", "-d", "--build", "--wait")
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
