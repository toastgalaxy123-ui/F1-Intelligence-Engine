"""S7-A.1 sources and immutable pilot cohort."""

from collections import defaultdict
from dataclasses import replace
from datetime import timedelta
from json import loads
from pathlib import Path
from zipfile import ZipFile

from f1ri.features.historical_field import reconcile_fixes
from f1ri.features.track_geometry import LocationFix
from f1ri.validation.historical_field_sources import (
    INPUT_ARCHIVE as S7A_INPUTS,
)
from f1ri.validation.historical_field_sources import (
    REPORTS,
    _source,
    checked_members,
    hash_bytes,
    load_pilots,
    stamp,
    write_archive,
)

PLAN = Path("config/experiments/stage7_s7_a1.json")
CACHE = Path("artifacts/runs/stage7_s7_a1_openf1_v1")
INPUTS = REPORTS / "stage7_s7_a1_inputs.zip"
INPUTS_SHA256 = "bbb29533153a7849fb782d039beb55a5a41b380be550dbc16981ed4c9b5eb3ad"
S7A_EVIDENCE = REPORTS / "stage7_s7_a_evidence.zip"
DASHBOARD = REPORTS / "stage7_s7_a1_dashboard.json"
PINNED = {
    S7A_EVIDENCE: "091a9a9c1dba45c95b3709f7aab816f351879579c1e0d22308b15f11cad33ca0",
    S7A_INPUTS: "241fa77a36d99a2048ea874b736dc1835674780953ca8a90d08aa6ddc41d2f40",
    REPORTS / "stage7_s7_a_metrics.json": "6942847c6a07972bcae10404ef48e7de1452bd3c5a333d30f2ea420dc704d66f",
    REPORTS / "stage7_s7_a_dashboard.json": "5cf465d069cc141da215489227d490c0f6bd308ca60651e6c19e7791ffb97159",
}


def freeze(root, output):
    files = {}
    for req in loads((root / PLAN).read_bytes())["acquisition"]["requests"]:
        folder = root / CACHE / req["id"]
        for p in sorted(folder.glob("*.json")):
            files[f"{req['id']}/{p.name}"] = p.read_bytes()
        if f"{req['id']}/request_outcome.json" not in files:
            raise ValueError(f"Acquisition not attempted: {req['id']}")
    if output.exists():
        raise FileExistsError(output)
    write_archive(output, files)
    return hash_bytes(output.read_bytes())


def _combine(session, extra):
    fixes = dict(session.fixes)
    merges = {}
    for driver, records in sorted(extra.items()):
        old = fixes.get(driver)
        old_records = [(f.point, f.raw_stamp, s) for f in old.rows for s in f.sources] if old else []
        fixes[driver], merges[driver] = reconcile_fixes((*old_records, *records))
        # The frozen loader has already quarantined any previous conflict.
        if old:
            conflicts = {r.point.at_utc for r in old.rows if r.conflict}
            if conflicts:
                from f1ri.features.historical_field import Series

                fixes[driver] = Series((replace(f, conflict=True) if f.point.at_utc in conflicts else f
                                        for f in fixes[driver].rows), lambda f: f.point.at_utc)
    return fixes, merges


def load(root):
    for p, expected in PINNED.items():
        if hash_bytes((root / p).read_bytes()) != expected:
            raise ValueError(f"Frozen S7-A source differs: {p}")
    archive_hash = hash_bytes((root / INPUTS).read_bytes())
    checked_members(root / INPUTS, INPUTS_SHA256)
    protocol = loads((root / PLAN).read_bytes())
    prior, source_meta = load_pilots(root)
    observations = defaultdict(lambda: defaultdict(list))
    sources = []
    requests = []
    with ZipFile(root / INPUTS) as archive:
        for req in protocol["acquisition"]["requests"]:
            prefix = req["id"]
            outcome = loads(archive.read(f"{prefix}/request_outcome.json"))
            if outcome["params"] != req["params"] or outcome["endpoint"] != req["endpoint"]:
                raise ValueError("The published acquisition must match the frozen query.")
            requests.append(outcome)
            if outcome["status"] != "RESPONSE_SAVED":
                continue
            session = req["params"]["session_key"]
            rows, manifest = _source(archive, f"{prefix}/location.json", session)
            if manifest["params"] != req["params"]:
                raise ValueError("Location metadata differs from the frozen request.")
            sources.append(manifest)
            earliest, latest = (stamp(req["params"][k]) for k in ("date>", "date<"))
            for row in rows:
                at = stamp(row["date"])
                if not earliest <= at <= latest:
                    raise ValueError("Acquired fix falls outside the approved source window.")
                observations[session][row["driver_number"]].append((
                    LocationFix(at, row["x"], row["y"]), row["date"], manifest["source"],
                ))
    merged = {}
    quality = {}
    for key, session in prior.items():
        fixes, quality[key] = _combine(session, observations[key])
        merged[key] = replace(session, fixes=fixes, source_sha256=hash_bytes(
            (session.source_sha256+archive_hash).encode()))
    visits = []
    for row in protocol["visits"]:
        start = stamp(row["entry"])
        stop = stamp(row["exit"])
        if row["session"] == 9987:
            found_entry = any(r["driver"] == row["driver"] and r["kind"] == "PIT_ENTRY" and
                              stamp(r["utc"]) == start for r in source_meta["imola_pit"])
            if not found_entry:
                raise ValueError(f"Missing frozen Imola pit report for {row['id']}.")
            exits = [stamp(r["utc"]) for r in source_meta["imola_pit"] if r["driver"] == row["driver"]
                     and r["kind"] == "PIT_EXIT" and 0 < (stamp(r["utc"])-start).total_seconds() <= 75]
            stop = min(exits, default=None)
        else:
            frozen = [r for r in source_meta["singapore_pit"] if r["driver_number"] == row["driver"] and
                      stamp(r["date"]) == start]
            if len(frozen) != 1:
                raise ValueError(f"Missing frozen Singapore pit report for {row['id']}.")
            computed = start+timedelta(seconds=frozen[0]["lane_duration"])
            if stop != computed:
                raise ValueError(f"Lane duration and timing endpoint differ for {row['id']}.")
        visits.append(row | {"entry": start, "exit": stop})
    return merged, prior, visits, protocol, {
        "sources": sources, "acquisition": requests, "inputs_sha256": archive_hash,
        "merge_duplicates_and_conflicts": quality, "earlier_sources": source_meta["frozen"],
        "pit_timing": {9896: source_meta["singapore_pit"], 9987: source_meta["imola_pit"]},
    }
