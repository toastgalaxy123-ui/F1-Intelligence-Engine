"""Immutable-by-default source snapshots and lightweight provenance metadata."""
from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path


def snapshot_path(root: Path, year: int, endpoint: str, session_key: int | None) -> Path:
    base = root / "data" / "raw" / "openf1" / f"year={year}"
    return (base / f"session_key={session_key}" if session_key is not None
            else base / "catalog") / f"{endpoint}.json"


def read_snapshot(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ValueError(f"Expected an OpenF1 JSON array of objects in {path}")
    return data


def save_snapshot(
    path: Path, raw_bytes: bytes, *, url: str, params: dict, refresh: bool = False
) -> None:
    if path.exists() and not refresh:
        raise FileExistsError(f"Snapshot already exists: {path} (use --refresh explicitly)")
    content = json.loads(raw_bytes)
    if not isinstance(content, list) or any(not isinstance(row, dict) for row in content):
        raise ValueError(f"Unexpected OpenF1 response for {url}: JSON root must be an array")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Preserve old bytes on an explicit refresh. This permits faithful reruns of
    # analyses that used historical versions of a subsequently corrected feed.
    if path.exists() and refresh and path.read_bytes() != raw_bytes:
        prior_hash = sha256(path.read_bytes()).hexdigest()[:16]
        history = path.parent / "history"
        history.mkdir(exist_ok=True)
        backup = history / f"{path.stem}-{prior_hash}.json"
        if not backup.exists():
            shutil.copy2(path, backup)
            prior_meta = path.with_suffix(".meta.json")
            if prior_meta.exists():
                shutil.copy2(prior_meta, history / f"{path.stem}-{prior_hash}.meta.json")
    # Atomic writes prevent half-written JSON if the process is interrupted.
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(raw_bytes)
    os.replace(tmp, path)
    manifest = {
        "source": "openf1",
        "url": url,
        "query": params,
        "retrieved_at_utc": datetime.now(UTC).isoformat(),
        "sha256": sha256(raw_bytes).hexdigest(),
        "records": len(content),
        "notes": "Source snapshot; do not manually edit. --refresh is explicit replacement.",
    }
    meta_tmp = path.with_suffix(".meta.tmp")
    meta_tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.replace(meta_tmp, path.with_suffix(".meta.json"))
