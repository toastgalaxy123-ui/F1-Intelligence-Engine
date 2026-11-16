"""Frozen Singapore cutoff audit without rebuilding or fitting D8R1."""

from dataclasses import asdict, fields
from datetime import timedelta
from json import loads
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

import duckdb

from f1ri.features.race_state import build_field_snapshot
from f1ri.features.temporal_maturity import (
    FIELD_MATRIX,
    SECTORS,
    Boundary,
    FieldValidity,
    visible_fields,
)
from f1ri.ingestion.compound_allocations import populate_silver_compound_allocations
from f1ri.ingestion.source_receipt import canonical, digest, utc
from f1ri.validation.historical_field_sources import INPUT_ARCHIVE, checked_members
from f1ri.validation.pit_branch_sources import PINNED
from f1ri.validation.strategy_sources import audit_model_assets
from f1ri.warehouse.build import _load_silver_table
from f1ri.warehouse.schema import SILVER_SCHEMAS

CUT = utc("2025-10-05T12:52:29.272Z")


def _driver_63(snapshot):
    return next(row.request for row in snapshot.drivers if row.driver_number == 63)


def d8r1_cutoff_audit(root):
    """Rebuild the same historical request and mutate only the future lap outcomes."""
    checked_members(root/INPUT_ARCHIVE, PINNED[INPUT_ARCHIVE])
    spec, _, assets = audit_model_assets(root)
    if assets["frozen_pace_replay"]["status"] != "VERIFIED_CONTINUATION_ONLY":
        raise ValueError("Frozen Stage 3-R/S1 request evidence is unavailable.")
    ref = root/spec["frozen_pace"]["path"]/"snapshot.json"
    saved = loads(ref.read_bytes())
    request = next(row["request"] for row in saved["drivers"] if row["driver_number"] == 63)
    with ZipFile(root/INPUT_ARCHIVE) as z:
        data = {ep: loads(z.read(f"singapore_timing/{ep}.json")) for ep in SILVER_SCHEMAS}
    laps = {r["lap_number"]: r for r in data["laps"] if r["driver_number"] == 63}
    original = laps[31]
    future = dict(original)
    future["lap_duration"] += 7
    for name in SECTORS:
        future[name] += 7/3
    future.update({"i1_speed": 1, "i2_speed": 1, "st_speed": 1,
                   "segments_sector_1": [1], "segments_sector_2": [2], "segments_sector_3": [3],
                   "is_pit_out_lap": not original["is_pit_out_lap"]})
    with TemporaryDirectory() as temp:
        path = Path(temp)/"prefix.duckdb"
        with duckdb.connect(str(path)) as db:
            db.execute("CREATE SCHEMA silver")
            for ep, rows in data.items():
                _load_silver_table(db, ep, rows, 9896)
            populate_silver_compound_allocations(db, root)
        evidence = {}
        vanilla = build_field_snapshot(path, 9896, CUT, 62, 20, clean_history_audit=evidence)
        with duckdb.connect(str(path)) as db:
            db.execute("""UPDATE silver.laps SET lap_duration=?, duration_sector_1=?, duration_sector_2=?,
                       duration_sector_3=?, is_pit_out_lap=? WHERE session_key=9896 AND driver_number=63
                       AND lap_number=31""",
                       [future[k] for k in ("lap_duration", *SECTORS, "is_pit_out_lap")])
        changed = build_field_snapshot(path, 9896, CUT, 62, 20)

    baseline_req = loads(canonical(asdict(_driver_63(vanilla))))
    # The archived serializer uses ISO 'T'; datetime identity is unchanged.
    request = request | {"as_of_utc": str(utc(request["as_of_utc"]))}
    exact = request == baseline_req
    same = canonical(asdict(vanilla)) == canonical(asdict(changed))
    scope = {
        "stage3_r_feature_path": "validation.stage3_r_strategy.load_controls reads the pinned S1 request",
        "original_request_builder": "features.race_state.build_field_snapshot",
        "silver_contract": list(SILVER_SCHEMAS["laps"]),
        "warehouse_causal_gate": "Next sequential line must already be in the query prefix. Implied result end must be "
                                 "at or before cutoff. Lap duration, sector durations and pit-out are masked otherwise.",
        "rolling_statistics": "Require prior lap numbers, completed times and clean green context.",
        "sector_fractions": "Use only positive complete sectors in the masked prefix.",
        "weather": "Latest source date at or before cutoff.",
        "stints": "Visible lap-start anchor and prior/adjacent pit report. Raw final stint lap is excluded.",
        "qualification": "Audited driver-63 cutoff request only; this is not a universal training or receipt audit.",
    }
    return original, future, utc(laps[32]["date_start"]), {
        "outcome": "NO_LEAKAGE_FOUND_IN_AUDITED_PATH" if same and exact else
        "LEAKAGE_CONFIRMED" if not same else "INCONCLUSIVE",
        "snapshot_manifest_sha256": digest(ref.read_bytes()), "same_frozen_request": exact,
        "mutated_request_unchanged": same, "complete_snapshot_sha256": digest(canonical(asdict(vanilla))),
        "cutoff": CUT, "request": baseline_req, "clean_stint_history": evidence[63],
        "scope": scope, "release": "MEAN_ONLY_VALIDATED", "historical_receipt": "UNAVAILABLE",
    }


def maturity_experiments(original, changed, following, audit):
    start = utc(original["date_start"])
    implied = start+timedelta(seconds=original["lap_duration"])
    inferred = Boundary(following, "ARCHIVED_NEXT_CONTIGUOUS_LINE")
    boundaries = {"finish": inferred}
    early, early_rows = visible_fields("laps", original, start, boundaries=boundaries)
    mutation, mutation_rows = visible_fields("laps", changed, start, boundaries=boundaries)
    after_at = max(following, implied)+timedelta(seconds=1)
    after, after_rows = visible_fields("laps", original, after_at, boundaries=boundaries)
    synthetic_late = after_at+timedelta(seconds=20)
    independent_later = Boundary(synthetic_late, "DECLARED_MUTATION_FIXTURE")
    changed_later, _ = visible_fields("laps", changed, synthetic_late, boundaries={"finish": independent_later})
    s1 = start+timedelta(seconds=original[SECTORS[0]])
    s1_boundary = Boundary(s1, "DECLARED_SECTOR_WITNESS", s1+timedelta(seconds=.2))
    first, _ = visible_fields("laps", original, s1+timedelta(seconds=1), boundaries={"sector_1": s1_boundary})
    unreceived, _ = visible_fields("laps", original, s1+timedelta(seconds=1),
                                   first_seen=s1+timedelta(seconds=3), processed=s1+timedelta(seconds=3.1),
                                   receipt=s1+timedelta(seconds=1), boundaries={"sector_1": s1_boundary})
    finish_obs = Boundary(following, "DECLARED_LINE_RECEIPT", after_at)
    received_final, _ = visible_fields("laps", original, after_at, first_seen=after_at, processed=after_at,
                                       receipt=after_at, boundaries={"finish": finish_obs})
    before_stint, _ = visible_fields("stints", {"session_key": 9896, "driver_number": 63, "lap_end": 31,
                                               "stint_number": 2, "compound": "SOFT"},
                                     start, first_seen=start, processed=start, receipt=start)
    test = {
        "T01": "lap_duration" not in early, "T02": first.get(SECTORS[0]) == original[SECTORS[0]],
        "T03": SECTORS[1] not in first, "T04": received_final.get("lap_duration") == original["lap_duration"],
        "T06": not unreceived, "T07": "compound" not in before_stint and "lap_end" not in before_stint,
        "T10": canonical((early, early_rows)) == canonical((mutation, mutation_rows)) and
        changed_later.get("lap_duration") != after.get("lap_duration"),
        "T11": "lap_duration" not in visible_fields("laps", original, after_at)[0],
        "T12": audit["outcome"] == "NO_LEAKAGE_FOUND_IN_AUDITED_PATH",
    }
    if not all(test.values()):
        raise ValueError(f"Temporal maturity control failed: {test}")
    return {"session": 9896, "driver": 63, "lap": 31, "start": start, "implied_finish": implied,
            "next_archived_line": following, "later_cutoff": after_at, "source_record": original,
            "lap_start_visible": early, "lap_start_field_status": early_rows,
            "later_visible_event_time_only": after, "later_status": after_rows,
            "declared_sector_example": first, "original_receipt": None, "controls": test,
            "mutation_of_later_values_changes_eligible_snapshot": True}


def matrix():
    return {
        "fields": {ep: {field: {"maturity": rule, "event_time_semantics": desc, "receipt_required": True,
                                "publication_verified": False}
                        for field, rule in spec.items()}
                   for (ep, spec), desc in zip(FIELD_MATRIX.items(), (
                       "Approximate lap-start anchor; aggregates mature separately.",
                       "Approximate track coordinates, route unproven.", "Competitive classification, not local order.",
                       "Reported gap or interval.", "Sample at date.", "Channel sample at date.",
                       "Reported message date, effect time unknown.", "Pit report is not entry or service completion.",
                       "No trustworthy timestamp for stint revisions.", "Static/scheduled metadata, no race finish proof.",
                   ), strict=True)},
        "schema": [f.name for f in fields(FieldValidity)],
    }
