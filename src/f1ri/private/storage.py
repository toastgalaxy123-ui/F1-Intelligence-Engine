"""Versioned SQLite storage for principals, sessions, workspaces and job receipts."""

import os
import sqlite3
from contextlib import closing, contextmanager
from hashlib import sha256
from json import loads
from pathlib import Path
from secrets import token_urlsafe
from time import time

from f1ri.api.research import ResearchError
from f1ri.api.workspace import WorkspaceFile
from f1ri.decision_board import canonical

SCHEMA = 1
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS principals (
    id INTEGER PRIMARY KEY, issuer TEXT NOT NULL, subject TEXT NOT NULL, label TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), UNIQUE(issuer,subject)
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, owner INTEGER NOT NULL REFERENCES principals(id), expires INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires);
CREATE TABLE IF NOT EXISTS workspaces (
    owner INTEGER NOT NULL REFERENCES principals(id), id TEXT NOT NULL,
    document BLOB NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL,
    PRIMARY KEY(owner,id)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, owner INTEGER NOT NULL REFERENCES principals(id), computation TEXT NOT NULL,
    expected TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('QUEUED','RUNNING','FAILED','CANCELED','COMPLETED')),
    created INTEGER NOT NULL, updated INTEGER NOT NULL, code TEXT, message TEXT,
    measurements BLOB, result BLOB, details BLOB
);
CREATE INDEX IF NOT EXISTS job_lookup ON jobs(owner, computation, expected, state, updated);
CREATE TABLE IF NOT EXISTS usage (
    owner INTEGER NOT NULL REFERENCES principals(id), action TEXT NOT NULL, created INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_lookup ON usage(owner,action,created);
PRAGMA user_version=1;
"""


def workspace_missing():
    return ResearchError("WORKSPACE_NOT_FOUND", "The workspace is unavailable.", 404)


def job_missing():
    return ResearchError("ANALYSIS_NOT_FOUND", "This analysis is unavailable.", 404)


@contextmanager
def lease(database):
    """A single local API process owns the durable worker queue."""
    target = database.with_name(database.name + ".worker")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a+b") as handle:
        if target.stat().st_size == 0:
            handle.write(b"1")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class Store:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self, *, write=False):
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                if write:
                    db.commit()
            except BaseException:
                if write:
                    db.rollback()
                raise

    def migrate(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA:
                raise ValueError("The workspace database requires a newer supported schema.")
            if not version:
                db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_SQL + "\nCOMMIT;")

    def integrity(self):
        with self.connect() as db:
            return (db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                    and not db.execute("PRAGMA foreign_key_check").fetchone()
                    and db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA)

    def principal(self, issuer, sub):
        with self.connect() as db:
            return db.execute("SELECT id,label FROM principals WHERE issuer=? AND subject=? AND enabled=1",
                              (issuer, sub)).fetchone()

    def enroll(self, issuer, sub, label):
        if not 1 <= len(sub) <= 255 or not 1 <= len(label.strip()) <= 50 or not label.isprintable():
            raise ValueError("Provide a valid immutable subject and display label.")
        with self.connect(write=True) as db:
            db.execute("""INSERT INTO principals(issuer,subject,label,enabled) VALUES(?,?,?,1)
                        ON CONFLICT(issuer,subject) DO UPDATE SET label=excluded.label,enabled=1""",
                       (issuer, sub, label.strip()))

    def revoke(self, issuer, sub):
        with self.connect(write=True) as db:
            owner = db.execute("SELECT id FROM principals WHERE issuer=? AND subject=?", (issuer, sub)).fetchone()
            if owner is None:
                raise ValueError("The operator must provide an enrolled OIDC subject.")
            db.execute("UPDATE principals SET enabled=0 WHERE id=?", (owner[0],))
            db.execute("DELETE FROM sessions WHERE owner=?", (owner[0],))
            return owner[0]

    @staticmethod
    def _token(raw):
        return sha256(raw.encode()).hexdigest()

    def new_session(self, owner, ttl, replace=None):
        raw = token_urlsafe(32)
        now = int(time())
        with self.connect(write=True) as db:
            db.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
            if replace and len(replace) == 43:
                db.execute("DELETE FROM sessions WHERE token_hash=?", (self._token(replace),))
            enabled = db.execute("SELECT enabled FROM principals WHERE id=?", (owner,)).fetchone()
            if not enabled or not enabled[0]:
                raise ResearchError("ACCESS_DENIED", "This identity is outside the private invitation list.", 403)
            db.execute("INSERT INTO sessions VALUES(?,?,?)", (self._token(raw), owner, now+ttl))
            # A retained session on another signed-in device stays valid.
            db.execute("""DELETE FROM sessions WHERE owner=? AND token_hash IN
                       (SELECT token_hash FROM sessions WHERE owner=? ORDER BY expires DESC, rowid DESC LIMIT -1 OFFSET 12)""",
                       (owner, owner))
        return raw

    def session(self, raw):
        if not raw or len(raw) != 43 or not raw.isascii():
            return None
        with self.connect() as db:
            return db.execute("""SELECT p.id,p.label,s.expires FROM sessions s
                              JOIN principals p ON p.id=s.owner WHERE s.token_hash=? AND s.expires>? AND p.enabled=1""",
                              (self._token(raw), int(time()))).fetchone()

    def logout(self, raw):
        if raw and len(raw) == 43 and raw.isascii():
            with self.connect(write=True) as db:
                db.execute("DELETE FROM sessions WHERE token_hash=?", (self._token(raw),))

    def limit(self, owner, action, cap):
        now = int(time())
        with self.connect(write=True) as db:
            db.execute("DELETE FROM usage WHERE created <= ?", (now-60,))
            count = db.execute("SELECT count(*) FROM usage WHERE owner=? AND action=?", (owner, action)).fetchone()[0]
            if count >= cap:
                raise ResearchError("RATE_LIMIT", "The private request limit was reached. Try again in a minute.", 429)
            db.execute("INSERT INTO usage VALUES(?,?,?)", (owner, action, now))

    def workspaces(self, owner):
        with self.connect() as db:
            return [loads(row[0]) for row in db.execute(
                "SELECT document FROM workspaces WHERE owner=? ORDER BY updated DESC,id", (owner,))]

    def workspace(self, owner, identity):
        with self.connect() as db:
            row = db.execute("SELECT document FROM workspaces WHERE owner=? AND id=?", (owner, identity)).fetchone()
            if row is None:
                raise workspace_missing()
            return WorkspaceFile.model_validate_json(row[0])

    def save(self, owner, document, cap, *, update=False):
        identity = str(document.id)
        now = int(time())
        with self.connect(write=True) as db:
            row = db.execute("SELECT created FROM workspaces WHERE owner=? AND id=?", (owner, identity)).fetchone()
            if update and row is None:
                raise workspace_missing()
            if not update and row:
                raise ResearchError("WORKSPACE_EXISTS", "This workspace is already saved.", 409)
            if not update and db.execute("SELECT count(*) FROM workspaces WHERE owner=?", (owner,)).fetchone()[0] >= cap:
                raise ResearchError("WORKSPACE_LIMIT", "The private workspace shelf is full.", 409)
            from datetime import UTC, datetime

            created = row[0] if row else now
            value = document.model_copy(update={"created_at": datetime.fromtimestamp(created, UTC),
                                                "updated_at": datetime.fromtimestamp(now, UTC)})
            db.execute("""INSERT INTO workspaces(owner,id,document,created,updated) VALUES(?,?,?,?,?)
                          ON CONFLICT(owner,id) DO UPDATE SET document=excluded.document,updated=excluded.updated""",
                       (owner, identity, canonical(value.model_dump(mode="json")), created, now))
        return value

    def delete(self, owner, identity):
        with self.connect(write=True) as db:
            if not db.execute("DELETE FROM workspaces WHERE owner=? AND id=?", (owner, identity)).rowcount:
                raise workspace_missing()

    def fail_interrupted(self):
        with self.connect(write=True) as db:
            return db.execute("""UPDATE jobs SET state='FAILED',code='SERVICE_RESTART',
                              message='The service restarted before committing a result. Submit the preserved inputs again.',
                              updated=? WHERE state IN ('QUEUED','RUNNING')""", (int(time()),)).rowcount

    def copy(self, destination):
        """Online SQLite backup, always into a newly allocated file."""
        target = Path(destination)
        if target.resolve() == self.path.resolve():
            raise ValueError("Choose a separate backup or restore destination.")
        if not self.path.is_file() or not self.integrity():
            raise ValueError("The source database integrity check failed.")
        with target.open("xb"):
            pass
        target.chmod(0o600)
        try:
            with self.connect() as source, closing(sqlite3.connect(target)) as dest:
                source.backup(dest)
            if not Store(target).integrity():
                raise ValueError("The restored database integrity check failed.")
        except BaseException:
            target.unlink(missing_ok=True)
            raise
