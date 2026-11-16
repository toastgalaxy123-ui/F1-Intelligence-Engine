"""Run the two loopback-only S8-A services in one terminal."""

from argparse import ArgumentParser
from os import environ
from pathlib import Path
from subprocess import Popen, TimeoutExpired
from sys import executable
from time import sleep


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--dev", action="store_true", help="Use Next.js development mode")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    web = root / "apps/web"
    next_cli = web / "node_modules/next/dist/bin/next"
    if not next_cli.is_file() or not args.dev and not (web / ".next/BUILD_ID").is_file():
        parser.error("Install the locked frontend dependencies and build before using production mode.")
    commands = (
        ([executable, "-m", "uvicorn", "f1ri.api.app:app", "--host", "127.0.0.1",
          "--port", "8000", "--workers", "1"], root),
        (["node", str(next_cli), "dev" if args.dev else "start", "--hostname", "127.0.0.1",
          "--port", "3000"], web),
    )
    active = []
    try:
        for command, directory in commands:
            active.append(Popen(command, cwd=directory, env=environ | {"NEXT_TELEMETRY_DISABLED": "1"}))
        while all(process.poll() is None for process in active):
            sleep(.25)
        return next(process.returncode for process in active if process.returncode is not None)
    except KeyboardInterrupt:
        return 0
    finally:
        for process in active:
            if process.poll() is None:
                process.terminate()
        for process in active:
            try:
                process.wait(timeout=5)
            except TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
