"""Opt-in bounded pit traces. Never fetch all-session high-frequency data."""
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pandas as pd

from f1ri.features.pit_events import utc
from f1ri.ingestion.openf1 import OpenF1Client
from f1ri.ingestion.raw_cache import read_snapshot, save_snapshot


def ingest_pit_telemetry(
    root: Path, client: OpenF1Client, *, session_key: int, driver_number: int,
    pit_event_id: str, start_utc, end_utc, refresh: bool = False,
) -> dict:
    start, end = utc(start_utc), utc(end_utc)
    if start is None or end is None or not 0 < (end-start).total_seconds() <= 180:
        raise ValueError("Pit telemetry window must be positive and at most 180 seconds")
    if any(c not in "0123456789:-_" for c in pit_event_id):
        raise ValueError("Invalid pit event ID")
    key = sha256(f"bounds-v1:{start.isoformat()}:{end.isoformat()}".encode()).hexdigest()[:12]
    folder = root / "data/telemetry/pit_events" / pit_event_id.replace(":", "_") / key
    manifest = {"session_key": session_key, "driver_number": driver_number,
                "pit_event_id": pit_event_id, "start_utc": start.isoformat(),
                "end_utc": end.isoformat(), "endpoints": {},
                "quality": "APPROXIMATE_SAMPLES_NOT_VERIFIED_ROUTE_ANCHORS"}
    for endpoint in ("car_data", "location"):
        path = folder / f"{endpoint}.json"
        if not path.exists() or refresh:
            # OpenF1 reconstructs `key=value` before parsing inequalities. The
            # HTTP separator supplies '=': key 'date>' gives date>=timestamp.
            # A key 'date>=' would produce date>==timestamp and match no rows.
            params = {"session_key": session_key, "driver_number": driver_number,
                      "date>": start.isoformat(), "date<": end.isoformat()}
            content, url = client.fetch(endpoint, params)
            save_snapshot(path, content, url=url, params=params, refresh=refresh)
        records = read_snapshot(path)
        frame = pd.DataFrame(records)
        if not frame.empty:
            dates = pd.to_datetime(frame.date, utc=True, format="ISO8601")
            if not ((dates >= start) & (dates <= end)).all():
                raise ValueError("Provider returned telemetry outside the requested bounds")
            if (frame.driver_number != driver_number).any() or (frame.session_key != session_key).any():
                raise ValueError("Provider returned another driver's trace")
            frame.to_parquet(folder / f"{endpoint}.parquet", index=False)
        manifest["endpoints"][endpoint] = {
            "records": len(records), "sha256": sha256(path.read_bytes()).hexdigest(),
        }
        if not frame.empty:
            ordered = pd.to_datetime(frame.date, utc=True, format="ISO8601").sort_values()
            gaps = ordered.diff().dt.total_seconds().dropna()
            manifest["endpoints"][endpoint]["median_spacing_s"] = float(gaps.median()) if len(gaps) else None
            manifest["endpoints"][endpoint]["max_gap_s"] = float(gaps.max()) if len(gaps) else None
            manifest["endpoints"][endpoint]["first_utc"] = ordered.iloc[0].isoformat()
            manifest["endpoints"][endpoint]["last_utc"] = ordered.iloc[-1].isoformat()
    manifest["data_status"] = "SAMPLED_UNVERIFIED_ANCHORS" if all(
        m["records"] for m in manifest["endpoints"].values()) else "MISSING_SAMPLES"
    manifest["snapshot_directory"] = str(folder.relative_to(root))
    (folder / "audit.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def audit_representative_pit_telemetry(root: Path, *, base_url: str = "https://api.openf1.org/v1") -> list[dict]:
    """Two bounded stops at each of five contrasting circuits; opt-in only."""
    results = []
    out = root/"artifacts/runs/stage4_pit_loss_v1/telemetry_audit.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with OpenF1Client(base_url) as client:
        for circuit, sid in (("Singapore", 9896), ("Monza", 9912), ("Sakhir", 10014),
                             ("Suzuka", 10006), ("Monte Carlo", 9979)):
            rows = client.cached_get(root, endpoint="pit", year=2025, session_key=sid)
            candidates = [r for r in rows if r.get("lane_duration") and r.get("stop_duration")
                          and 0 < r["stop_duration"] < 6]
            ordered = sorted(candidates, key=lambda r: (r["stop_duration"], r["date"], r["driver_number"]))
            chosen = [ordered[0], ordered[-1]] if len(ordered) > 1 else ordered
            for row in chosen:
                sequence = 1+sum(r["driver_number"] == row["driver_number"] and r["date"] < row["date"] for r in rows)
                event_id = f"{sid}:{row['driver_number']}:{sequence}"
                ts = utc(row["date"])
                summary = ingest_pit_telemetry(
                    root, client, session_key=sid, driver_number=row["driver_number"],
                    pit_event_id=event_id,
                    start_utc=ts-pd.Timedelta(seconds=min(row["lane_duration"]+15, 140)),
                    end_utc=ts+pd.Timedelta(seconds=20),
                )
                summary.update(circuit=circuit, provider_event_utc=ts.isoformat(),
                               lane_boundary_method="BACKWARDS_FROM_PROVIDER_REPORT_TIME_APPROXIMATE",
                               raw_lane_duration_s=row["lane_duration"],
                               raw_stop_duration_s=row["stop_duration"])
                results.append(summary)
                out.write_text(json.dumps(results, indent=2))
                print(f"Telemetry {circuit} {event_id}: {summary['data_status']}", flush=True)
    return results
