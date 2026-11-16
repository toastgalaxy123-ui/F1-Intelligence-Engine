"""Operator-only controls for the disposable localhost acceptance fixture."""

from pathlib import Path
from re import fullmatch
from sys import argv, stdin

from f1ri.private.config import PrivateConfig
from f1ri.private.storage import Store

if __name__ == "__main__":
    stage, action, identity, *args = argv[1:]
    if action == "expire" and identity == "-":
        identity = stdin.read(65)
    cfg = PrivateConfig.model_validate_json((Path(stage)/"staging.private.json").read_bytes())
    if cfg.environment != "staging" or cfg.database.parent.resolve() != Path(stage).resolve():
        raise SystemExit("This operator fixture can only modify its staging database.")
    store = Store(cfg.database)
    if action == "expire" and fullmatch(r"[a-f0-9]{64}", identity):
        with store.connect(write=True) as db:
            if not db.execute("UPDATE sessions SET expires=1 WHERE token_hash=?", (identity,)).rowcount:
                raise SystemExit("No matching fixture session.")
    elif action == "revoke":
        store.revoke(cfg.oidc_issuer, identity)
    elif action == "enroll":
        store.enroll(cfg.oidc_issuer, identity, args[0])
    else:
        raise SystemExit("Unsupported fixture action.")
