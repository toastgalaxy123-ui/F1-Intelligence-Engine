"""Serialized local jobs with bounded queue, wall time, RSS and cache sizes."""

import os
import subprocess
import sys
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from hashlib import sha256
from json import loads
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, RLock
from time import monotonic

import psutil

from f1ri.api.contracts import Job, Result
from f1ri.api.research import ResearchError
from f1ri.api.worker import MAX_RESULT_BYTES
from f1ri.api.workspace import AnalysisDetails

WALL_SECONDS = 35
RSS_BYTES = 720 * 1024**2
MAX_PENDING = 4
MAX_RETAINED = 32
MAX_CACHE_BYTES = 24_000_000
TERMINAL = {"COMPLETED", "FAILED", "CANCELED", "UNSUPPORTED"}


@dataclass
class Entry:
    job: Job
    stop: Event = field(default_factory=Event)
    size: int = 0
    details: AnalysisDetails | None = None


class Jobs:
    def __init__(self, root, *, wall_seconds=WALL_SECONDS, rss_bytes=RSS_BYTES):
        self.root = root
        self.wall_seconds = wall_seconds
        self.rss_bytes = rss_bytes
        self.entries = OrderedDict()
        self.lock = RLock()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="f1ri-research")

    def submit(self, analysis_id, request, expected=None):
        identity = sha256(f"replay:{analysis_id}:{expected}".encode()).hexdigest() if expected else analysis_id
        with self.lock:
            entry = self.entries.get(identity)
            if entry and entry.job.state in {"QUEUED", "RUNNING", "COMPLETED"}:
                self.entries.move_to_end(identity)
                result = entry.job.result
                if expected and result and result.result_sha256 != expected:
                    raise ResearchError("REPLAY_CHECKSUM_MISMATCH", "The verified result checksum differs.", 409)
                return entry.job.model_copy(update={"cache_hit": result is not None})
            if sum(row.job.state not in TERMINAL for row in self.entries.values()) >= MAX_PENDING:
                raise ResearchError("QUEUE_FULL", "The local research queue is full. Try again after a run finishes.", 429)
            entry = Entry(Job(id=identity, state="QUEUED"))
            self.entries[identity] = entry
            self.entries.move_to_end(identity)
            self.worker.submit(self._execute, analysis_id, entry, request, expected)
            self._prune()
            return entry.job

    def get(self, identity):
        with self.lock:
            if identity not in self.entries:
                raise ResearchError("ANALYSIS_NOT_FOUND", "This analysis is outside the local result cache.", 404)
            return self.entries[identity].job

    def cancel(self, identity):
        with self.lock:
            job = self.get(identity)
            if job.state not in TERMINAL:
                self.entries[identity].stop.set()
                if job.state == "QUEUED":
                    self._set(self.entries[identity], "CANCELED", code="CANCELED", message="Analysis canceled.")
            return self.entries[identity].job

    def details(self, identity):
        with self.lock:
            self.get(identity)
            result = self.entries[identity].details
            if result is None:
                raise ResearchError("DETAILS_UNAVAILABLE", "Checkpoint traces require a completed physical comparison.", 409)
            return result

    def close(self):
        with self.lock:
            for entry in self.entries.values():
                entry.stop.set()
        self.worker.shutdown(wait=True, cancel_futures=True)

    def _set(self, entry, state, **details):
        with self.lock:
            entry.job = entry.job.model_copy(update={"state": state, **details})

    def _prune(self):
        while len(self.entries) > MAX_RETAINED or sum(row.size for row in self.entries.values()) > MAX_CACHE_BYTES:
            expired = next((key for key, row in self.entries.items() if row.job.state in TERMINAL), None)
            if expired is None:
                return
            del self.entries[expired]

    def _execute(self, identity, entry, request, expected):
        with self.lock:
            if entry.stop.is_set():
                return
            self._set(entry, "RUNNING")
        start = monotonic()
        peak = cpu = 0
        try:
            with TemporaryDirectory(prefix="f1ri-web-") as work:
                work = Path(work)
                (work / "request.json").write_text(request.model_dump_json(), encoding="utf-8")
                env = os.environ | {"PYTHONHASHSEED": "0", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
                                    "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1"}
                code = None
                with subprocess.Popen([sys.executable, "-m", "f1ri.api.worker", str(self.root), str(work)],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as child:
                    process = psutil.Process(child.pid)
                    try:
                        while child.poll() is None:
                            try:
                                peak = max(peak, process.memory_info().rss)
                                times = process.cpu_times()
                                cpu = max(cpu, times.user+times.system)
                            except psutil.NoSuchProcess:
                                break
                            if entry.stop.is_set():
                                code = "CANCELED"
                            elif peak > self.rss_bytes:
                                code = "MEMORY_LIMIT"
                            elif monotonic()-start > self.wall_seconds:
                                code = "TIME_LIMIT"
                            if code:
                                break
                            entry.stop.wait(.025)
                    finally:
                        if child.poll() is None:
                            child.kill()
                        child.wait()
                measurements = {"wall_s": monotonic()-start, "cpu_s": cpu, "peak_rss_mib": peak/1024**2}
                if code or entry.stop.is_set():
                    code = code or "CANCELED"
                    return self._set(entry, "CANCELED" if code == "CANCELED" else "FAILED", code=code,
                                     message="The run stopped before a complete result was available.",
                                     measurements=measurements)
                path = work / "output.json"
                if child.returncode or not path.is_file() or path.stat().st_size > MAX_RESULT_BYTES:
                    raise ValueError("Invalid worker response")
                value = loads(path.read_bytes())
                measurements["cpu_s"] = max(cpu, value.get("cpu_s", 0.0))
                if "result" not in value:
                    return self._set(entry, "FAILED", code=value["code"], message=value["message"],
                                     measurements=measurements)
                result = Result.model_validate(value["result"])
                if result.analysis_id != identity:
                    raise ValueError("Unexpected worker identity")
                if expected and expected != result.result_sha256:
                    return self._set(entry, "FAILED", code="REPLAY_CHECKSUM_MISMATCH",
                                     message="The recomputed result checksum differs.", measurements=measurements)
                with self.lock:
                    if entry.stop.is_set():
                        return self._set(entry, "CANCELED", code="CANCELED", message="Analysis canceled.",
                                         measurements=measurements)
                    entry.size = path.stat().st_size
                    entry.details = AnalysisDetails.model_validate(value["details"]) if "details" in value else None
                    self._set(entry, "COMPLETED", result=result, measurements=measurements)
                    self._prune()
        except Exception:  # noqa: BLE001 -- Worker failures have a stable public error envelope.
            self._set(entry, "FAILED", code="WORKER_UNAVAILABLE",
                      message="The local research worker could not deliver a result.")
