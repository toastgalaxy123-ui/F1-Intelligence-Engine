"""Bounded OpenF1 collection with an append-only, recoverable receipt journal."""

import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from json import loads
from pathlib import Path
from uuid import uuid4

from f1ri.ingestion.openf1 import OpenF1Client
from f1ri.ingestion.source_receipt import (
    CAPTURE_SCHEMA,
    OBSERVED,
    RETROSPECTIVE,
    SCHEMAS,
    SourceReceiptEnvelope,
    canonical,
    clock_quality,
    digest,
    identity,
    parse_payload,
    utc,
)

BASE_URL = "https://api.openf1.org/v1"
MAX_BODY_BYTES = 4 * 1024 * 1024


def _write_new(path, raw):
    """Publish a complete immutable file; an abandoned temporary file is inert."""
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temp.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if os.name == "nt":
            os.rename(temp, path)
        else:
            os.link(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@dataclass(frozen=True)
class CapturePlan:
    session: int
    endpoints: tuple[str, ...]
    duration_s: int
    interval_s: float
    request_budget: int
    max_retries: int
    mode: str = RETROSPECTIVE
    window_start: str | None = None
    window_end: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "endpoints", tuple(self.endpoints))
        if (type(self.session) is not int or self.session <= 0 or not self.endpoints or
                not set(self.endpoints) <= SCHEMAS.keys() or len(self.endpoints) != len(set(self.endpoints)) or
                not 1 <= self.duration_s <= 300 or not 2.1 <= self.interval_s <= 60 or
                not 1 <= self.request_budget <= 100 or not 0 <= self.max_retries <= 3 or
                self.mode not in (RETROSPECTIVE, OBSERVED)):
            raise ValueError("Use a bounded session, approved endpoints, at least 2.1 seconds between polls, "
                             "and up to 100 attempts, 3 retries and 300 seconds.")
        start, end = utc(self.window_start), utc(self.window_end)
        if (start is None) != (end is None) or start is not None and not 0 < (end-start).total_seconds() <= 120:
            raise ValueError("Provide a paired UTC source window of at most 120 seconds.")
        if set(self.endpoints) & {"location", "car_data", "intervals"} and start is None:
            raise ValueError("High-volume endpoints require a bounded UTC source window.")

    def params(self, endpoint):
        params = {"session_key": self.session}
        if self.window_start and endpoint in ("location", "car_data", "intervals"):
            params.update({"date>": self.window_start, "date<": self.window_end})
        return params


def _names(sequence):
    return f"{sequence:06d}.intent.json", f"{sequence:06d}.receipt.json"


def _manifest(directory, plan, resume):
    path = directory/"manifest.json"
    if resume:
        manifest = loads(path.read_bytes())
        if manifest["schema"] != CAPTURE_SCHEMA or manifest["plan"] != loads(canonical(plan.__dict__)):
            raise ValueError("Resume must use the original immutable capture plan.")
        return manifest
    directory.mkdir(parents=True, exist_ok=False)
    (directory/"raw").mkdir()
    (directory/"journal").mkdir()
    manifest = {"schema": CAPTURE_SCHEMA, "capture_id": uuid4().hex, "plan": plan.__dict__,
                "started_utc": datetime.now(UTC), "coverage": "UNVERIFIED",
                "provider_publication_utc": None}
    _write_new(path, canonical(manifest))
    return manifest


def _receipt(meta, raw, tracking, mode):
    error = meta.get("error")
    status = meta["http_status"]
    count = None
    times = []
    if not error and status == 200:
        try:
            if meta["headers"].get("content-encoding", "identity").lower() != "identity":
                raise ValueError("Response encoding cannot be safely parsed.")
            rows = parse_payload(raw)
            times = [event for r in rows if (event := identity(tracking["session"], tracking["endpoint"], r)[1])]
            count = len(rows)
        except (ValueError, UnicodeDecodeError):
            error = "SCHEMA_OR_ENCODING_REJECTED"
    elif not error:
        error = f"HTTP_{status}"
    quality = clock_quality(meta["request_utc"], meta["completed_utc"], meta["request_ns"], meta["completed_ns"])
    return SourceReceiptEnvelope(**(meta | tracking | {
        "error": error, "body_bytes": len(raw), "body_sha256": digest(raw),
        "record_count": count, "earliest_event_utc": min(times, default=None),
        "latest_event_utc": max(times, default=None), "processed_utc": datetime.now(UTC),
        "clock_quality": quality, "support": "BODY_RECORDED" if not error else "SOURCE_UNAVAILABLE",
        "coverage": "UNVERIFIED", "mode": mode,
    }))


def retry_wait(envelope):
    """Honor Retry-After and avoid replaying permanent failures."""
    if envelope.error in ("INTERRUPTED", "CAPTURE_DEADLINE", "BODY_LIMIT", "SCHEMA_OR_ENCODING_REJECTED"):
        return None
    status = envelope.http_status
    if status == 429:
        raw = envelope.headers.get("retry-after")
        if raw:
            try:
                return max(5., float(raw))
            except ValueError:
                try:
                    return max(5., (utc(parsedate_to_datetime(raw))-envelope.completed_utc).total_seconds())
                except (ValueError, TypeError):
                    pass
        return 60.
    if status is not None and 500 <= status <= 599 or envelope.error in (
        "ConnectError", "ReadTimeout", "RemoteProtocolError", "ConnectTimeout"
    ):
        return float(min(2**envelope.retry, 16))
    return None


def capture(directory: Path, plan: CapturePlan, *, resume=False, client=None):
    """Use one capture directory and enforce an attempt budget across restarts."""
    manifest = _manifest(directory, plan, resume)
    journal = directory/"journal"
    # Exclusive ownership prevents two collectors from allocating one sequence.
    # An orphaned lock can be inspected and cleared by an explicit recovery call.
    lock = directory/"writer.lock"
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    try:
        return _collect(directory, journal, manifest, plan, client)
    finally:
        lock.unlink(missing_ok=True)


def _collect(directory, journal, manifest, plan, client):
    from contextlib import nullcontext

    intents = sorted(journal.glob("*.intent.json"))
    prior = [loads(p.read_bytes()) for p in intents]
    sequence = len(prior)
    if [r["sequence"] for r in prior] != list(range(1, sequence+1)):
        raise ValueError("Capture attempt journal has a gap or reused index.")
    poll = max((r["poll"] for r in prior), default=0)
    elapsed_wall = (datetime.now(UTC)-utc(manifest["started_utc"])).total_seconds()
    if elapsed_wall < 0:
        raise ValueError("Local clock moved behind the original collection start.")
    deadline = time.monotonic()+max(0., plan.duration_s-elapsed_wall)
    epoch = uuid4().hex
    result = None
    stop = "REQUEST_BUDGET"
    transport = nullcontext(client) if client is not None else OpenF1Client(BASE_URL, plan.interval_s)
    try:
        with transport as source:
            # A resumed process has a distinct monotonic epoch. Keep the
            # throttle across restarts without comparing the old counters.
            if prior:
                time.sleep(min(plan.interval_s, max(0., deadline-time.monotonic())))
            while sequence < plan.request_budget and time.monotonic() < deadline:
                endpoint = plan.endpoints[poll % len(plan.endpoints)]
                poll += 1
                for attempt in range(plan.max_retries+1):
                    if sequence >= plan.request_budget:
                        break
                    seq = sequence+1
                    intent, commit = _names(seq)
                    tracking = {"provider": "openf1", "endpoint": endpoint, "session": plan.session,
                                "parameters": plan.params(endpoint), "capture_id": manifest["capture_id"],
                                "sequence": seq, "poll": poll, "retry": attempt,
                                "request_id": f"{manifest['capture_id']}:{seq}", "clock_epoch": epoch}

                    def started(clock, mono, path=journal/intent, row=tracking):
                        _write_new(path, canonical(row | {"request_utc": clock, "request_ns": mono}))

                    payload = source.capture_attempt(endpoint, tracking["parameters"], deadline=deadline,
                                                     max_bytes=MAX_BODY_BYTES, started=started)
                    if payload is None:
                        stop = "CAPTURE_DURATION"
                        break
                    sequence = seq
                    meta, raw = payload
                    result = _receipt(meta, raw, tracking, plan.mode)
                    body = directory/"raw"/f"{result.body_sha256}.bin"
                    if not body.exists():
                        _write_new(body, raw)
                    _write_new(journal/commit, canonical(result.document()))
                    if result.error == "INTERRUPTED":
                        stop = "INTERRUPTED"
                        break
                    wait = retry_wait(result)
                    if wait is None or attempt >= plan.max_retries or sequence >= plan.request_budget:
                        break
                    if time.monotonic()+wait >= deadline:
                        stop = "RETRY_AFTER_BEYOND_BUDGET"
                        break
                    time.sleep(wait)
                if stop != "REQUEST_BUDGET":
                    break
            if time.monotonic() >= deadline and stop == "REQUEST_BUDGET":
                stop = "CAPTURE_DURATION"
    except KeyboardInterrupt:
        stop = "INTERRUPTED"
    finally:
        _write_new(journal/f"end-{epoch}.json", canonical({"epoch": epoch, "stop": stop, "finished": datetime.now(UTC)}))
    return {"capture_id": manifest["capture_id"], "attempts": len(list(journal.glob("*.intent.json"))),
            "stop": stop, "last_status": result.http_status if result else None}


def recover_lock(directory):
    """Clear only a verifiably abandoned local writer lock."""
    lock = directory/"writer.lock"
    if not lock.is_file():
        return
    pid = int(lock.read_text())
    if os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong)
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            kernel.CloseHandle(handle)
            raise ValueError("The capture writer is still running.")
        if ctypes.get_last_error() != 87:
            raise ValueError("The capture writer cannot be ruled out.")
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise ValueError("The capture writer is still running.")
    lock.unlink()


def load_capture_files(files):
    """Verify a directory or archive represented by safe relative member names."""
    manifest = loads(files["manifest.json"])
    if manifest["schema"] != CAPTURE_SCHEMA:
        raise ValueError("Unknown receipt journal format.")
    plan = CapturePlan(**manifest["plan"])
    envelopes, orphaned = [], []
    intents = sorted(name for name in files if name.startswith("journal/") and name.endswith(".intent.json"))
    commits = {name for name in files if name.startswith("journal/") and name.endswith(".receipt.json")}
    for order, name in enumerate(intents, 1):
        pending = loads(files[name])
        expected_intent, expected_commit = _names(order)
        commit = f"journal/{expected_commit}"
        if (name != f"journal/{expected_intent}" or pending["sequence"] != order or
                pending["session"] != plan.session or pending["endpoint"] not in plan.endpoints or
                pending["parameters"] != plan.params(pending["endpoint"]) or
                pending["request_id"] != f"{manifest['capture_id']}:{order}" or
                pending["capture_id"] != manifest["capture_id"]):
            raise ValueError("Invalid journal request identity.")
        if commit not in files:
            orphaned.append(pending)
            continue
        commits.remove(commit)
        receipt = SourceReceiptEnvelope.from_document(loads(files[commit]))
        if any(receipt.document()[key] != (utc(value) if key == "request_utc" else value)
               for key, value in pending.items()) or receipt.mode != plan.mode:
            raise ValueError("Response does not match its request.")
        raw = files[f"raw/{receipt.body_sha256}.bin"]
        if len(raw) != receipt.body_bytes or digest(raw) != receipt.body_sha256:
            raise ValueError("Raw receipt checksum differs.")
        if not receipt.error and len(parse_payload(raw)) != receipt.record_count:
            raise ValueError("Receipt parsed record count differs.")
        envelopes.append((receipt, raw))
    if commits or len(intents) > plan.request_budget:
        raise ValueError("Unmatched receipts or exceeded capture budget.")
    return manifest, envelopes, orphaned


def capture_files(directory):
    return {p.relative_to(directory).as_posix(): p.read_bytes() for p in sorted(directory.rglob("*"))
            if p.is_file() and not p.name.startswith(".") and p.name != "writer.lock"}
