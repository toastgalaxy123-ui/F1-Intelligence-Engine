"""Read-only, receipt-scoped shadow decision diagnostics and immutable seals."""

from dataclasses import asdict, dataclass
from json import loads

from f1ri.features.receipt_field import reconstruct_received_field
from f1ri.features.temporal_maturity import filter_receipt_snapshot
from f1ri.ingestion.source_capture import _write_new
from f1ri.ingestion.source_receipt import (
    EVENT_ONLY,
    EXPERIMENT,
    OBSERVED,
    RETROSPECTIVE,
    canonical,
    digest,
    utc,
)

MODEL_SHA = "f98e8444a5982dd482a29444ce2c06848a39829cc248cb6cfceb760b59eae16a"
REQUIRED_FEEDS = ("location", "pit", "race_control")


@dataclass(frozen=True)
class ShadowConfiguration:
    year: int | None
    expected_model_sha256: str = MODEL_SHA
    search_complete: bool = False
    unique_recommendation: bool = False


@dataclass(frozen=True)
class ShadowSnapshot:
    capture_id: str
    session: int
    decision_utc: str
    event_cutoff: str
    receipt_cutoff: str | None
    capture_mode: str
    collector_clock_quality: tuple
    source_prefix_sha256: str
    endpoint_support: dict
    versions: tuple
    maturity: dict
    physical: dict
    model: dict
    configuration: dict
    support: tuple
    withholding_reasons: tuple
    tyre_state: str = "UNAVAILABLE"
    inventory: None = None
    status: str = "SHADOW_DECISION_WITHHELD"
    action: None = None


def make_snapshot(state, configuration, *, observed_geometry=None):
    """Qualify source evidence and report independent downstream gates."""
    mode = state["mode"]
    if mode not in (EVENT_ONLY, RETROSPECTIVE, EXPERIMENT, OBSERVED):
        raise ValueError("Unrecognized receipt evidence mode.")
    cutoff, event, receipt = map(utc, (state["decision_utc"], state["event_cutoff"], state["receipt_cutoff"]))
    if not cutoff or not event or event > cutoff or (receipt is None) != (mode == EVENT_ONLY) or (
        receipt and receipt > cutoff
    ):
        raise ValueError("Invalid shadow decision clock.")
    if receipt and any(utc(v["usable_utc"]) > receipt or utc(v["first_seen_utc"]) > receipt
                       for v in state["observations"]):
        raise ValueError("Replay contains a version outside the receipt cutoff.")
    filtered = filter_receipt_snapshot(state) if receipt else state | {"observations": []}
    field = {"support": "PHYSICAL_STATE_UNSUPPORTED", "field": None, "withheld_feeds": REQUIRED_FEEDS}
    if observed_geometry is not None:
        field = reconstruct_received_field(observed_geometry, canonical(filtered))
    chart = field["field"] or {}
    endpoint = state["endpoints"]
    reasons = set()
    statuses = {status for row in endpoint.values() for status in row["status"]}
    if mode == EVENT_ONLY:
        reasons.add("ORIGINAL_RECEIPT_UNAVAILABLE")
    for ep in REQUIRED_FEEDS:
        source = endpoint.get(ep, {})
        if not source.get("field_input_allowed"):
            reasons.add(f"{ep.upper()}_INPUT_UNSUPPORTED")
            reasons.update(source.get("status", ()))
    if not chart or chart.get("supported_positions") != len(chart.get("field_roster", ())):
        reasons.add("PHYSICAL_STATE_UNSUPPORTED")
    # S7-A mapped order is not an independently certified S7-A.1 predecessor.
    reasons.add("PHYSICAL_PREDECESSOR_UNCERTIFIED")
    reasons.update(("PIT_GEOMETRY_UNSUPPORTED", "TYRE_INVENTORY_UNAVAILABLE"))
    era_supported = configuration.year is not None and 2023 <= configuration.year <= 2025
    identical = configuration.expected_model_sha256 == MODEL_SHA
    if not era_supported:
        reasons.add("MODEL_ERA_UNSUPPORTED")
    if not identical:
        reasons.add("MODEL_HASH_MISMATCH")
    if not configuration.search_complete:
        reasons.add("SEARCH_NOT_COMPLETE")
    if not configuration.unique_recommendation:
        reasons.add("NO_UNIQUE_RECOMMENDATION")
    counts = {}
    versions = []
    for row in filtered["observations"]:
        for maturity in row["maturity"].values():
            status = maturity["status"]
            counts[status] = counts.get(status, 0)+1
        versions.append({key: row[key] for key in ("key", "endpoint", "version", "body_sha256", "first_seen_utc",
                                                    "processed_utc", "usable_utc", "visible_payload_sha256",
                                                    "source_event_utc", "request_id")} | {"field_validity": row["maturity"]})
    if any(code in counts for code in ("MATURITY_UNVERIFIED", "COMPLETION_EVIDENCE_CONFLICT",
                                       "BOUNDARY_NOT_RECEIPT_VISIBLE", "EVENT_CLOCK_UNAVAILABLE")):
        statuses.add("TEMPORAL_VALIDITY_UNCERTAIN")
    if receipt:
        statuses.add("RECEIPT_REPLAYABLE")
    if receipt and mode == EXPERIMENT and all(endpoint.get(ep, {}).get("field_input_allowed") for ep in REQUIRED_FEEDS):
        statuses.add("SHADOW_SNAPSHOT_SUPPORTED")
    return ShadowSnapshot(
        state["capture_id"], state["session"], state["decision_utc"], state["event_cutoff"],
        state["receipt_cutoff"], mode, tuple(sorted({r["clock_quality"] for r in endpoint.values()
                                                     if r.get("clock_quality")})),
        state["prefix_sha256"], endpoint, tuple(versions), counts,
        {"support": field["support"], "localized": chart.get("supported_positions", 0),
         "mapped_predecessors": chart.get("supported_predecessors", 0),
         "certified_predecessors": 0, "merge_certified": False},
        {"supported_era": era_supported, "pinned_sha256": MODEL_SHA, "matches_pin": identical,
         "release": "MEAN_ONLY_VALIDATED", "g3": "FAILED"},
        asdict(configuration), tuple(sorted(statuses)), tuple(sorted(reasons)),
    )


def seal_decision(snapshot, directory=None):
    content = {"schema": "s7-c0-sealed-decision-v1", "snapshot": asdict(snapshot),
               "assumptions": "READ_ONLY_SHADOW_PREFLIGHT", "candidate_library": [],
               "candidate_evaluation": None, "live": "LIVE_UNSUPPORTED"}
    sealed = content | {"sha256": digest(canonical(content))}
    raw = canonical(sealed)
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        _write_new(directory/f"{sealed['sha256']}.json", raw)
    return sealed


def check_seal(raw):
    row = loads(raw)
    pinned = row.pop("sha256")
    if digest(canonical(row)) != pinned:
        raise ValueError("Shadow checkpoint seal mismatch.")
    return pinned
