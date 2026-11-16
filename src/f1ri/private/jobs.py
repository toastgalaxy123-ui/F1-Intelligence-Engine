"""Principal-scoped work handles with atomic lifecycle and result retention."""

from hashlib import sha256
from json import loads
from logging import getLogger
from secrets import token_hex
from shutil import rmtree
from time import time

from f1ri.api.contracts import Job, Result
from f1ri.api.jobs import MAX_PENDING, TERMINAL, Entry, Jobs
from f1ri.api.research import ResearchError
from f1ri.api.workspace import AnalysisDetails
from f1ri.decision_board import canonical
from f1ri.private.storage import job_missing

LOGGER = getLogger("f1ri.private")
MAX_DURABLE_BYTES = 48_000_000
MAX_OWNER_HISTORY = 64
MAX_GLOBAL_HISTORY = 512


class OwnedJobs(Jobs):
    def __init__(self, root, store, config, **limits):
        super().__init__(root, queue_seconds=config.queue_seconds, **limits)
        self.store, self.config = store, config
        self.work_root = store.path.with_name(store.path.name + ".scratch")
        self.work_root.mkdir(mode=0o700, exist_ok=True)
        for directory in self.work_root.glob("f1ri-web-*"):
            if directory.is_dir() and not directory.is_symlink():
                rmtree(directory, ignore_errors=True)
        recovered = store.fail_interrupted()
        LOGGER.info("service_ready recovered_jobs=%d", recovered)

    def _lookup(self, owner, identity):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE owner=? AND id=?", (owner, identity)).fetchone()
        if row is None:
            raise job_missing()
        return row

    def get_for(self, owner, identity):
        row = self._lookup(owner, identity)
        if row["state"] == "COMPLETED" and self._expired(row):
            raise ResearchError("RESULT_EXPIRED", "This result expired. Reproduce it from its saved inputs.", 410)
        return Job(id=identity, state=row["state"], code=row["code"], message=row["message"],
                   measurements=loads(row["measurements"]) if row["measurements"] else None,
                   result=Result.model_validate_json(row["result"]) if row["result"] else None)

    def details_for(self, owner, identity):
        row = self._lookup(owner, identity)
        if not row["details"] or self._expired(row):
            raise ResearchError("DETAILS_UNAVAILABLE", "There are no retained checkpoint traces for this run.", 409)
        return AnalysisDetails.model_validate_json(row["details"])

    def _expired(self, row):
        return row["result"] is None or row["updated"] <= int(time()) - self.config.result_days * 86400

    def cancel_for(self, owner, identity):
        with self.lock:
            row = self._lookup(owner, identity)
            if row["state"] not in TERMINAL and identity in self.entries:
                self.cancel(identity)
            return self.get_for(owner, identity)

    def submit_for(self, owner, computation, request, expected=None):
        self.store.limit(owner, "submit", self.config.rate_submissions)
        now = int(time())
        with self.lock, self.store.connect(write=True) as db:
            self._retain(db, owner, now)
            cached = db.execute("""SELECT id,state FROM jobs WHERE owner=? AND computation=? AND expected=?
                 AND (state IN ('QUEUED','RUNNING') OR (state='COMPLETED' AND result IS NOT NULL))
                 ORDER BY updated DESC LIMIT 1""", (owner, computation, expected or "")).fetchone()
            if cached:
                return self.get_for(owner, cached["id"]).model_copy(update={"cache_hit": cached["state"] == "COMPLETED"})

            pending = db.execute("SELECT owner FROM jobs WHERE state IN ('QUEUED','RUNNING')").fetchall()
            if len(pending) >= MAX_PENDING or sum(p[0] == owner for p in pending) >= self.config.owner_pending:
                raise ResearchError("QUEUE_FULL", "The private analysis queue is full. Wait for an active run.", 429)
            identity = token_hex(32)
            db.execute("INSERT INTO jobs(id,owner,computation,expected,state,created,updated) VALUES(?,?,?,?,?,?,?)",
                       (identity, owner, computation, expected or "", "QUEUED", now, now))
            # The worker cannot acquire the lock until the enclosing transaction has committed.
            entry = Entry(Job(id=identity, state="QUEUED"))
            self.entries[identity] = entry
            self._prune()
            self.worker.submit(self._execute, computation, entry, request, expected)
            return entry.job

    def _set(self, entry, state, **details):
        with self.lock:
            current = entry.job.model_copy(update={"state": state, **details})
            result = canonical(current.result.model_dump(mode="json")) if current.result else None
            traces = canonical(entry.details.model_dump(mode="json")) if result and entry.details else None
            now = int(time())
            with self.store.connect(write=True) as db:
                if result:
                    supplied = loads(result)
                    checksum = supplied.pop("result_sha256")
                    if sha256(canonical(supplied)).hexdigest() != checksum:
                        raise ValueError("Worker result failed its canonical receipt check.")
                db.execute("""UPDATE jobs SET state=?,updated=?,code=?,message=?,measurements=?,result=?,details=?
                              WHERE id=?""", (state, now, current.code, current.message,
                         canonical(current.measurements) if current.measurements else None,
                         result, traces, current.id))
                if state in TERMINAL:
                    owner = db.execute("SELECT owner FROM jobs WHERE id=?", (current.id,)).fetchone()[0]
                    self._retain(db, owner, now)
            entry.job = current
            if state in TERMINAL:
                perf = current.measurements or {}
                LOGGER.info("job_terminal state=%s code=%s outstanding=%d wall_ms=%d cpu_ms=%d rss_mib=%d", state,
                            current.code or "OK", sum(e.job.state not in TERMINAL for e in self.entries.values()),
                            int(perf.get("wall_s", 0)*1000), int(perf.get("cpu_s", 0)*1000),
                            int(perf.get("peak_rss_mib", 0)))

    def _retain(self, db, owner, now):
        db.execute("UPDATE jobs SET result=NULL,details=NULL WHERE result IS NOT NULL AND updated<=?",
                   (now-self.config.result_days*86400,))
        for selection, params in (("owner=?", (owner, MAX_OWNER_HISTORY)), ("1=1", (MAX_GLOBAL_HISTORY,))):
            db.execute(f"""DELETE FROM jobs WHERE id IN (SELECT id FROM jobs WHERE {selection}
                         AND state IN ('COMPLETED','CANCELED','FAILED') ORDER BY updated DESC,rowid DESC LIMIT -1 OFFSET ?)""",
                       params)
        total = db.execute("SELECT coalesce(sum(length(result)+coalesce(length(details),0)),0) FROM jobs").fetchone()[0]
        if total > MAX_DURABLE_BYTES:
            for job, size in db.execute("""SELECT id,length(result)+coalesce(length(details),0) FROM jobs
                                           WHERE result IS NOT NULL ORDER BY updated,rowid""").fetchall():
                db.execute("UPDATE jobs SET result=NULL,details=NULL WHERE id=?", (job,))
                total -= size
                if total <= MAX_DURABLE_BYTES:
                    break
