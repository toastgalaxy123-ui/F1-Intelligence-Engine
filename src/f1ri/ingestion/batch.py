"""Batch historical ingestion, warehouse build, and validation manifest generation."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import yaml

from f1ri.cli import ingest
from f1ri.config import load_config
from f1ri.ingestion.raw_cache import snapshot_path
from f1ri.validation.quality import assert_valid, check_session
from f1ri.warehouse.build import build_warehouse


def load_experiment_cohort(root: Path, experiment_config_path: Path | None = None) -> list[dict[str, Any]]:
    cfg = load_config(root)
    rel_path = experiment_config_path or Path(
        cfg.get("modeling", {}).get(
            "default_experiment_config", "config/experiments/stage2_pace_v1.yaml"
        )
    )
    full_path = rel_path if rel_path.is_absolute() else (root / rel_path)
    exp_cfg = yaml.safe_load(full_path.read_text(encoding="utf-8"))
    cohort_cfg = exp_cfg.get("cohort", {})
    entries: list[dict[str, Any]] = []
    for split_name, items in cohort_cfg.items():
        for item in items or []:
            entries.append(
                {
                    "split": split_name,
                    "year": int(item["year"]),
                    "session_key": int(item["session_key"]),
                    "label": str(item.get("label", f"{item['year']}_{item['session_key']}")),
                    "inspected": bool(item.get("inspected", False)),
                }
            )
    return entries


def run_batch_ingestion(
    root: Path,
    entries: list[dict[str, Any]] | None = None,
    *,
    full: bool = True,
    refresh: bool = False,
    manifest_out: Path | None = None,
) -> dict[str, Any]:
    """Ingest, build, and validate a batch of race sessions while recording a manifest."""
    cfg = load_config(root)
    target_entries = entries if entries is not None else load_experiment_cohort(root)
    required_endpoints = list(cfg["data"]["core_endpoints"])
    if full:
        required_endpoints += list(cfg["data"]["additional_endpoints"])

    manifest_records: list[dict[str, Any]] = []
    for entry in target_entries:
        year = int(entry["year"])
        session_key = int(entry["session_key"])
        split = str(entry.get("split", "unspecified"))
        label = str(entry.get("label", f"{year}_{session_key}"))
        inspected = bool(entry.get("inspected", False))

        rec: dict[str, Any] = {
            "session_key": session_key,
            "year": year,
            "split": split,
            "label": label,
            "inspected": inspected,
            "raw_complete": False,
            "warehouse_built": False,
            "integrity_passed": False,
            "clean_air_classified": False,
            "quality_metrics": {},
            "endpoint_sha256": {},
            "status": "PENDING",
            "failure_reason": None,
        }
        try:
            ingest(root, year=year, session_key=session_key, full=full, refresh=refresh)
            sha_map: dict[str, str] = {}
            for ep in required_endpoints:
                p = snapshot_path(root, year, ep, session_key)
                meta_p = p.with_suffix(".meta.json")
                if not p.exists():
                    raise FileNotFoundError(f"Missing snapshot after ingest: {p}")
                if meta_p.exists():
                    meta = json.loads(meta_p.read_text(encoding="utf-8"))
                    sha_map[ep] = meta.get("sha256", "")
            rec["raw_complete"] = True
            rec["endpoint_sha256"] = sha_map

            build_warehouse(root, year=year, session_key=session_key)
            rec["warehouse_built"] = True

            q_metrics = check_session(root, session_key=session_key)
            rec["quality_metrics"] = q_metrics
            assert_valid(q_metrics)
            rec["integrity_passed"] = True
            rec["clean_air_classified"] = q_metrics.get("clean_green_pace_laps", 0) > 0

            # Enrich meeting_key and circuit_short_name from silver.sessions
            db_path = root / "data" / "warehouse" / "f1ri.duckdb"
            con = duckdb.connect(str(db_path), read_only=True)
            try:
                s_row = con.execute(
                    """
                    SELECT meeting_key, circuit_key, circuit_short_name, country_name
                    FROM silver.sessions
                    WHERE session_key = ?
                    """,
                    [session_key],
                ).fetchone()
                if s_row:
                    rec["meeting_key"] = int(s_row[0]) if s_row[0] is not None else None
                    rec["circuit_key"] = int(s_row[1]) if s_row[1] is not None else None
                    rec["circuit_short_name"] = s_row[2]
                    rec["country_name"] = s_row[3]
            finally:
                con.close()

            rec["status"] = "READY"
            print(
                f"[BATCH OK] {label:26} | session_key={session_key:<6} | "
                f"laps={q_metrics.get('laps', 0):<4} | "
                f"clean_green={q_metrics.get('clean_green_pace_laps', 0):<4}"
            )
        except Exception as exc:  # noqa: BLE001
            rec["status"] = "FAILED"
            rec["failure_reason"] = f"{type(exc).__name__}: {exc}"
            print(f"[BATCH FAIL] {label:24} | session_key={session_key:<6} | {rec['failure_reason']}")

        manifest_records.append(rec)

    manifest = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "total_sessions": len(manifest_records),
        "ready_sessions": sum(1 for r in manifest_records if r["status"] == "READY"),
        "failed_sessions": sum(1 for r in manifest_records if r["status"] != "READY"),
        "sessions": manifest_records,
    }

    out_path = manifest_out or (
        root / "artifacts" / "runs" / "stage2_pace_v1" / "data_manifest.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
