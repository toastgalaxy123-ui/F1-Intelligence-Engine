"""Build, validate or run the opt-in single-host private application stack."""

import os
import signal
import sys
from argparse import ArgumentParser
from pathlib import Path
from re import fullmatch
from subprocess import Popen, TimeoutExpired, run
from time import sleep

from f1ri.private.config import PrivateConfig

ROOT = Path(__file__).resolve().parents[1]


def frontend_env(dist):
    return os.environ | {"NEXT_PUBLIC_F1RI_PRIVATE": "true", "NEXT_PUBLIC_F1RI_API_URL": "/api/v1",
                         "NEXT_TELEMETRY_DISABLED": "1", "F1RI_PRIVATE_DIST": dist}


def main():
    args = ArgumentParser(description=__doc__)
    args.add_argument("command", choices=["build", "validate", "serve"])
    args.add_argument("--config", type=Path)
    args.add_argument("--caddy", default="caddy")
    args.add_argument("--cert", type=Path)
    args.add_argument("--key", type=Path)
    args.add_argument("--bind-interface", default="127.0.0.1")
    args.add_argument("--build-slot", default="")
    args.add_argument("--staging-pid-file", type=Path)
    opts = args.parse_args()
    if opts.build_slot and not fullmatch(r"[a-z0-9-]{1,24}", opts.build_slot):
        args.error("Build slots use up to 24 lowercase letters, numbers and dashes.")
    dist = ".next-private" + (f"-{opts.build_slot}" if opts.build_slot else "")
    web = ROOT/"apps/web"
    next_cli = web/"node_modules/next/dist/bin/next"
    if opts.command == "build":
        # Build only with public flags, never load the secret configuration.
        build_env = {k: v for k, v in frontend_env(dist).items() if k not in ("F1RI_PRIVATE_CONFIG", "F1RI_EDGE_TOKEN")}
        return run(["node", str(next_cli), "build"], cwd=web, env=build_env, check=True).returncode
    if not opts.config or not opts.cert or not opts.key or not opts.cert.is_file() or not opts.key.is_file():
        args.error("Specify a private config and readable TLS certificate and key.")
    cfg = PrivateConfig.model_validate_json(opts.config.read_bytes())
    if cfg.environment == "staging" and opts.bind_interface != "127.0.0.1":
        args.error("The staging gateway can only bind to loopback.")
    if opts.staging_pid_file and (cfg.environment != "staging" or
                                  opts.staging_pid_file.resolve().parent != cfg.database.parent.resolve()):
        args.error("A rehearsal PID file must be in the disposable staging database directory.")
    if opts.bind_interface not in ("127.0.0.1", "0.0.0.0"):
        args.error("Use loopback or explicitly choose IPv4 ingress.")
    env = frontend_env(dist) | {"F1RI_EDGE_TOKEN": cfg.edge_secret.get_secret_value(),
                            "F1RI_GATEWAY_ORIGIN": cfg.origin, "F1RI_GATEWAY_BIND": opts.bind_interface,
                            "F1RI_GATEWAY_CERT": opts.cert.resolve().as_posix(),
                            "F1RI_GATEWAY_KEY": opts.key.resolve().as_posix(),
                            "F1RI_GATEWAY_STATE": (cfg.database.parent/"gateway-state").as_posix(),
                            "F1RI_PRIVATE_CONFIG": str(opts.config.resolve()), "F1RI_REPO_ROOT": str(ROOT),
                            "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
    edge = [opts.caddy, "--config", str(ROOT/"deploy/private.Caddyfile"), "--adapter", "caddyfile"]
    run([edge[0], "validate", *edge[1:]], cwd=ROOT, env=env, check=True)
    if opts.command == "validate":
        return 0
    if not (web/dist/"BUILD_ID").is_file():
        args.error("Build the production private frontend first.")

    logging = ROOT/"deploy/private-logging.json"
    processes = []
    try:
        processes.append(Popen([sys.executable, "-m", "uvicorn", "f1ri.api.app:app", "--host", "127.0.0.1", "--port",
                                "8100", "--workers", "1", "--no-proxy-headers", "--no-access-log",
                                "--log-config", str(logging), "--log-level", cfg.log_level.lower()], cwd=ROOT, env=env))
        frontend = {k: v for k, v in env.items() if k != "F1RI_PRIVATE_CONFIG"}
        processes.append(Popen(["node", str(next_cli), "start", "--hostname", "127.0.0.1", "--port", "3100"],
                               cwd=web, env=frontend))
        processes.append(Popen([edge[0], "run", *edge[1:]], cwd=ROOT, env=frontend))
        if opts.staging_pid_file:
            opts.staging_pid_file.write_text(str(processes[0].pid), encoding="ascii")
        while all(proc.poll() is None for proc in processes):
            sleep(.25)
        return next(proc.returncode for proc in processes if proc.returncode is not None)
    except KeyboardInterrupt:
        return 0
    finally:
        for child in reversed(processes):
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
        for child in processes:
            try:
                child.wait(timeout=6)
            except TimeoutExpired:
                child.kill()
                child.wait()
        if opts.staging_pid_file:
            opts.staging_pid_file.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
