"""Post-lock FIA evidence catalog; characterize fixed predictions without retuning."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime, time
from pathlib import Path

import pandas as pd

from f1ri.ingestion.fia_reference_archive import collect_stage4_fia_documents
from f1ri.ingestion.pit_lane_reference import load_pit_lane_reference
from f1ri.ingestion.pit_reference_documents import SPEED_RULE, verified_speed_reference
from f1ri.validation.pit_backtest import _json, _safe_json
from f1ri.validation.pit_stabilization import load_stabilization_config, verify_selection_lock
from f1ri.warehouse.schema import REFERENCE_SCHEMAS

DATE = re.compile(r"\bDate\s*:?\s+(\d{1,2}\s+[A-Za-z]+\s+20\d{2})")


def _date_evidence(text: str) -> tuple[str, str]:
    from dateutil.parser import parse
    match = DATE.search(text)
    if match is None:
        raise ValueError("No printed publication date in official document")
    return parse(match.group(1), dayfirst=True).date().isoformat(), match.group(1)


def _available(document: dict, text: str) -> datetime:
    printed, _ = _date_evidence(text)
    date = max(printed, document["archive_publication_date"])
    return datetime.combine(datetime.fromisoformat(date).date(), time.max, tzinfo=UTC)


def _cached_text(folder: Path, document: dict) -> str:
    pdf = folder/f"{document['source_sha256']}.pdf"
    if hashlib.sha256(pdf.read_bytes()).hexdigest() != document["source_sha256"]:
        raise ValueError("Official source PDF hash changed")
    return (folder/f"{document['source_sha256']}.txt").read_text()


def build_fia_facts(root: Path) -> tuple[pd.DataFrame, list[dict]]:
    run = root/"artifacts/runs/stage4_1_stabilization"
    folder = run/"reference_sources"
    catalog = json.loads((folder/"event_document_catalog.json").read_text())
    examples = pd.concat([pd.read_parquet(run/phase/"examples.parquet")
                          for phase in ("development", "reserved_tests", "supplementary_tests")], ignore_index=True)
    cutoffs = pd.to_datetime(examples.forecast_as_of_utc, utc=True).groupby(examples.session_key).min().to_dict()
    visual_path = root/"config/references/stage4_1_fia_visual_observations.json"
    visual = json.loads(visual_path.read_text()) if visual_path.exists() else []
    facts, rows = [], []
    for event in catalog["events"]:
        if "error" in event:
            raise ValueError(f"Incomplete archive collection: {event}")
        cutoff = cutoffs.get(event["session_key"], pd.Timestamp(event["race_start_utc"]))
        documents = event["downloaded_documents"]
        eligible, rejected = [], []
        for doc in documents:
            text = _cached_text(folder, doc)
            if doc["kind"] not in {"EVENT_NOTES", "PIT_SPEED_DECISION"} or not SPEED_RULE.search(text):
                continue
            try:
                printed, evidence = _date_evidence(text)
                metadata = {"source_url": doc["url"], "season": event["year"],
                            "circuit_short_name": event["circuit_short_name"],
                            "document_circuit_aliases": [event["event"].replace(" Grand Prix", "")],
                            "source_publication_date": printed, "publication_date_evidence": evidence,
                            "archive_publication_date": doc["archive_publication_date"],
                            "effective_to_utc": event["race_end_utc"]}
                row = verified_speed_reference(text, metadata, doc["source_sha256"])
                if pd.Timestamp(row["available_as_of_utc"]) > cutoff:
                    rejected.append({"source_url": doc["url"], "reason": "NOT_AVAILABLE_BEFORE_FIRST_FORECAST"})
                else:
                    eligible.append(row)
            except ValueError as exc:
                rejected.append({"source_url": doc["url"], "reason": str(exc)})
        # Visual observations are explicitly reviewed source-page facts, not OCR
        # or guesses from a previous season. The quoted image is retained locally.
        for observation in [v for v in visual if v["session_key"] == event["session_key"]]:
            source = next(d for d in documents if d["source_sha256"] == observation["source_sha256"])
            text = _cached_text(folder, source)
            available = _available(source, text)
            if available > cutoff:
                raise ValueError("Visual source was not available before this forecast")
            speed = float(observation["pit_speed_limit_kph"])
            if speed not in {60, 80}:
                raise ValueError("Unexpected visually reviewed speed sign")
            eligible.append({"reference_id": f"{event['year']}:{event['circuit_short_name']}:{source['source_sha256'][:16]}:visual",
                             "circuit_short_name": event["circuit_short_name"], "season": event["year"],
                             "layout_version": None, "pit_speed_limit_kph": speed,
                             "effective_from_utc": available.isoformat(), "available_as_of_utc": available.isoformat(),
                             "effective_to_utc": event["race_end_utc"], "source_url": source["url"],
                             "source_sha256": source["source_sha256"], "source_quote": observation["observation"],
                             "verification_status": "VERIFIED_OFFICIAL_MAP_SPEED_SIGN",
                             "mapping_confidence": "VISUALLY_REVIEWED_OFFICIAL_PIT_MAP",
                             "notes": f"Visual evidence on PDF page {observation['pdf_page']}; route length and provider timing boundaries remain unverified"})
        speeds = {r["pit_speed_limit_kph"] for r in eligible}
        if len(speeds) > 1:
            raise ValueError(f"Conflicting dated pit-speed sources require manual review: {event['session_key']}")
        speed_row = min(eligible, key=lambda r: r["available_as_of_utc"]) if eligible else None
        layouts = []
        for doc in [d for d in documents if d["kind"] == "LAYOUT_DOCUMENT"]:
            text = _cached_text(folder, doc)
            available = _available(doc, text)
            if available <= cutoff:
                layouts.append({**doc, "available_as_of_utc": available.isoformat(),
                                "document_identity_only": True, "physical_geometry_version_verified": False})
        changes = []
        for doc in [d for d in documents if d["kind"] == "EVENT_NOTES"]:
            text = _cached_text(folder, doc)
            if _available(doc, text) > cutoff:
                continue
            match = re.search(r"Changes to the Circuit(.*?)(?:\n\s*\d{1,2}[.)]\s|\Z)", text, re.IGNORECASE | re.DOTALL)
            if match:
                changes.append({"source_url": doc["url"], "source_sha256": doc["source_sha256"],
                                "quote": match.group(0).strip()[:2200]})
        fact = {"session_key": event["session_key"], "season": event["year"], "split": event["split"],
                "circuit_short_name": event["circuit_short_name"], "first_forecast_as_of_utc": cutoff.isoformat(),
                "speed_reference": speed_row, "layout_documents": layouts, "circuit_change_evidence": changes,
                "pit_lane_length_m": None, "provider_route_boundaries_verified": False,
                "source_rejections": rejected, "source_archive_url": event["index_url"],
                "all_downloaded_source_pdfs": documents}
        facts.append(fact)
        if speed_row:
            rows.append(speed_row)
    frame = pd.DataFrame(rows).reindex(columns=list(REFERENCE_SCHEMAS["pit_lane_reference"]))
    destination = root/"data/raw/reference/stage4_1_fia"
    destination.mkdir(parents=True, exist_ok=True)
    frame.to_csv(destination/"pit_lane_reference.csv", index=False, lineterminator="\n")
    load_pit_lane_reference(destination/"pit_lane_reference.csv")
    _json(destination/"catalog.json", _safe_json({"retrieved_at_utc": catalog["retrieved_at_utc"],
          "role": "POST_LOCK_REFERENCE_AUDIT_NOT_USED_TO_RESELECT_OR_REFIT", "events": facts}))
    return frame, facts


def run_fia_reference_audit(root: Path, *, fetch: bool = False) -> dict:
    cfg, path = load_stabilization_config(root)
    lock = verify_selection_lock(root, cfg, path)
    if fetch:
        collect_stage4_fia_documents(root)
    frame, facts = build_fia_facts(root)
    run = root/"artifacts/runs/stage4_1_stabilization"
    predictions = pd.read_parquet(run/"test_predictions.parquet")
    reviewed = pd.read_parquet(root/"artifacts/runs/stage4_pit_loss_v1/predictions.parquet")
    characterization = []
    for fact in [f for f in facts if f["split"] in {"test", "reviewed_diagnostic"}]:
        selected = predictions[(predictions.session_key == fact["session_key"]) & (predictions.model_id == "P1")]
        model = "P1"
        if fact["split"] == "reviewed_diagnostic":
            selected = reviewed[(reviewed.session_key == fact["session_key"]) & (reviewed.model_id == "P8")
                                & reviewed.eligible_primary_label]
            model = "P8 reviewed net cohort"
        lane = selected[selected.eligible_direct_lane]
        net = selected[selected.eligible_primary_label]
        speed = fact["speed_reference"]
        characterization.append({"session_key": fact["session_key"], "circuit": fact["circuit_short_name"], "fixed_model": model,
                                 "verified_speed_kph": speed["pit_speed_limit_kph"] if speed else None,
                                 "layout_document_available": bool(fact["layout_documents"]),
                                 "direct_lane_n": len(lane), "mean_measured_lane_s": lane.target_lane_duration_s.mean(),
                                 "fixed_model_lane_mae_s": (lane.target_lane_duration_s-lane.expected_lane_duration_s).abs().mean(),
                                 "eligible_net_n": len(net)})
    summary = {"retrieved_at_utc": datetime.now(UTC).isoformat(), "network_access": "CONFIRMED_FIA_HTTP_200",
               "events_catalogued": len(facts), "events_with_verified_speed": len(frame),
               "events_with_preforecast_layout_documents": sum(bool(f["layout_documents"]) for f in facts),
               "text_verified_speed_rows": int(frame.verification_status.eq("VERIFIED_OFFICIAL_SPEED_ONLY").sum()),
               "visual_map_sign_speed_rows": int(frame.verification_status.eq("VERIFIED_OFFICIAL_MAP_SPEED_SIGN").sum()),
               "selected_model_unchanged": lock["selected_model"], "models_refit": False,
               "physical_route_lengths_verified": 0, "relative_strategy_approval": False,
               "test_characterization": characterization}
    _json(run/"fia_reference_audit.json", _safe_json(summary))
    table = ["| Session | Circuit | Fixed model | Sourced km/h | Layout source | Lane n | Lane mean s | Lane MAE s | Net n |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in characterization:
        mean = "—" if pd.isna(row["mean_measured_lane_s"]) else f"{row['mean_measured_lane_s']:.3f}"
        mae = "—" if pd.isna(row["fixed_model_lane_mae_s"]) else f"{row['fixed_model_lane_mae_s']:.3f}"
        table.append(f"| {row['session_key']} | {row['circuit']} | {row['fixed_model']} | {row['verified_speed_kph'] or '—'} | {row['layout_document_available']} | {row['direct_lane_n']} | {mean} | {mae} | {row['eligible_net_n']} |")
    source_table = ["| Season | Session | Circuit | km/h | Evidence | Available UTC |", "| --- | --- | --- | --- | --- | --- |"]
    for fact in facts:
        source = fact["speed_reference"]
        if source:
            source_table.append(f"| {fact['season']} | {fact['session_key']} | {fact['circuit_short_name']} | {source['pit_speed_limit_kph']:g} | [FIA document]({source['source_url']}) — {source['verification_status']} | {source['available_as_of_utc']} |")
    text = f"""# Stage 4.1 — Official FIA circuit-reference audit

FIA access is active. {len(facts)} race weekends were checked; **{len(frame)}** have pre-forecast event-specific speed evidence and **{summary['events_with_preforecast_layout_documents']}** have dated circuit/pit-layout documents. Text evidence uses an explicit event-speed directive or stewards' statement. Visual speed signs are separately identified. Unknown speeds remain missing. No route length was estimated from an unscaled drawing.

This audit was added after the model selection and primary test results were frozen. The selected model remains **{lock['selected_model']}**. No model weights, calibration, eligibility or primary results were changed. New reference CSVs live in a separate subdirectory and do not enter the frozen study's input loader. They are available for a future preregistered feature study; this audit establishes no forecast improvement.

## Availability and provenance

Each source has its official URL, SHA-256, document date, archive publication date and exact text quote or identified visual observation. Availability is conservatively the later of the printed date and archive publication date, at end-of-day UTC. Each accepted source precedes that race's first saved forecast. Drawings have source-document identities, which are not claimed to be physical geometry version numbers. The catalog records stated circuit changes separately. Provider report timestamps still lack independently matched legal entry/exit timing boundaries.

## Fixed-model test characterization

{chr(10).join(table)}

Known pit speed alone does not supply distance, deceleration, acceleration, service or on-track counterfactual movement. Lusail's dated drawings depict an 80 sign; Montreal's pre-race stewards' decision explicitly sets 80 km/h, and Baku's dated drawing also depicts 80. These sources do not establish a slower Lusail speed restriction as the explanation for its roughly +4 s net bias. Route geometry and reference bias remain unresolved. Lane and net populations differ; the Lusail row uses the eight previously reviewed P8 net cases. This is descriptive evidence about fixed predictions, not another model selection exercise. The lack of usable Montreal/Silverstone net targets and independent VSC/SC references is unchanged.

## Source-verified speed references

{chr(10).join(source_table)}

The complete source catalog is `data/raw/reference/stage4_1_fia/catalog.json`; load the separate `pit_lane_reference.csv` explicitly for subsequent studies. Raw PDFs and extracted text are retained in the ignored run's `reference_sources` directory. Reproduce collection and audit with `python -m f1ri.validation.pit_fia_reference_audit --fetch`. Stage 5 remains untouched.
"""
    (root/"artifacts/reports/stage4_1_fia_reference_audit.md").write_text(text)
    verify_selection_lock(root, cfg, path)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true")
    args = parser.parse_args()
    print(run_fia_reference_audit(Path.cwd(), fetch=args.fetch))
