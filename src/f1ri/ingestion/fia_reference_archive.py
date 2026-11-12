"""Official event-document discovery and immutable PDF caching for reference audits."""
from __future__ import annotations

import hashlib
import html
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import httpx
import yaml

from f1ri.ingestion.raw_cache import read_snapshot, snapshot_path

SEASONS = {2023: "season-2023-2042", 2024: "season-2024-2043", 2025: "season-2025-2071"}
ARCHIVE = "https://www.fia.com/documents/championships/fia-formula-one-world-championship-14"
EVENT_ALIASES = {"Mexico Grand Prix": "Mexico City Grand Prix", "Brazil Grand Prix": "São Paulo Grand Prix",
                 "Zandvoort Grand Prix": "Dutch Grand Prix", "Montreal Grand Prix": "Canadian Grand Prix",
                 "Silverstone Grand Prix": "British Grand Prix", "Baku Grand Prix": "Azerbaijan Grand Prix"}


def parse_archive_documents(body: str, index_url: str, race_start: datetime) -> list[dict]:
    documents = []
    for item in re.findall(r'<li class="document-row\b.*?</li>', body, re.DOTALL):
        link = re.search(r'<a[^>]+href="([^"]+)"', item)
        title = re.search(r'<div class="title">(.*?)</div>', item, re.DOTALL)
        published = re.search(r'class="date-display-single">(.*?)</span>', item, re.DOTALL)
        if not (link and title and published):
            continue
        title = html.unescape(re.sub('<[^>]+>', '', title.group(1))).strip()
        stamp = datetime.strptime(published.group(1), "%d.%m.%y %H:%M").replace(tzinfo=UTC)
        # Treat the literal archive date conservatively as end-of-day UTC in
        # the verifier. This initial filter is not an assertion of availability.
        if stamp >= race_start:
            continue
        if re.search(r"race director.*event notes?", title, re.IGNORECASE):
            kind = "EVENT_NOTES"
        elif re.search(r"circuit map|pit lane drawing", title, re.IGNORECASE):
            kind = "LAYOUT_DOCUMENT"
        elif re.search(r"pit lane.*speed|speed.*pit lane", title, re.IGNORECASE):
            kind = "PIT_SPEED_DECISION"
        else:
            continue
        url = str(httpx.URL(urljoin(index_url, html.unescape(link.group(1)))))
        if urlsplit(url).hostname != "www.fia.com" or urlsplit(url).scheme != "https":
            raise ValueError("Archive document leaves the official HTTPS FIA host")
        documents.append({"title": title, "url": url, "archive_published_literal": published.group(1)+" CET",
                          "archive_publication_date": stamp.date().isoformat(), "kind": kind})
    return documents


def select_archive_sources(documents: list[dict]) -> list[dict]:
    selected = []
    for kind in ("EVENT_NOTES", "LAYOUT_DOCUMENT", "PIT_SPEED_DECISION"):
        group = sorted([d for d in documents if d["kind"] == kind], key=lambda d: datetime.strptime(
            d["archive_published_literal"][:-4], "%d.%m.%y %H:%M"))
        selected.extend(group[:1] if kind == "PIT_SPEED_DECISION" else group[:1]+group[-1:])
    return list({doc["url"]: doc for doc in selected}.values())


def cache_official_pdf(client: httpx.Client, folder: Path, document: dict) -> dict:
    response = client.get(document["url"])
    response.raise_for_status()
    content = response.content
    if not content.startswith(b"%PDF") or len(content) > 10*1024*1024:
        raise ValueError("Expected a bounded official PDF")
    sha = hashlib.sha256(content).hexdigest()
    pdf, txt = folder/f"{sha}.pdf", folder/f"{sha}.txt"
    if not pdf.exists():
        pdf.write_bytes(content)
    if not txt.exists():
        subprocess.run(["pdftotext", "-layout", str(pdf), str(txt)], check=True, capture_output=True)
    return {**document, "source_sha256": sha}


def collect_stage4_fia_documents(root: Path) -> dict:
    cfg = yaml.safe_load((root/"config/experiments/stage4_1_stabilization.yaml").read_text())
    entries = [{**e, "split": s} for s in ("train", "val", "test") for e in cfg["cohort"][s]]
    entries += [{"year": 2025, "session_key": 9850, "label": "2025 Qatar GP", "split": "reviewed_diagnostic"},
                {"year": 2025, "session_key": 9939, "label": "2025 Belgian GP", "split": "supplementary_test"}]
    folder = root/"artifacts/runs/stage4_1_stabilization/reference_sources"
    folder.mkdir(parents=True, exist_ok=True)
    catalog = []
    with httpx.Client(timeout=35, follow_redirects=False) as client:
        for entry in entries:
            info = read_snapshot(snapshot_path(root, entry["year"], "sessions", entry["session_key"]))[0]
            event = re.sub(r"^\d{4} ", "", entry["label"]).replace(" GP", " Grand Prix")
            event = EVENT_ALIASES.get(event, event)
            url = ARCHIVE+"/season/"+SEASONS[entry["year"]]+"/event/"+quote(event, safe="")
            response = client.get(url)
            response.raise_for_status()
            body = response.text
            index_sha = hashlib.sha256(response.content).hexdigest()
            (folder/f"{index_sha}.html").write_bytes(response.content)
            documents = parse_archive_documents(body, url, datetime.fromisoformat(info["date_start"]))
            downloaded = [cache_official_pdf(client, folder, d) for d in select_archive_sources(documents)]
            catalog.append({**entry, "event": event, "circuit_short_name": info["circuit_short_name"],
                            "race_start_utc": info["date_start"], "race_end_utc": info["date_end"],
                            "index_url": url, "index_sha256": index_sha,
                            "all_relevant_documents": documents, "downloaded_documents": downloaded})
            print(f"FIA source audit {entry['session_key']}: {len(downloaded)} PDFs", flush=True)
            result = {"retrieved_at_utc": datetime.now(UTC).isoformat(), "events": catalog}
            (folder/"event_document_catalog.json").write_text(json.dumps(result, indent=2)+"\n")
    return result
