"""One-command runner for the movie service (DESIGN.md §11).

    uv run python run.py up [--test]       build and start the stack, wait until healthy
    uv run python run.py test [pytest args] run the test suite (e2e tests start their own isolated stack)
    uv run python run.py down [--volumes]   stop the stack; --volumes also wipes the data
    uv run python run.py logs               follow the service logs
"""

import argparse
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


def cmd_up(args: argparse.Namespace) -> int:
    compose("up", "-d", "--build", "--wait")
    url = api_url()
    print(f"\nMovie API is up: {url}  (docs: {url}/docs)")
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
