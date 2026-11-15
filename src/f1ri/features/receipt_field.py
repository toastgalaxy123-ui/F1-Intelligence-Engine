"""Feed receipt-visible observations into the existing strict historical geometry."""

from collections import defaultdict
from dataclasses import replace
from json import loads

from f1ri.features.historical_field import (
    FieldMode,
    Series,
    TimedValue,
    indexed,
    reconcile_fixes,
    reconstruct_field,
)
from f1ri.features.track_geometry import LocationFix
from f1ri.ingestion.source_receipt import EXPERIMENT, utc


def reconstruct_received_field(session, replay_bytes):
    """Receipt and physical support remain independent even on a passing fixture."""
    replay = loads(replay_bytes)
    if replay["session"] != session.key:
        raise ValueError("Receipt and geometry sessions differ.")
    feeds = replay["endpoints"]
    required = ("location", "pit", "race_control")
    missing = [ep for ep in required if not feeds.get(ep, {}).get("field_input_allowed")]
    base = {"mode": "RECEIPT_TIME_REPLAY", "source_mode": replay["mode"],
            "receipt_cutoff": replay["receipt_cutoff"], "event_cutoff": replay["event_cutoff"],
            "prefix_sha256": replay["prefix_sha256"], "historical_strategy": "UNSUPPORTED",
            "receipt_qualified_operational": False, "field": None}
    if replay["mode"] != EXPERIMENT:
        return base | {"support": "NO_COMPARABLE_RACING_RECEIPT_CLOCK", "withheld_feeds": missing}
    if missing:
        return base | {"support": "REQUIRED_SOURCE_UNAVAILABLE", "withheld_feeds": missing}

    points, laps, classifications, pit = (defaultdict(list) for _ in range(4))
    control = []
    commands = {"SAFETY CAR DEPLOYED": "SC_REPORTED", "TRACK CLEAR": "TRACK_CLEAR_REPORTED",
                "VIRTUAL SAFETY CAR DEPLOYED": "VSC_REPORTED"}
    for row in replay["observations"]:
        ep, raw = row["endpoint"], row["payload"]
        event = utc(row["source_event_utc"])
        if event is None:
            continue
        driver = raw.get("driver_number")
        source = f"{row['request_id']}:{row['version']}"
        if ep == "location":
            try:
                points[driver].append((LocationFix(event, raw["x"], raw["y"]), row["source_timestamp"], source))
            except (ValueError, TypeError, KeyError):
                return base | {"support": "INVALID_LOCATION_PAYLOAD", "withheld_feeds": ["location"]}
        elif ep == "laps":
            laps[driver].append(TimedValue(event, raw["lap_number"]-1, source))
        elif ep == "position":
            classifications[driver].append(TimedValue(event, raw["position"], source))
        elif ep == "pit":
            # The report proves a timing record was received. The strict
            # reconstruction still cannot infer a lane exit or a merge.
            pit[driver].append(TimedValue(event, "LANE_ENTRY_TIMING", source))
        elif ep == "race_control" and raw.get("message") in commands:
            control.append(TimedValue(event, commands[raw["message"]], source))
    if any(len({x.value for x in control if x.at == t}) > 1 for t in {x.at for x in control}):
        return base | {"support": "SOURCE_CONFLICT", "withheld_feeds": ["race_control"]}
    bounded = replace(session, fixes={d: reconcile_fixes(rows)[0] for d, rows in points.items()},
                      laps=indexed(laps), classification=indexed(classifications), pit=indexed(pit),
                      control=Series(control, lambda row: row.at), source_sha256=replay["prefix_sha256"])
    field = reconstruct_field(bounded, utc(replay["event_cutoff"]), FieldMode.CAUSAL)
    return base | {"support": "CONTROLLED_RECEIPT_FILTERED_FIELD", "withheld_feeds": [], "field": field}
