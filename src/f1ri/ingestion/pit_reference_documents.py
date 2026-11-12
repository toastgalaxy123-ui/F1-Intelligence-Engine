"""Source-checked FIA event-note imports; never guess historical pit speed limits."""
from __future__ import annotations

import hashlib
import re
import subprocess
from datetime import UTC, datetime, time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pandas as pd

from f1ri.warehouse.schema import REFERENCE_SCHEMAS

OFFICIAL_HOSTS = {"fia.com", "www.fia.com"}
SPEED_RULE = re.compile(r"pit[\s-]*lane\s+speed[\s-]*limit.{0,100}?(\d{2,3})\s*(?:km/h|kph|kmh)", re.IGNORECASE | re.DOTALL)


def verified_speed_reference(text: str, metadata: dict, source_sha256: str) -> dict:
    url = urlsplit(metadata["source_url"])
    if url.scheme != "https" or url.hostname not in OFFICIAL_HOSTS or url.username or url.password:
        raise ValueError("Pit references require an official public HTTPS FIA source")
    circuit, season = metadata["circuit_short_name"], int(metadata["season"])
    aliases = metadata.get("document_circuit_aliases", [circuit])
    if not any(str(a).casefold() in text.casefold() for a in aliases) or str(season) not in text:
        raise ValueError("Document does not establish the requested event identity")
    quote = metadata.get("speed_limit_quote")
    if quote is not None and quote not in text:
        raise ValueError("Speed-limit quote is absent from the source document")
    matches = list(SPEED_RULE.finditer(quote or text))
    speeds = {int(m.group(1)) for m in matches}
    if len(speeds) != 1 or not 0 < next(iter(speeds), 0) <= 120:
        raise ValueError("Missing or ambiguous event-specific pit-lane speed limit")
    published = datetime.fromisoformat(metadata["source_publication_date"]).date()
    date_evidence = metadata["publication_date_evidence"]
    if date_evidence not in text:
        raise ValueError("Publication-date evidence is absent from the document")
    from dateutil.parser import parse
    if parse(date_evidence, dayfirst=True).date() != published:
        raise ValueError("Publication date differs from its source evidence")
    # Without an independently verified publication time/timezone, availability
    # is the END of that date, never an invented start-of-day publication time.
    available = datetime.combine(published, time.max, tzinfo=UTC)
    if metadata.get("archive_publication_date"):
        # A printed document date does not prove the document was public then.
        # An observed archive publication date can only move availability later.
        archive_date = datetime.fromisoformat(metadata["archive_publication_date"]).date()
        available = max(available, datetime.combine(archive_date, time.max, tzinfo=UTC))
    effective_to = pd.Timestamp(metadata["effective_to_utc"])
    if effective_to.tzinfo is None or effective_to <= available:
        raise ValueError("Reference effective period must follow publication")
    return {
        "reference_id": f"{season}:{circuit}:{source_sha256[:16]}",
        "circuit_short_name": circuit, "season": season,
        "layout_version": metadata.get("verified_layout_version"),
        "pit_speed_limit_kph": float(next(iter(speeds))),
        "effective_from_utc": available.isoformat(), "effective_to_utc": effective_to.isoformat(),
        "available_as_of_utc": available.isoformat(), "source_url": metadata["source_url"],
        "source_sha256": source_sha256, "source_quote": matches[0].group(0),
        "verification_status": "VERIFIED_OFFICIAL_SPEED_ONLY",
        "mapping_confidence": "VERIFIED_SPEED_LAYOUT_NOT_INFERRED",
        "notes": "Official event-specific speed directive; legal route anchors and lane length remain unverified",
    }


def import_fia_pit_reference(root: Path, metadata: dict, *, catalog_path: Path | None = None) -> dict:
    parsed = urlsplit(metadata["source_url"])
    if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_HOSTS:
        raise ValueError("Expected official FIA HTTPS document")
    # Inherited proxy and TLS trust remain enabled; no network-policy workarounds.
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        response = client.get(metadata["source_url"])
        response.raise_for_status()
    content = response.content
    if not content.startswith(b"%PDF") or len(content) > 10*1024*1024:
        raise ValueError("Expected a bounded official PDF document")
    sha = hashlib.sha256(content).hexdigest()
    folder = root/"artifacts/runs/stage4_1_stabilization/reference_sources"
    folder.mkdir(parents=True, exist_ok=True)
    pdf, extracted = folder/f"{sha}.pdf", folder/f"{sha}.txt"
    if not pdf.exists():
        pdf.write_bytes(content)
    subprocess.run(["pdftotext", "-layout", str(pdf), str(extracted)], check=True, capture_output=True)
    row = verified_speed_reference(extracted.read_text(), metadata, sha)
    path = catalog_path or root/"data/raw/reference/pit_lane_reference_verified.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = pd.read_csv(path) if path.exists() else pd.DataFrame()
    if not existing.empty and row["reference_id"] in set(existing.reference_id):
        return row
    frame = pd.concat([existing, pd.DataFrame([row])], ignore_index=True).reindex(columns=REFERENCE_SCHEMAS["pit_lane_reference"])
    frame.to_csv(path, index=False, lineterminator="\n")
    return row
