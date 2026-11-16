"""Operate the authenticated research database without exposing operator routes."""

from argparse import ArgumentParser
from json import dumps
from pathlib import Path

from f1ri.api.contracts import MODEL_SHA
from f1ri.private.config import PrivateConfig
from f1ri.private.storage import Store


def main():
    args = ArgumentParser(description=__doc__)
    args.add_argument("--config", type=Path, required=True)
    commands = args.add_subparsers(dest="operation", required=True)
    commands.add_parser("migrate")
    commands.add_parser("integrity")
    commands.add_parser("status")
    enroll = commands.add_parser("enroll")
    enroll.add_argument("--subject", required=True)
    enroll.add_argument("--label", required=True)
    revoke = commands.add_parser("revoke")
    revoke.add_argument("--subject", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--destination", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--source", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    opts = args.parse_args()
    cfg = PrivateConfig.model_validate_json(opts.config.read_bytes())
    store = Store(cfg.database)
    if opts.operation == "status":
        with store.connect() as db:
            counts = dict(db.execute("SELECT state,count(*) FROM jobs GROUP BY state").fetchall())
            timeout_count = db.execute("SELECT count(*) FROM jobs WHERE code IN ('TIME_LIMIT','QUEUE_TIMEOUT')").fetchone()[0]
            schema = db.execute("PRAGMA user_version").fetchone()[0]
        size = sum(path.stat().st_size for path in cfg.database.parent.glob(cfg.database.name+"*") if path.is_file())
        print(dumps({"schema": schema, "storage_bytes": size, "jobs_by_state": counts,
                     "retained_timeout_count": timeout_count, "model_sha256": MODEL_SHA}))
        return
    if opts.operation == "restore":
        Store(opts.source).copy(opts.destination)
        # A restored environment never reactivates previously issued cookies.
        with Store(opts.destination).connect(write=True) as db:
            db.execute("DELETE FROM sessions")
    elif opts.operation == "migrate":
        store.migrate()
    elif opts.operation == "enroll":
        store.enroll(cfg.oidc_issuer, opts.subject, opts.label)
    elif opts.operation == "revoke":
        store.revoke(cfg.oidc_issuer, opts.subject)
    elif opts.operation == "backup":
        store.copy(opts.destination)
    elif not store.integrity():
        raise SystemExit("Database integrity validation failed.")
    print(f"{opts.operation}: OK")


if __name__ == "__main__":
    main()
