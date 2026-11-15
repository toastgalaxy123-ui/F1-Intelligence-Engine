"""Canonical evidence contracts for one local response and its source records."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from json import dumps, loads

EVENT_ONLY = "EVENT_TIME_ONLY"
OBSERVED = "FIRST_SEEN_OBSERVED"
EXPERIMENT = "SYNTHETIC_DELIVERY_EXPERIMENT"
RETROSPECTIVE = "RETROSPECTIVE_RETRIEVAL"
CAPTURE_SCHEMA = "s7-b-source-receipt-v1"
CLOCK_UNKNOWN = "SYNCHRONIZATION_UNKNOWN"
CLOCK_ANOMALY = "CLOCK_INCONSISTENT"
CLOCK_FIXTURE = "SYNTHETIC_SHARED_UTC"

# Schedule timestamps on sessions are not observed event times. Stints have no
# trustworthy event clock. Neither is consumed as physical state.
SCHEMAS = {
    "sessions": (None, ()),
    "stints": (None, ("driver_number", "stint_number")),
    "laps": ("date_start", ("driver_number", "lap_number")),
    "race_control": ("date", ("date", "category", "scope", "sector", "driver_number", "lap_number", "message")),
    "weather": ("date", ("date",)),
    "pit": ("date", ("driver_number", "lap_number", "date")),
    "location": ("date", ("driver_number", "date")),
    "position": ("date", ("driver_number", "date")),
    "intervals": ("date", ("driver_number", "date")),
    "car_data": ("date", ("driver_number", "date")),
}

DRIVER_FEEDS = ("location", "position", "intervals", "laps", "pit", "stints", "car_data")


def canonical(value):
    return dumps(value, default=str, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data):
    return sha256(data).hexdigest()


def utc(value):
    if value is None:
        return None
    clock = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(clock, datetime) or clock.utcoffset() is None:
        raise ValueError("Source and decision clocks require explicit UTC offsets.")
    return clock.astimezone(UTC)


def clock_quality(start, end, start_ns, end_ns):
    elapsed = (end_ns-start_ns)/1e9
    difference = (end-start).total_seconds()
    if elapsed < 0 or difference < 0 or abs(elapsed-difference) > .25:
        return CLOCK_ANOMALY
    return CLOCK_UNKNOWN


def identity(session, endpoint, row):
    """Constrain inference to documented stable endpoint keys."""
    if row.get("session_key") != session or type(row.get("session_key")) is not int:
        raise ValueError("Record session does not match the requested session.")
    clock, keys = SCHEMAS[endpoint]
    source_event = utc(row.get(clock)) if clock else None
    if endpoint in DRIVER_FEEDS and (
        type(row.get("driver_number")) is not int or row["driver_number"] <= 0
    ):
        raise ValueError("Driver identity is unavailable.")
    optional = {"scope", "driver_number", "lap_number", "sector"} if endpoint == "race_control" else set()
    parts = []
    for key in keys:
        val = source_event.isoformat() if key == clock and source_event else row.get(key)
        if val is None and key not in optional or not isinstance(val, (str, int, type(None))):
            raise ValueError(f"Ambiguous observation identity field {key}.")
        parts.append(val)
    return canonical([session, endpoint, *parts]).decode(), source_event


def parse_payload(raw):
    value = loads(raw, parse_constant=_reject_nonfinite)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("OpenF1 receipt body must be an array of objects.")
    return value


def _reject_nonfinite(value):
    raise ValueError(f"Unsupported JSON constant {value}.")


@dataclass(frozen=True)
class SourceReceiptEnvelope:
    provider: str
    endpoint: str
    session: int
    parameters: dict
    capture_id: str
    sequence: int
    poll: int
    retry: int
    request_id: str
    clock_epoch: str
    request_utc: datetime
    request_ns: int
    first_byte_utc: datetime | None
    completed_utc: datetime
    completed_ns: int
    processed_utc: datetime
    http_status: int | None
    headers: dict
    body_sha256: str
    body_bytes: int
    record_count: int | None
    earliest_event_utc: datetime | None
    latest_event_utc: datetime | None
    clock_quality: str
    support: str
    error: str | None
    coverage: str
    mode: str
    provider_publication_utc: None = None

    @property
    def duration_s(self):
        return (self.completed_ns-self.request_ns)/1e9

    @property
    def available_utc(self):
        return max(self.completed_utc, self.processed_utc, self.request_utc)

    def document(self):
        return asdict(self)

    @classmethod
    def from_document(cls, row):
        clocks = ("request_utc", "first_byte_utc", "completed_utc", "processed_utc",
                  "earliest_event_utc", "latest_event_utc")
        data = row | {key: utc(row[key]) for key in clocks}
        result = cls(**data)
        if result.provider_publication_utc is not None:
            raise ValueError("Provider publication clocks require independent evidence.")
        return result
