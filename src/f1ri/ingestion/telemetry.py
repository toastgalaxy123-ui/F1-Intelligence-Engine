"""Opt-in bounded OpenF1 telemetry with immutable raw cache provenance."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pandas as pd

from f1ri.ingestion.openf1 import OpenF1Client
from f1ri.ingestion.raw_cache import read_snapshot, save_snapshot


@dataclass(frozen=True, slots=True)
class TelemetryWindow:
    session_key: int
    driver_number: int | None
    start_utc: datetime
    end_utc: datetime

    def __post_init__(self) -> None:
        if (
            self.session_key <= 0 or (self.driver_number is not None and self.driver_number <= 0)
            or self.start_utc.utcoffset() is None or self.end_utc.utcoffset() is None
            or not 0 < (self.end_utc - self.start_utc).total_seconds() <= 180
        ):
            raise ValueError("Telemetry requires a session and a UTC window up to 180 seconds")

    def bounds(self) -> tuple[str, str]:
        return self.start_utc.astimezone(UTC).isoformat(), self.end_utc.astimezone(UTC).isoformat()


def load_telemetry_window(
    root: Path, window: TelemetryWindow, endpoint: str, *, client: OpenF1Client | None = None,
) -> tuple[pd.DataFrame, dict]:
    if endpoint not in {"location", "car_data"}:
        raise ValueError("Bounded telemetry supports location and car_data")
    start, end = window.bounds()
    key = sha256(f"{start}:{end}".encode()).hexdigest()[:16]
    field = "field" if window.driver_number is None else str(window.driver_number)
    path = (root / "data/telemetry/proximity" / str(window.session_key)
            / field / key / f"{endpoint}.json")
    if path.is_file():
        records = read_snapshot(path)
        fetched = None
    else:
        if client is None:
            raise FileNotFoundError(f"Missing bounded telemetry: {path}. Run with --ingest.")
        params = {"session_key": window.session_key, "date>": start, "date<": end}
        if window.driver_number is not None:
            params["driver_number"] = window.driver_number
        fetched = client.fetch(endpoint, params)
        records = json.loads(fetched[0])
        if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
            raise ValueError("Expected an OpenF1 array of telemetry records")
    fields = ["x", "y", "z"] if endpoint == "location" else ["speed"]
    frame = pd.DataFrame(records, columns=["session_key", "driver_number", "date", *fields])
    frame["date"] = pd.to_datetime(frame.date, utc=True, format="ISO8601", errors="raise")
    numbers = pd.to_numeric(frame.driver_number, errors="coerce")
    valid_driver = numbers.gt(0) & numbers.mod(1).eq(0)
    if window.driver_number is not None:
        valid_driver &= frame.driver_number.eq(window.driver_number)
    if not (
        frame.session_key.eq(window.session_key).all()
        and valid_driver.all()
        and frame.date.between(window.start_utc, window.end_utc).all()
    ):
        raise ValueError("Provider returned telemetry outside the declared identity or time bounds")
    if fetched is not None:
        content, url = fetched
        save_snapshot(path, content, url=url, params=params)
    frame["driver_number"] = numbers.astype(int)
    out_of_order = (frame.groupby("driver_number").date.diff().dt.total_seconds() < 0).sum()
    frame = frame.drop_duplicates().sort_values("date", kind="stable").reset_index(drop=True)
    conflicts = frame.duplicated(subset=["driver_number", "date"], keep=False)
    summary = {
        "endpoint": endpoint, "session_key": window.session_key, "driver_number": window.driver_number,
        "start_utc": start, "end_utc": end, "sha256": sha256(path.read_bytes()).hexdigest(),
        "cache_path": path.relative_to(root).as_posix(), "usable_rows": int((~conflicts).sum()),
        "conflicting_rows": int(conflicts.sum()),
    }
    if window.driver_number is None:
        summary |= {"received_rows": len(records), "duplicates": len(records) - len(frame),
                    "out_of_order_rows": int(out_of_order)}
    return frame.loc[~conflicts].reset_index(drop=True), summary
