"""Bounded acquisition and integrity checks for the S7-A pilot inputs."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import loads
from pathlib import Path
from urllib.parse import urlencode
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import httpx

from f1ri.features.historical_field import Series, Session, TimedValue, indexed, reconcile_fixes
from f1ri.features.track_geometry import LocationFix, TrackGeometry, build_racing_loop
from f1ri.ingestion.openf1 import OpenF1Client
from f1ri.ingestion.raw_cache import save_snapshot

PROTOCOL = Path("config/experiments/stage7_s7_a.json")
REPORTS = Path("artifacts/reports")
INPUT_CACHE = Path("artifacts/runs/stage7_s7_a_openf1_v1")
INPUT_ARCHIVE = REPORTS / "stage7_s7_a_inputs.zip"
S5A = REPORTS / "stage5_s5_a_neutralization_mechanics_ledgers.zip"
S5B = REPORTS / "stage5_s5_b_historical_neutralization_validation_ledgers.zip"
S6C = REPORTS / "stage6_s6_c_evidence.zip"
SINGAPORE = Path("artifacts/runs/stage3_r1_sources/data/raw/openf1/year=2025/session_key=9896")
FROZEN = {
    S5A: "f4529337aadccd8e4e3387c84dd32ff2534d8dd54bb315d0bb67ae9f62583591",
    S5B: "b3453e1da24093ee370b318824e81d8d3a927f91a56fa65c1ab9173689b4db3c",
    REPORTS / "stage5_s3_geometry.json": "7ac05df0e983688320103bec464b14c5393963d91f03c6b9ca76b8a78e5e5292",
    REPORTS / "stage5_s3_c_geometry_9987.json": "1329f6890e8ffdde3ad9f815353ef9588a8ad087a33c9ac6e229b3a45b31f10d",
    S6C: "dd37f824d2f521e5129ae570bbe42cd9f4901977caac83eee18140e2fea343d6",
    REPORTS / "stage6_s6_c_metrics.json": "0d0bbefa9573b529ae70eb780d78503aef353b50c15cf6b214df474388d8172e",
    INPUT_ARCHIVE: "241fa77a36d99a2048ea874b736dc1835674780953ca8a90d08aa6ddc41d2f40",
}


def stamp(raw):
    if not raw:
        return None
    clock = datetime.fromisoformat(raw) if isinstance(raw, str) else raw
    if clock.utcoffset() is None:
        raise ValueError("Archive event clocks require an explicit UTC offset.")
    return clock.astimezone(UTC)


def hash_bytes(raw):
    return sha256(raw).hexdigest()


def checked_members(path, frozen=None):
    raw = path.read_bytes()
    if frozen and hash_bytes(raw) != frozen:
        raise ValueError(f"Frozen archive differs: {path.name}")
    with ZipFile(path) as archive:
        entries = {name: expected for expected, name in (
            row.split("  ", 1) for row in archive.read("SHA256SUMS").decode().splitlines() if row)}
        if set(archive.namelist()) != entries.keys() | {"SHA256SUMS"} or len(archive.namelist()) != len(entries)+1:
            raise ValueError(f"Unexpected archive contents: {path.name}")
        for name, expected in entries.items():
            if hash_bytes(archive.read(name)) != expected:
                raise ValueError(f"Checksum mismatch: {path.name}/{name}")
    return entries


def write_archive(path, files):
    """Write a reproducible, checksummed archive with stable member metadata."""
    sums = "".join(f"{hash_bytes(raw)}  {name}\n" for name, raw in sorted(files.items())).encode()
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, raw in sorted(files.items() | {"SHA256SUMS": sums}.items()):
            archive.writestr(ZipInfo(name, (1980, 1, 1, 0, 0, 0)), raw, compress_type=ZIP_DEFLATED)


def freeze_inputs(root, output):
    """Freeze the four new responses and the untracked Singapore archive dependencies."""
    files = {}
    protocol = loads((root / PROTOCOL).read_bytes())
    for req in protocol["acquisition"]["requests"]:
        folder = root / INPUT_CACHE / req["id"]
        for p in sorted(folder.glob("*.json")):
            files[f"acquired/{req['id']}/{p.name}"] = p.read_bytes()
        if f"acquired/{req['id']}/request_outcome.json" not in files:
            raise ValueError(f"Acquisition outcome missing: {req['id']}")
    for endpoint in ("sessions", "drivers", "laps", "position", "intervals", "pit",
                     "stints", "race_control", "weather", "session_result", "starting_grid"):
        for suffix in (".json", ".meta.json"):
            name = endpoint + suffix
            files[f"singapore_timing/{name}"] = (root / SINGAPORE / name).read_bytes()
    # The S3 later-window control is independent of the earlier geometry fit.
    for p in sorted((root / "data/telemetry/proximity/9896").glob("*/b7e2b38ee93634b0/location*.json")):
        files[f"singapore_later/{p.parents[1].name}/{p.name}"] = p.read_bytes()
    with output.open("xb"):
        pass
    write_archive(output, files)
    return hash_bytes(output.read_bytes())


def acquire(root):
    """Attempt only the frozen queries; every outcome is retained in the new cache."""
    return acquire_protocol(root / PROTOCOL, root / INPUT_CACHE)


def acquire_protocol(protocol, out):
    """Run a versioned bounded acquisition plan using the established raw cache."""
    from f1ri.validation.proximity_pilot import _json

    config = loads(protocol.read_bytes())["acquisition"]
    requests = config["requests"]
    if len(requests) != config["logical_request_budget"]:
        raise ValueError("Acquisition manifest and request budget disagree.")
    out.mkdir(parents=True, exist_ok=True)
    outcomes = []
    with OpenF1Client(config["endpoint_base"], config["minimum_call_interval_s"]) as client:
        for query in requests:
            endpoint, params = query["endpoint"], query["params"]
            folder = out / query["id"]
            output = folder / f"{endpoint}.json"
            manifest = folder / "request_outcome.json"
            if manifest.exists():
                outcomes.append(loads(manifest.read_bytes()))
                continue
            url = f"{config['endpoint_base']}/{endpoint}?{urlencode(params)}"
            record = {"id": query["id"], "endpoint": endpoint, "params": params, "url": url}
            try:
                payload, source_url = client.fetch(endpoint, params)
                save_snapshot(output, payload, url=source_url, params=params)
                record.update(status="RESPONSE_SAVED", http_status=200, url=source_url)
            except (httpx.HTTPError, RuntimeError, ValueError) as error:
                code = getattr(getattr(error, "response", None), "status_code", None)
                if code is None and "Access denied (" in str(error):
                    code = int(str(error).split("Access denied (")[1][:3])
                record.update(status="UNAVAILABLE", http_status=code, failure=type(error).__name__)
            record["retrieved_at_utc"] = datetime.now(UTC)
            folder.mkdir(parents=True, exist_ok=True)
            _json(manifest, record)
            outcomes.append(record)
    return outcomes


def _source(archive, name, session):
    raw = archive.read(name)
    meta = loads(archive.read(name.removesuffix(".json") + ".meta.json"))
    rows = loads(raw)
    if (hash_bytes(raw) != meta["sha256"] or len(rows) != meta["records"]
            or meta["query"].get("session_key") != session or any(r["session_key"] != session for r in rows)):
        raise ValueError(f"Session or row integrity mismatch in {name}")
    source = f"{Path(archive.filename).name}::{name}"
    stamps = [stamp(r.get("date", r.get("date_start"))) for r in rows]
    stamps = [s for s in stamps if s]
    return rows, {"source": source, "session": session, "endpoint": Path(name).stem, "records": len(rows),
                  "sha256": meta["sha256"], "url": meta["url"], "params": meta["query"],
                  "retrieved_at_utc": meta["retrieved_at_utc"], "earliest": min(stamps, default=None),
                  "latest": max(stamps, default=None), "drivers": sorted({r["driver_number"] for r in rows
                                                                        if r.get("driver_number") is not None}),
                  "receipt": "UNKNOWN", "revision": meta.get("revision")}


def _timed(rows, source, clock_key, value):
    values = defaultdict(list)
    for row in rows:
        if (clock := stamp(row.get(clock_key))) is not None:
            values[row["driver_number"]].append(TimedValue(clock, value(row), source))
    return values


def _control(rows, source):
    by_message = {"VIRTUAL SAFETY CAR DEPLOYED": "VSC_REPORTED", "SAFETY CAR DEPLOYED": "SC_REPORTED",
                  "VIRTUAL SAFETY CAR ENDING": "VSC_ENDING_REPORTED", "SAFETY CAR IN THIS LAP": "SC_IN_REPORTED",
                  "TRACK CLEAR": "TRACK_CLEAR_REPORTED"}
    return Series((TimedValue(stamp(row["date"]), by_message[row["message"]], source)
                   for row in rows if row["message"] in by_message), lambda row: row.at)


def load_pilots(root):
    """Load only immutable releases; source files are opened once per pilot run."""
    figures = {}
    for path, expected in FROZEN.items():
        if hash_bytes((root / path).read_bytes()) != expected:
            raise ValueError(f"Frozen source differs: {path}")
        figures[path.as_posix()] = expected
    manifests = []
    locations = defaultdict(lambda: defaultdict(list))
    protocol = loads((root / PROTOCOL).read_bytes())
    with ZipFile(root / INPUT_ARCHIVE) as primary, ZipFile(root / S5B) as imola, ZipFile(root / S5A) as prior:
        acquired = [loads(primary.read(f"acquired/{query['id']}/request_outcome.json"))
                    for query in protocol["acquisition"]["requests"]]
        for archive in (primary, imola):
            for name in sorted(archive.namelist()):
                if not name.endswith("/location.json"):
                    continue
                meta = loads(archive.read(name.removesuffix(".json") + ".meta.json"))
                session = meta["query"]["session_key"]
                rows, source = _source(archive, name, session)
                manifests.append(source)
                for row in rows:
                    coords = LocationFix(stamp(row["date"]), row["x"], row["y"])
                    locations[session][row["driver_number"]].append((coords, row["date"], source["source"]))
        singapore = {}
        for name in ("sessions", "drivers", "laps", "position", "intervals", "pit", "stints",
                     "race_control", "weather", "session_result", "starting_grid"):
            singapore[name], source = _source(primary, f"singapore_timing/{name}.json",
                                               9892 if name == "starting_grid" else 9896)
            if name == "starting_grid":
                source["related_race_session"] = 9896
            manifests.append(source)
        imola_laps = defaultdict(list)
        for window in ("vsc_timing", "sc_timing"):
            rows, source = _source(imola, f"source_snapshots/{window}/laps.json", 9987)
            manifests.append(source)
            for driver, values in _timed(rows, source["source"], "date_start", lambda r: r["lap_number"]-1).items():
                imola_laps[driver].extend(values)
        episodes = [r for r in loads(imola.read("historical_neutralization_intervals.json"))
                    if r["session_key"] == 9987 and r["status"] in ("SC", "VSC")]
        messages = [r for r in loads(imola.read("race_control_events.json")) if r["session_key"] == 9987]
        visits = {}
        for evidence in loads(prior.read("source_manifest.json"))["historical"].values():
            if evidence["session"] != 9987:
                continue
            for row in evidence["observed_route_events"]:
                visits[row["utc"], row["driver"], row["kind"]] = row
    fixes, quality = {}, {}
    for session, records in locations.items():
        fixes[session], quality[session] = {}, {}
        for driver, rows in records.items():
            fixes[session][driver], quality[session][driver] = reconcile_fixes(rows)

    references = protocol["pilots"]["9896"]
    first, end = stamp(references["reference_start"]), stamp(references["reference_next_line"])
    reference_rows = fixes[9896][references["reference_driver"]].window(first-timedelta(seconds=2),
                                                                         end+timedelta(seconds=2))
    reference_fixes = [row.point for row in reference_rows if not row.conflict]
    sgp = build_racing_loop(reference_fixes, start=first, end=end, version="singapore-2025-earlier-reference-s7a",
                           length_m=4927., available_at_utc=max(p.at_utc for p in reference_fixes))
    geometry_path = REPORTS / "stage5_s3_c_geometry_9987.json"
    doc = loads((root / geometry_path).read_bytes())
    imola_geometry = TrackGeometry(doc["version"], doc["length_m"], stamp(doc["available_at_utc"])+timedelta(seconds=1.25),
                                   tuple(map(tuple, doc["points_xy"])), doc["reference_end_gap_m"])
    singapore_source = f"{INPUT_ARCHIVE.name}::singapore_timing"
    imola_source = f"{S5B.name}::race_control_events.json"
    historic_pit = _timed(({"driver_number": r["driver"], "date": r["utc"], "kind": r["kind"]}
                           for r in visits.values()), S5A.name, "date",
                          lambda r: "LANE_ENTRY_TIMING" if r["kind"] == "PIT_ENTRY" else "LANE_EXIT_TIMING")
    sgp_timing = _timed(singapore["pit"], f"{singapore_source}/pit.json", "date", lambda _: "LANE_ENTRY_TIMING")
    meta = singapore["sessions"][0]
    sessions = {
        9896: Session(9896, "Singapore 2025", tuple(r["driver_number"] for r in singapore["drivers"]),
                      stamp(meta["date_start"]), stamp(meta["date_end"]), sgp, "acquired/singapore_reference/location.json",
                      fixes[9896], indexed(_timed(singapore["laps"], f"{singapore_source}/laps.json", "date_start",
                                                lambda r: r["lap_number"]-1)),
                      indexed(_timed(singapore["position"], f"{singapore_source}/position.json", "date",
                                     lambda r: r["position"])), indexed(sgp_timing),
                      _control(singapore["race_control"], f"{singapore_source}/race_control.json"),
                      FROZEN[INPUT_ARCHIVE]),
        9987: Session(9987, "Imola 2025", tuple(imola_laps), stamp("2025-05-18T13:36:30+00:00"),
                      stamp("2025-05-18T14:25:30+00:00"), imola_geometry, geometry_path.as_posix(), fixes[9987],
                      indexed(imola_laps), {}, indexed(historic_pit), _control(messages, imola_source),
                      hash_bytes((FROZEN[INPUT_ARCHIVE]+FROZEN[S5B]+FROZEN[S5A]).encode()), tuple(episodes)),
    }
    return sessions, {"sources": manifests, "acquisition": acquired, "frozen": figures, "duplicate_audit": quality,
                      "protocol": protocol, "imola_pit": list(visits.values()), "singapore_pit": singapore["pit"]}
