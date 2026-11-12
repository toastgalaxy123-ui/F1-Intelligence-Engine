"""Plan and assemble bounded source slices for longer S3-C development replays."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

import pandas as pd
import yaml

from f1ri.ingestion.telemetry import TelemetryWindow
from f1ri.validation.traffic_acquisition import ReplaySpan, TrafficWindow, acquire_window


@dataclass(frozen=True, slots=True)
class ExtendedWindow:
    replay: TrafficWindow
    anchor: str
    reused: tuple[TelemetryWindow, ...]
    requested: tuple[TelemetryWindow, ...]


def plan_extensions(path: Path, protocol_hash: str, cohort: dict, originals, track, geometry_available):
    raw = path.read_text(encoding="utf-8")
    plan = yaml.safe_load(raw)
    key = plan["session_key"]
    if (plan["frozen_protocol_sha256"] != protocol_hash or cohort["session_key"] != key
            or cohort["split"] != plan["split"] or plan["split"] != "development"):
        raise ValueError("Coverage pilot requires its frozen protocol and a development race")
    prefix = timedelta(seconds=plan["prefix_seconds"])
    suffix = timedelta(seconds=plan["suffix_seconds"])
    pad = timedelta(seconds=plan["boundary_overlap_seconds"])
    logical_chunk = timedelta(seconds=plan["maximum_chunk_seconds"]) - 2 * pad
    if min(prefix, suffix, logical_chunk) <= timedelta(0) or pad < timedelta(0):
        raise ValueError("Coverage pilot needs positive extension and request spans")
    green = [(row.start_utc, row.end_utc) for row in track.itertuples()
             if row.status == "GREEN" and row.confidence == "HIGH"]
    extensions = []
    for stratum in plan["strata_priority"]:
        for window in sorted(originals, key=lambda item: item.telemetry.start_utc):
            if window.kind != stratum:
                continue
            start, end = window.telemetry.start_utc - prefix, window.telemetry.end_utc + suffix
            if start < geometry_available or not any(a <= start < end <= b for a, b in green):
                continue
            if any(start < item.replay.telemetry.end_utc and end > item.replay.telemetry.start_utc
                   for item in extensions):
                continue
            span = ReplaySpan(key, start, end)
            reused = tuple(w.telemetry for w in originals
                           if w.telemetry.start_utc < end and w.telemetry.end_utc > start)
            requests = uncovered_requests(span, reused, logical_chunk, pad)
            extensions.append(ExtendedWindow(TrafficWindow(stratum, span, window.seed_driver, window.seed_lap),
                                             window.name, reused, requests))
            break
    if [item.anchor for item in extensions] != plan["anchors"]:
        raise ValueError("Selected coverage pilot anchors differ from the frozen plan")
    budget_seconds = sum((chunk.end_utc - chunk.start_utc).total_seconds()
                         for item in extensions for chunk in item.requested)
    requests = sum(len(item.requested) for item in extensions) * 2
    if budget_seconds > plan["maximum_new_source_seconds"] or requests > plan["maximum_new_requests"]:
        raise ValueError("Coverage pilot exceeds its frozen acquisition budget")
    return extensions, plan | {"plan_sha256": sha256(raw.encode()).hexdigest(),
                               "new_request_window_seconds": budget_seconds, "bounded_requests": requests}


def uncovered_requests(span: ReplaySpan, cached, chunk: timedelta, pad: timedelta):
    """Reuse existing slices and pad new requests only at internal joins."""
    gaps = []
    cursor = span.start_utc
    for window in sorted(cached, key=lambda item: item.start_utc):
        start, end = max(span.start_utc, window.start_utc), min(span.end_utc, window.end_utc)
        if start >= end:
            continue
        if cursor < start:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < span.end_utc:
        gaps.append((cursor, span.end_utc))
    requests = []
    for start, end in gaps:
        while start < end:
            edge = min(end, start + chunk)
            requests.append(TelemetryWindow(span.session_key, None, max(span.start_utc, start - pad),
                                            min(span.end_utc, edge + pad)))
            start = edge
    return tuple(requests)


def merge_source_frames(frames, span: ReplaySpan) -> tuple[pd.DataFrame, dict]:
    combined = pd.concat(frames, ignore_index=True)
    combined = combined[combined.date.between(span.start_utc, span.end_utc)]
    exact = combined.drop_duplicates()
    conflict = exact.duplicated(["driver_number", "date"], keep=False)
    rows = exact.loc[~conflict].sort_values("date", kind="stable").reset_index(drop=True)
    return rows, {"rows_in_span": len(combined), "duplicate_rows_removed": len(combined) - len(exact),
                  "conflicting_rows_excluded": int(conflict.sum()), "usable_rows": len(rows)}


def acquire_extension(root, extension, endpoints, drivers, client):
    groups = {endpoint: [] for endpoint in endpoints}
    sources = []
    for windows, label in ((extension.reused, "REUSED_BASELINE"), (extension.requested, "EXTENSION")):
        for window in windows:
            frames, provenance = acquire_window(root, window, endpoints, drivers,
                                                None if label == "REUSED_BASELINE" else client)
            sources.extend(source | {"slice_role": label} for source in provenance)
            for endpoint, frame in frames.items():
                groups[endpoint].append(frame)
    merged, counts = {}, {}
    for endpoint, frames in groups.items():
        merged[endpoint], counts[endpoint] = merge_source_frames(frames, extension.replay.telemetry)
    return merged, {"anchor": extension.anchor, "sources": sources, "merge": counts}
