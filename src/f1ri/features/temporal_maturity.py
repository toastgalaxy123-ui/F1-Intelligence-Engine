"""Field-specific maturity gates layered over receipt-visible source versions."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from math import isfinite

from f1ri.ingestion.source_receipt import canonical, digest, utc

SECTORS = tuple(f"duration_sector_{n}" for n in (1, 2, 3))
SEGMENTS = tuple(f"segments_sector_{n}" for n in (1, 2, 3))
LAP_FINAL = ("lap_duration", "is_pit_out_lap", "st_speed", *SEGMENTS)
METADATA = ("session_key", "meeting_key", "driver_number")
FIELD_MATRIX = {
    "laps": {"date_start": "APPROXIMATE_LAP_START_ANCHOR", "lap_duration": "COMPLETED_LAP",
             **dict.fromkeys(SECTORS, "SUPPORTED_SECTOR_LINE_OR_FINISH"),
             **dict.fromkeys(SEGMENTS, "COMPLETE_LAP_ARRAY"),
             "i1_speed": "FIRST_INTERMEDIATE_LINE_OR_FINISH", "i2_speed": "SECOND_INTERMEDIATE_LINE_OR_FINISH",
             "is_pit_out_lap": "COMPLETE_LAP_AND_PIT_CONTEXT", "st_speed": "COMPLETE_LAP"},
    "location": dict.fromkeys(("date", "x", "y", "z"), "STAMPED_APPROXIMATE_COORDINATE"),
    "position": dict.fromkeys(("date", "position"), "STAMPED_COMPETITIVE_POSITION"),
    "intervals": dict.fromkeys(("date", "interval", "gap_to_leader"), "STAMPED_REPORTED_INTERVAL"),
    "weather": dict.fromkeys(("date", "air_temperature", "track_temperature", "humidity", "rainfall",
                               "pressure", "wind_speed", "wind_direction"), "STAMPED_WEATHER_SAMPLE"),
    "car_data": dict.fromkeys(("date", "speed", "rpm", "n_gear", "throttle", "brake", "drs"),
                              "STAMPED_TELEMETRY_SAMPLE"),
    "race_control": dict.fromkeys(("date", "category", "flag", "scope", "sector", "lap_number", "message"),
                                   "REPORTED_MESSAGE_NOT_PHYSICAL_EFFECT"),
    "pit": {"date": "REPORTED_PIT_EVENT", "lap_number": "REPORTED_PIT_EVENT",
            "lane_duration": "INDEPENDENT_LANE_COMPLETION_REQUIRED",
            "pit_duration": "INDEPENDENT_LANE_COMPLETION_REQUIRED", "stop_duration": "SERVICE_COMPLETION_REQUIRED"},
    "stints": {"stint_number": "STINT_TRANSITION_CONFIRMATION_REQUIRED",
               "lap_start": "STINT_TRANSITION_CONFIRMATION_REQUIRED",
               "lap_end": "STINT_END_CONFIRMATION_REQUIRED",
               "compound": "STINT_TRANSITION_CONFIRMATION_REQUIRED",
               "tyre_age_at_start": "STINT_TRANSITION_CONFIRMATION_REQUIRED"},
    "sessions": {"year": "IDENTITY_METADATA", "session_name": "IDENTITY_METADATA",
                 "session_type": "IDENTITY_METADATA", "date_start": "SCHEDULE_METADATA",
                 "date_end": "SCHEDULE_METADATA_NOT_RACE_FINISH",
                 "circuit_key": "IDENTITY_METADATA", "circuit_short_name": "IDENTITY_METADATA"},
}
LAP_KEYS = frozenset({"lap_number", "date_start"})


@dataclass(frozen=True)
class Boundary:
    """An observed evidence clock. Availability is independently carried in receipt mode."""

    event_utc: datetime
    source: str
    usable_utc: datetime | None = None


@dataclass(frozen=True)
class FieldValidity:
    field: str
    status: str
    source_event: datetime | None
    measurement_start: datetime | None = None
    completion_evidence: datetime | None = None
    earliest_supported_completion: datetime | None = None
    boundary_source: str | None = None
    first_seen: datetime | None = None
    processed: datetime | None = None
    provider_publication: None = None
    usable: datetime | None = None


def _number(value):
    return value if type(value) in (int, float) and isfinite(value) and value > 0 else None


def _lap_boundary(row, field, boundaries, event, receipt):
    start = utc(row["date_start"])
    if field in SECTORS or field in ("i1_speed", "i2_speed"):
        n = SECTORS.index(field)+1 if field in SECTORS else int(field[1])
        landmark = f"sector_{n}"
    else:
        n, landmark = 0, "finish"
    boundary = boundaries.get(landmark) or boundaries.get("finish")
    if boundary is None or boundary.event_utc > event or boundary.event_utc <= start:
        return None, None, "MATURITY_UNVERIFIED"
    if receipt is not None and (boundary.usable_utc is None or boundary.usable_utc > receipt):
        return None, None, "BOUNDARY_NOT_RECEIPT_VISIBLE"
    if n:
        vals = [_number(row.get(name)) for name in SECTORS[:n]]
        if any(value is None for value in vals):
            return None, None, "COMPLETION_TIME_UNRESOLVED"
        earliest = start+timedelta(seconds=sum(vals))
    else:
        duration = _number(row.get("lap_duration"))
        if duration is None:
            return None, None, "COMPLETION_TIME_UNRESOLVED"
        earliest = start+timedelta(seconds=duration)
    if earliest > event:
        return None, None, "MATURITY_UNVERIFIED"
    if boundary.event_utc+timedelta(seconds=.25) < earliest:
        return None, None, "COMPLETION_EVIDENCE_CONFLICT"
    return boundary, earliest, "FIELD_EVENT_TIME_ELIGIBLE" if receipt is None else "FIELD_RECEIPT_ELIGIBLE"


def visible_fields(endpoint, row, cutoff, *, first_seen=None, processed=None, usable=None, receipt=None, boundaries=None):
    """Return only eligible payload. Unavailable fields never expose derived future clocks."""
    event = utc(cutoff)
    receipt, first_seen, processed, usable = map(utc, (receipt, first_seen, processed, usable))
    usable = usable or processed
    boundaries = boundaries or {}
    if receipt is not None and (not first_seen or not processed or max(first_seen, processed, usable) > receipt):
        return {}, {}
    start_key = "date_start" if endpoint == "laps" else "date"
    begin = utc(row.get(start_key)) if endpoint != "sessions" else None
    if begin is not None and begin > event:
        return {}, {}
    contract = FIELD_MATRIX.get(endpoint, {})
    value = {k: row[k] for k in METADATA if k in row}
    reports = {}
    default_status = "FIELD_RECEIPT_ELIGIBLE" if receipt is not None else "FIELD_EVENT_TIME_ELIGIBLE"
    for field, rule in contract.items():
        if endpoint != "laps" and (field not in row or row[field] is None):
            continue
        mark = earliest = None
        status = default_status
        if endpoint == "laps" and field not in LAP_KEYS:
            if begin is None:
                status = "MATURITY_UNVERIFIED"
            else:
                mark, earliest, status = _lap_boundary(row, field, boundaries, event, receipt)
        elif rule.endswith("_REQUIRED") or rule == "SCHEDULE_METADATA_NOT_RACE_FINISH":
            status = "MATURITY_UNVERIFIED"
        elif begin is None and endpoint != "sessions":
            status = "EVENT_CLOCK_UNAVAILABLE"
        if status == default_status and row.get(field) is None:
            status = "FIELD_NOT_REPORTED"
        if status == default_status:
            value[field] = row[field]
        reports[field] = asdict(FieldValidity(
            field, status, begin, begin, mark.event_utc if mark else None, earliest,
            mark.source if mark else None, first_seen if receipt else None, processed if receipt else None,
            usable=usable if receipt else None,
        ))
    if endpoint == "laps" and begin is not None and "lap_number" in row:
        value["lap_number"] = row["lap_number"]
    return value, reports


def filter_receipt_snapshot(snapshot):
    """Mask partial fields without re-reading or augmenting the replay's receipt prefix."""
    by_lap = {}
    for row in snapshot["observations"]:
        payload = row["payload"]
        if row["endpoint"] != "laps" or not payload.get("date_start"):
            continue
        slot = (payload["session_key"], payload["driver_number"], payload["lap_number"])
        by_lap[slot] = row
    rows = []
    for row in snapshot["observations"]:
        endpoint, payload = row["endpoint"], row["payload"]
        boundary = {}
        if endpoint == "laps":
            next_key = (payload["session_key"], payload["driver_number"], payload["lap_number"]+1)
            if next_row := by_lap.get(next_key):
                boundary["finish"] = Boundary(utc(next_row["payload"]["date_start"]), "RECEIPT_VISIBLE_NEXT_LAP_LINE",
                                              utc(next_row["usable_utc"]))
        clean, evidence = visible_fields(endpoint, payload, snapshot["event_cutoff"],
                                         first_seen=row["first_seen_utc"], processed=row["processed_utc"],
                                         usable=row["usable_utc"],
                                         receipt=snapshot["receipt_cutoff"], boundaries=boundary)
        if clean:
            # The sanitized payload gets its own identity; keep the actually
            # received version and original processing clock in the lineage.
            rows.append(row | {"payload": clean, "maturity": evidence, "visible_payload_sha256": digest(canonical(clean))})
    return snapshot | {"observations": rows}
