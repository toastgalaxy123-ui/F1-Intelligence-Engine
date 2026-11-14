"""Frozen Stage 3-R source census and bounded OpenF1 acquisition."""
import json
from hashlib import file_digest
from shutil import copyfile

import httpx
import yaml

from f1ri.config import load_config
from f1ri.ingestion.batch import load_experiment_cohort
from f1ri.ingestion.openf1 import OpenF1Client
from f1ri.ingestion.raw_cache import snapshot_path
from f1ri.validation.proximity_pilot import _json

PLAN = "config/experiments/stage3_r1.yaml"


def digest(path):
    with path.open("rb") as file:
        return file_digest(file, "sha256").hexdigest()


def study_inputs(root):
    plan = yaml.safe_load((root/PLAN).read_bytes())
    old = root/plan["historical_config"]
    if digest(old) != plan["historical_config_sha256"]:
        raise ValueError("Frozen Stage 3 specification differs")
    entries = load_experiment_cohort(root, old)
    d8 = json.loads((root/"config/experiments/stage5_s1_d8_manifest.json").read_bytes())
    for role, field in (("train", "train_sessions"), ("val", "calibration_sessions")):
        if {e["session_key"] for e in entries if e["split"] == role} != set(d8[field]):
            raise ValueError(f"Historical cohort disagrees with D8 {role} metadata")
    if len({e["session_key"] for e in entries}) != len(entries):
        raise ValueError("Duplicate session in frozen cohort")
    return plan, entries


def _endpoint_paths(root, cache, year, session, endpoint):
    return [snapshot_path(p, year, endpoint, session) for p in (cache, root)]


def _valid(path):
    if not path.is_file():
        return None
    meta = json.loads(path.with_suffix(".meta.json").read_bytes())
    if meta["sha256"] != digest(path):
        raise ValueError(f"OpenF1 snapshot hash mismatch at {path}")
    return {k: meta[k] for k in ("sha256", "records", "retrieved_at_utc", "query", "source")}


def census(root, cache, entries):
    data = load_config(root)["data"]
    endpoints = data["core_endpoints"] + data["additional_endpoints"]
    return [e | {"endpoints": {ep: next((record for p in _endpoint_paths(
        root, cache, e["year"], e["session_key"], ep) if (record := _valid(p))), None) for ep in endpoints}}
        for e in entries]


def prepare_reference(root, cache):
    if cache.resolve() == root.resolve():
        raise ValueError("Use an isolated reconstruction cache outside the original warehouse root")
    for name in ("config/default.yaml", "data/raw/reference/pirelli_allocations.csv"):
        dest = cache/name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            copyfile(root/name, dest)
        if digest(dest) != digest(root/name):
            raise ValueError(f"Reconstruction reference mismatch: {name}")


def stage_cached_input(root, cache, year, session, endpoint):
    dst, src = _endpoint_paths(root, cache, year, session, endpoint)
    if not dst.exists() and _valid(src):
        dst.parent.mkdir(parents=True, exist_ok=True)
        copyfile(src, dst)
        copyfile(src.with_suffix(".meta.json"), dst.with_suffix(".meta.json"))


def acquire(root, cache):
    plan, entries = study_inputs(root)
    records = [e for e in census(root, cache, entries) if e["split"] in plan["acquisition"]["roles"]]
    data = load_config(root)["data"]
    endpoints = data["core_endpoints"] + data["additional_endpoints"]
    order = ("train", "val", "test")
    # Interleave groups so an interrupted run leaves evidence from each role.
    groups = [[r for r in records if r["split"] == role] for role in order]
    records = [g[i] for i in range(max(map(len, groups))) for g in groups if i < len(g)]
    missing = [{"session": r["session_key"], "year": r["year"], "race": r["label"], "role": r["split"],
                "endpoint": ep, "reason": "required original warehouse/clean-air input",
                "range": "declared completed race session", "existing_cached": False}
               for r in records for ep, meta in r["endpoints"].items() if not meta]
    catalogs = sorted({r["year"] for r in records
                       if not any(p.is_file() for p in _endpoint_paths(root, cache, r["year"], None, "sessions"))})
    if len(missing)+len(catalogs) > plan["acquisition"]["logical_request_limit"]:
        raise ValueError("Stage 3-R acquisition exceeds the preregistered request limit")
    prepare_reference(root, cache)
    manifest = {"plan": plan["acquisition"], "missing": missing, "uncached_qualifying_catalog_years": catalogs,
                "logical_requests_planned": len(missing)+len(catalogs)}
    if not (cache/"acquisition_protocol.json").exists():
        _json(cache/"acquisition_protocol.json", manifest)
    failures = {}
    with OpenF1Client(data["openf1_base_url"], plan["acquisition"]["min_seconds_between_requests"]) as client:
        def cached(year, session, ep, params=None):
            stage_cached_input(root, cache, year, session, ep)
            return client.cached_get(cache, endpoint=ep, year=year, session_key=session, params=params)

        for row in records:
            session, year = row["session_key"], row["year"]
            for ep in endpoints:
                try:
                    query = None
                    if ep == "starting_grid" and not any(
                        p.is_file() for p in _endpoint_paths(root, cache, year, session, ep)
                    ):
                        info = cached(year, session, "sessions")
                        qual = [r for r in cached(year, None, "sessions") if r.get("session_name") == "Qualifying"
                                and r.get("meeting_key") == info[0].get("meeting_key")]
                        if len(qual) != 1:
                            raise ValueError("Qualifying session is ambiguous")
                        query = {"session_key": qual[0]["session_key"]}
                    rows = cached(year, session, ep, query)
                    if ep == "sessions" and (len(rows) != 1 or rows[0]["year"] != year
                                              or rows[0]["session_name"] != "Race"):
                        raise ValueError("Expected one year-matched Grand Prix race")
                except (ValueError, RuntimeError, OSError, IndexError, httpx.HTTPError) as error:
                    failures[f"{session}/{ep}"] = str(error)
                    if ep == "sessions":
                        break
            print(f"Stage 3-R {session}: {row['label']} source scan complete", flush=True)
            _json(cache/"acquisition_failures.json", failures)
    return failures
