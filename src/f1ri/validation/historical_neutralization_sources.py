"""S5-B archive verification and a census of the approved historical sources."""
import itertools
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from statistics import median
from zipfile import ZipFile

import duckdb

from f1ri.features.track_geometry import LocationFix
from f1ri.validation.overtake_historical_study import _load_json
from f1ri.validation.traffic_history_study import _sha

REPORTS = {
    "s5_a1": "stage5_s5_a1_pit_restart_stabilization",
    "s5_a": "stage5_s5_a_neutralization_mechanics",
    "s4_b1": "stage5_s4_b1_evidence_stabilization",
    "s4_b": "stage5_s4_b_historical_overtakes",
    "s4_a": "stage5_s4_a_overtaking_mechanics",
    "c4": "stage5_s3_c4_clean_reference_validation",
}
TABLES = ("silver.laps", "silver.intervals", "silver.position", "silver.pit", "silver.stints",
          "silver.race_control", "gold.track_status_intervals", "silver.sessions", "silver.drivers")


def clock(value):
    if not value:
        return None
    at = datetime.fromisoformat(value) if isinstance(value, str) else value
    if at.utcoffset() is None:
        raise ValueError("Archive clocks require a UTC offset")
    return at


def verify_releases(root, expected):
    reports = root / "artifacts/reports"
    parity = {}
    for key, prefix in REPORTS.items():
        observed = _sha(reports / f"{prefix}_metrics.json")
        if observed != expected[key]:
            raise ValueError(f"Frozen metrics differ for {key}")
        parity[key] = {"sha256": observed, "status": "METRICS_BYTES_VERIFIED"}
    manifest = _load_json(root / "config/experiments/stage5_s1_d8_manifest.json")
    candidate = root / manifest["model_path"]
    declared = manifest["files_sha256"][manifest["model_path"]]
    if declared != expected["d8"] or (candidate.exists() and _sha(candidate) != declared):
        raise ValueError("Frozen D8 hash differs")
    parity["d8"] = {"sha256": declared, "status": "MODEL_BYTES_VERIFIED" if candidate.exists()
                    else "FROZEN_MANIFEST_ONLY_MODEL_UNAVAILABLE"}
    return parity


def read_release(root, label):
    prefix = REPORTS[label]
    with ZipFile(root / "artifacts/reports" / f"{prefix}_ledgers.zip") as archive:
        sums = dict(line.strip().split(maxsplit=1)[::-1] for line in
                    archive.read("SHA256SUMS").decode().splitlines() if line.strip())
        for name, expected in sums.items():
            if sha256(archive.read(name)).hexdigest() != expected:
                raise ValueError(f"Frozen {label} archive differs at {name}")
        if archive.read("metrics.json") != (root / "artifacts/reports" / f"{prefix}_metrics.json").read_bytes():
            raise ValueError(f"Frozen {label} archive metrics differ from the verified release")
        return {name: json.loads(archive.read(name)) for name in sums if name.endswith(".json")}


def _timeline(rows, date_key, driver_key):
    dates = defaultdict(list)
    steps = []
    gaps = []
    for row in rows:
        if stamp := clock(row.get(date_key)):
            dates[row.get(driver_key)].append(stamp)
    for stamps in dates.values():
        unique = sorted(set(stamps))
        for a, b in itertools.pairwise(unique):
            step = (b-a).total_seconds()
            steps.append(step)
            if step > 2 and date_key == "date" and driver_key == "driver_number":
                gaps.append(step)
    times = [s for vals in dates.values() for s in vals]
    return {"earliest": min(times, default=None), "latest": max(times, default=None),
            "rows": len(rows), "drivers": len(dates.keys()-{None}),
            "median_sampling_s": median(steps) if steps else None,
            "max_spacing_s": max(steps, default=None), "within_source_intervals_over_2s": len(gaps)}


def _location_coverage(windows):
    spans = []
    for start, end in sorted(windows):
        if spans and start <= spans[-1][1]:
            spans[-1][1] = max(end, spans[-1][1])
        else:
            spans.append([start, end])
    return {"requested_ranges_utc": spans,
            "internal_request_gaps_utc": [[a[1], b[0]] for a, b in itertools.pairwise(spans)]}


@dataclass
class AuditSources:
    census: list
    runtime: dict
    old_location: dict
    new_location: dict
    laps: dict


def audit_sources(root, protocol, original, geometry_folder, warehouse):
    approved = protocol["cohort"]
    census = []
    counts = {}
    if warehouse.exists():
        with duckdb.connect(str(warehouse), read_only=True) as db:
            tables = {f"{s}.{t}" for s, t in db.execute(
                "SELECT table_schema, table_name FROM information_schema.tables").fetchall()}
            for table in TABLES:
                for session in approved:
                    count = db.execute(f"SELECT count(*) FROM {table} WHERE session_key=?", [session]).fetchone()[0] \
                        if table in tables else None
                    counts[table, session] = count
                    row = {"session": session, "endpoint": table, "source": "LOCAL_WAREHOUSE",
                           "rows": count, "status": "AVAILABLE" if count else
                           "NO_APPROVED_SESSION_ROWS" if count == 0 else "TABLE_UNAVAILABLE"}
                    if count:
                        result = db.execute(f"SELECT * FROM {table} WHERE session_key=?", [session])
                        fields = [col[0] for col in result.description]
                        dates = next((key for key in ("date", "date_start", "start_utc") if key in fields), None)
                        if dates:
                            row.update(_timeline([dict(zip(fields, r, strict=True)) for r in result.fetchall()],
                                                 dates, "driver_number"))
                    census.append(row)
    runtime = {
        "mode": "VERIFIED_RESEARCH_ARCHIVES_WITH_BOUNDED_RAW_SNAPSHOTS",
        "warehouse_present": warehouse.exists(),
        "local_warehouse_sha256": _sha(warehouse) if warehouse.exists() else None,
        "frozen_warehouse_sha256": original["warehouse_sha256"],
        "warehouse_backed_reconstruction_executed": False,
        "warehouse_approved_lap_rows": {s: counts.get(("silver.laps", s)) for s in approved},
        "stage3_runtime_present": (Path("C:/Projects/f1-race-intelligence") / "artifacts/models/degradation_v1").exists(),
        "stage1_interval_source": "ARCHIVED_RACE_CONTROL_MESSAGE_SUBSET",
        "raw_cache_roles": {
            s: {"role": role, "telemetry_manifest_files": len(list(
                (root / "data/telemetry/proximity" / str(s)).glob("**/*.meta.json"))),
                "raw_endpoint_filenames": sorted(p.name for p in
                    (root / "data/raw/openf1/year=2025" / f"session_key={s}").glob("*.json")
                    if not p.name.endswith(".meta.json")),
                "payloads_opened": False, "independent_episode_count": None}
            for s, role in protocol["cache_census_roles"].items()
        },
    }
    location = {"old": defaultdict(dict), "new": defaultdict(dict)}
    laps = {}
    windows = defaultdict(list)
    for session, race in approved.items():
        base = root / "data/telemetry/proximity" / str(session)
        added = root / "data/telemetry/s5_b" / str(session)
        for group, folder in (("old", base), ("new", added)):
            for path in sorted(folder.glob("**/*.meta.json")):
                meta = _load_json(path)
                data = path.with_name(path.name.replace(".meta.json", ".json"))
                digest = _sha(data)
                if digest != meta["sha256"] or meta["query"]["session_key"] != session:
                    raise ValueError(f"Source provenance differs at {data}")
                rows = _load_json(data)
                if len(rows) != meta["records"] or any(r["session_key"] != session for r in rows):
                    raise ValueError(f"Source record count or session differs at {data}")
                endpoint = data.stem
                stamp_key = "date_start" if endpoint == "laps" else "date"
                census.append({"session": session, "race": race["circuit"], "endpoint": endpoint,
                               "source": "BOUNDED_S5_B" if group == "new" else "APPROVED_EXISTING_CACHE",
                               "path": data.relative_to(root).as_posix(), "sha256": digest,
                               "retrieved_at_utc": meta["retrieved_at_utc"], "query": meta["query"],
                               "precision": "SOURCE_MILLISECOND_ACCURACY_UNMEASURED",
                               "receipt_time_known": False, "cutoff_policy": protocol["availability"],
                               "layout": "imola-2025-fia-sampled-v1" if session == 9987 else None,
                               "lineage": "OPENF1_TIMING" if endpoint == "laps" else "OPENF1_TELEMETRY",
                               **_timeline(rows, stamp_key, "driver_number")})
                if session == 9987 and endpoint == "location":
                    if "driver_number" not in meta["query"]:
                        windows[group].append((clock(meta["query"]["date>"]), clock(meta["query"]["date<"])))
                    for r in rows:
                        stamp = clock(r["date"])
                        record = LocationFix(stamp, r["x"], r["y"])
                        series = location[group][r["driver_number"]]
                        if stamp in series and series[stamp] != record:
                            series[stamp] = None  # Exclude conflicting coordinates at a single clock.
                        else:
                            series[stamp] = record
                if session == 9987 and endpoint == "laps" and group == "new":
                    laps[data.parent.name] = rows
    runtime["imola_raw_location_coverage"] = {group: _location_coverage(ranges) for group, ranges in windows.items()}
    for name, evidence in original["historical"].items():
        for endpoint, key, date, driver in (
            ("race_control", "observed_messages", "date", "driver_number"),
            ("lap_line", "observed_lines", "utc", "driver"),
            ("route_events", "observed_route_events", "utc", "driver"),
        ):
            census.append({"session": evidence["session"], "window": name, "endpoint": endpoint,
                           "source": "VERIFIED_S5_A_SOURCE_MANIFEST", "lineage": "OPENF1_TIMING_ARCHIVE",
                           "receipt_time_known": False, "raw_sha256": None,
                           **_timeline(evidence[key], date, driver)})
    for name in ("geometry.json", "physical_positions.parquet", "markers.parquet",
                 "coverage_driver_snapshots.parquet", "control_passages.parquet", "acquisition.json"):
        path = geometry_folder / name
        census.append({"session": 9987, "endpoint": name, "source": "S3_DERIVED_RESEARCH_ARTIFACT",
                       "sha256": _sha(path) if path.exists() else None,
                       "status": "AVAILABLE" if path.exists() else "UNAVAILABLE",
                       "layout": "imola-2025-fia-sampled-v1", "independent_of_raw_telemetry": False})
    return AuditSources(census, runtime, location["old"], location["new"], laps)
