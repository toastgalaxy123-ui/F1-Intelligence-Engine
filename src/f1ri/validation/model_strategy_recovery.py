"""Recover only the repository-pinned D8 artifact and summarize saved support."""
import importlib.metadata
import subprocess
from pathlib import Path
from zipfile import ZipFile

import numpy as np

from f1ri.simulator.pace_adapter import load_pace_bundle, reference_parity
from f1ri.validation.overtake_historical_study import _load_json
from f1ri.validation.traffic_history_study import _sha

D8 = "config/experiments/stage5_s1_d8_manifest.json"


def _git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    return result.stdout.splitlines() if result.returncode == 0 else []


def recover_bundle(root, stage3_root, backup=None):
    manifest = _load_json(root / D8)
    keys = manifest["files_sha256"]
    model = manifest["model_path"]
    basename = Path(model).name
    report_files = sorted((root/"artifacts/reports").glob("*.zip"))
    if backup is not None and backup.is_file():
        report_files.append(backup)
    archives = {}
    for p in report_files:
        with ZipFile(p) as archive:
            archives[p.name] = [n for n in archive.namelist() if any(n.endswith(k) for k in keys)
                                and not n.endswith("pirelli_allocations.csv")]
    git_objects = [s.split(" ", 1)[1] for s in _git(root, "rev-list", "--objects", "--all")
                   if " " in s and (s.endswith(basename) or "/stage3_degradation_v1/" in s)]
    lfs = [s for s in _git(root, "lfs", "ls-files", "--all") if basename in s]
    audit = {"status": "MODEL_UNAVAILABLE", "metadata": manifest, "roots": {},
             "git_matching_paths": git_objects, "lfs_matching_paths": lfs, "archive_members": archives,
             "package_versions": {name: importlib.metadata.version(name) for name in ("numpy", "pandas", "scikit-learn")},
             "original_model_loaded": False, "reference_parity": None}
    bundle = None
    for label, directory in (("original_stage3", stage3_root), ("current_project", root)):
        files = {name: {"expected_sha256": digest, "observed_sha256": _sha(directory/name)
                       if (directory/name).is_file() else None} for name, digest in keys.items()}
        for row in files.values():
            row["status"] = ("MISSING" if row["observed_sha256"] is None else "VERIFIED"
                             if row["observed_sha256"] == row["expected_sha256"] else "HASH_MISMATCH")
        audit["roots"][label] = {"exists": directory.is_dir(), "files": files}
        if not all(row["status"] == "VERIFIED" for row in files.values()):
            if files[model]["status"] == "VERIFIED" and audit["status"] == "MODEL_UNAVAILABLE":
                audit["status"] = "PARTIAL_MODEL_BUNDLE"
            continue
        try:
            bundle = load_pace_bundle(root, directory)
            parity = reference_parity(bundle, root/"tests/fixtures/stage5_s1_d8_reference.json")
        except (ValueError, OSError, ImportError, AttributeError, KeyError) as error:
            audit["status"] = "MODEL_INCOMPATIBLE"
            audit["load_reason"] = str(error)
            bundle = None
            continue
        audit.update(status="ORIGINAL_MODEL_VERIFIED", original_model_loaded=True,
                     verified_root=label, reference_parity=parity)
        break
    frozen = _load_json(root/"config/experiments/stage3_frozen_reference.json")["files_sha256"]
    audit["frozen_source_hashes"] = {p: _sha(root/p) == h for p, h in frozen.items()}
    audit["historic_warehouse_available"] = (stage3_root/"data/warehouse/f1ri.duckdb").is_file()
    audit["local_warehouse_sha256"] = _sha(root/"data/warehouse/f1ri.duckdb") if (
        root/"data/warehouse/f1ri.duckdb").is_file() else None
    return audit, bundle


def saved_samples_summary(root, prior, indices, horizons):
    info = prior["frozen_pace"]
    folder = root/info["path"]
    if any(not (folder/n).is_file() or _sha(folder/n) != h for n, h in info["files"].items()):
        return {"status": "MODEL_UNAVAILABLE", "rows": [], "sample_count": 0}
    snapshot = _load_json(folder/"snapshot.json")
    requests = {r["driver_number"]: r["request"] for r in snapshot["drivers"]}
    out = []
    with np.load(folder/"trajectory_samples.npz", allow_pickle=False) as data:
        drivers = sorted(int(k) for k in data.files)
        for driver in drivers:
            batch = data[str(driver)]
            r = requests[driver]
            for k in horizons:
                sub = batch[:, :k]
                totals = sub.sum(axis=1)
                out.append({
                    "driver": driver, "horizon": k, "compound": r["compound_relative"], "start_age": r["tyre_age_at_start"],
                    "planned_start_lap": r["planned_start_lap"], "mean_lap_s": float(sub[:, -1].mean()),
                    "sd_lap_s": float(sub[:, -1].std(ddof=1)), "mean_cumulative_s": float(totals.mean()),
                    "sd_cumulative_s": float(totals.std(ddof=1)),
                    "p05_cumulative_s": float(np.quantile(totals, .05)), "p95_cumulative_s": float(np.quantile(totals, .95)),
                })
        driver_pair = (4, 63)
        series = data[str(driver_pair[1])]
        corr = float(np.corrcoef(series[:, 0], series[:, 1])[0, 1])
        between = float(np.corrcoef(data[str(driver_pair[0])][:, 0], series[:, 0])[0, 1])
        sample_rows = {i: {d: data[str(d)][i].tolist() for d in driver_pair} for i in indices}
    summary = {"status": "FROZEN_PACE_CONTINUATION", "rows": out, "sample_count": len(series),
               "drivers": drivers, "within_car_63_lap1_lap2_correlation": corr,
               "between_4_63_lap1_sample_correlation": between,
               "cross_driver_policy": "S1_INDEPENDENT_SEED_PER_DRIVER_STINT",
               "sample_rows": sample_rows, "source_hashes": info["files"], "feature_cutoff": snapshot["as_of_utc"]}
    return summary


def feature_audit(root, prior):
    folder = root/prior["frozen_pace"]["path"]
    if not (folder/"snapshot.json").exists() or _sha(folder/"snapshot.json") != prior["frozen_pace"]["files"]["snapshot.json"]:
        return {"status": "REQUEST_UNSUPPORTED", "rows": []}
    s = _load_json(folder/"snapshot.json")
    drivers = [r for r in s["drivers"] if r["request"]]
    return {"status": "REQUEST_UNSUPPORTED", "session": s["session_key"], "source_cutoff": s["as_of_utc"],
            "weather_observed_at_utc": s["weather_observed_at_utc"], "role": "FROZEN_PILOT",
            "reason": ["D8 bundle unavailable or requires independent initialization",
                       "No exact historical predecessor field established at this S1 cutoff",
                       "No verified replacement set inventory and physical branch/merge for the field"],
            "rows": [{"driver": d["driver_number"], "completed_laps": d["completed_laps"],
                      "last_crossing_utc": d["last_crossing_utc"], "request": d["request"],
                      "exact_physical_order": None, "replacement_set_inventory": None,
                      "eligibility": "CONTINUATION_ARCHIVE_ONLY"} for d in drivers]}
