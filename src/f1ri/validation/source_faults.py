"""Deterministic, separate delivery transformations for acquisition research."""

from datetime import timedelta
from json import loads

from f1ri.features.receipt_field import reconstruct_received_field
from f1ri.ingestion.source_receipt import (
    CLOCK_ANOMALY,
    CLOCK_FIXTURE,
    EXPERIMENT,
    SourceReceiptEnvelope,
    canonical,
    digest,
    identity,
    utc,
)
from f1ri.ingestion.source_replay import ReplayIndex

BASE = utc("2025-05-18T14:06:27Z")
SUITE_ID = "s7-b-controlled-delivery"
ENDPOINTS = ("location", "position", "pit", "race_control", "laps")
ROSTER = (1, 4)


def _at(offset):
    return BASE+timedelta(seconds=offset)


def response(sequence, endpoint, start, delivery, rows=(), *, status=200, error=None, headers=None, clock=CLOCK_FIXTURE,
             process_lag=0.):
    raw = canonical(list(rows))
    stamps = [event for row in rows if (event := identity(9987, endpoint, row)[1])]
    env = SourceReceiptEnvelope(
        "openf1", endpoint, 9987, {"session_key": 9987}, SUITE_ID, sequence, sequence, 0,
        f"{SUITE_ID}:{sequence}", "deterministic-fixture-clock", _at(start), int((start+30)*1e9),
        _at(delivery), _at(delivery), int((delivery+30)*1e9), _at(delivery+process_lag),
        status, headers or {}, digest(raw), len(raw), len(rows) if not error else None,
        min(stamps, default=None), max(stamps, default=None), clock,
        "BODY_RECORDED" if not error else "SOURCE_UNAVAILABLE", error, "COMPLETE_CONTROLLED_QUERY", EXPERIMENT,
    )
    return env, raw


def _position(driver, offset, p=1):
    return {"session_key": 9987, "driver_number": driver, "date": _at(offset).isoformat(), "position": p}


def _sc(offset):
    return {"session_key": 9987, "date": _at(offset).isoformat(), "driver_number": None,
            "category": "SafetyCar", "scope": None, "message": "SAFETY CAR DEPLOYED"}


def _lap(offset, duration):
    return {"session_key": 9987, "driver_number": 1, "lap_number": 46,
            "date_start": _at(offset).isoformat(), "lap_duration": duration}


def _raw_locations(session):
    rows = []
    for driver in ROSTER:
        for record in session.fixes[driver].window(_at(-1), BASE):
            if not record.conflict:
                p = record.point
                rows.append({"session_key": 9987, "driver_number": driver, "date": p.at_utc.isoformat(),
                             "x": p.x, "y": p.y})
    return rows


def _replay(responses, offset, policy, *, event=None):
    at = _at(offset)
    index = ReplayIndex(SUITE_ID, 9987, ENDPOINTS, responses, mode=EXPERIMENT, roster=ROSTER)
    return loads(index.replay(at, event_cutoff=_at(offset if event is None else event), receipt_cutoff=at,
                              policy=policy)), index


def _values(snapshot, endpoint):
    return [r["payload"] for r in snapshot["observations"] if r["endpoint"] == endpoint]


def run_faults(session, policy, recovery):
    location = _raw_locations(session)
    early = response(1, "location", -.1, .1, location)
    sound = response(2, "race_control", .1, .15, [_sc(0)])
    pit_row = {"session_key": 9987, "date": _at(-.02).isoformat(), "driver_number": 1, "lap_number": 46}
    good_pit = response(3, "pit", .15, .2, [pit_row])
    field_responses = [early, sound, good_pit]
    result = {}
    demos = {}

    def check(key, label, passed, sample, schedule=()):
        if not passed:
            raise ValueError(f"Failed deterministic receipt fault: {key} {label}")
        result[key] = {"scenario": label, "passed": True, "mode": EXPERIMENT,
                       "observed": sample, "schedule": list(schedule)}

    def snapshots(schedule, *cutoffs):
        data = [_replay(schedule, t, policy) for t in cutoffs]
        return [s for s, _ in data], data[-1][1]

    def timing(schedule):
        return [{"request_id": e.request_id, "endpoint": e.endpoint, "start": e.request_utc,
                 "delivered": e.completed_utc, "usable": e.available_utc, "status": e.http_status,
                 "error": e.error, "body_sha256": e.body_sha256} for e, _ in schedule]

    delayed = [response(1, "position", 0, 4, [_position(1, -.2)], process_lag=.5)]
    states, _ = snapshots(delayed, 2, 4.2, 5)
    counts = [len(_values(s, "position")) for s in states]
    check("F01", "Delayed response and processing", counts == [0, 0, 1], counts, timing(delayed))

    late_old = response(1, "position", 0, 4, [_position(1, -1, 2)])
    early_fresh = response(2, "position", 1, 2, [_position(1, -1, 1)])
    states, out_of_order = snapshots([late_old, early_fresh], 3, 5)
    values = [_values(s, "position")[0]["position"] for s in states]
    check("F02", "Out-of-order response", values == [1, 1] and
          any(e["kind"] == "OUT_OF_ORDER_RESPONSE_SUPPRESSED" for e in out_of_order.audit), values,
          timing([late_old, early_fresh]))

    duplicate_schedule = [response(i, "position", i, i+.1, [_position(1, 0)]*2) for i in (1, 2)]
    states, index = snapshots(duplicate_schedule, 1.5, 3)
    first = [s["observations"][0]["first_seen_utc"] for s in states]
    check("F03", "Duplicate response and duplicate rows", len(index.versions) == 1 and first[0] == first[1],
          first, timing(duplicate_schedule))

    rev = [response(1, "laps", 1, 1.1, [_lap(-95, 94.)]), response(2, "laps", 3, 4, [_lap(-94.5, 94.5)])]
    states, index = snapshots(rev, 2, 5)
    values = [_values(s, "laps")[0]["lap_duration"] for s in states]
    check("F04", "Corrected lap duration and event timestamp", values == [94., 94.5] and len(index.versions) == 2,
          values, timing(rev))
    demos["correction"] = states

    silent, _ = snapshots([early], 8)
    check("F05", "Missed scheduled poll", silent[0]["endpoints"]["race_control"]["responses"] == 0,
          silent[0]["endpoints"]["race_control"])

    rate = response(1, "position", 1, 1.1, status=429, error="HTTP_429", headers={"retry-after": "5"})
    retry = response(2, "position", 6.2, 6.3, [_position(1, 6), _position(4, 6)])
    states, _ = snapshots([rate, retry], 2, 7)
    check("F06", "Rate limit and recovery", not _values(states[0], "position") and
          states[1]["endpoints"]["position"]["successful"] == 1, [s["endpoints"]["position"] for s in states],
          timing([rate, retry]))

    server = response(1, "position", 1, 1.2, status=503, error="HTTP_503")
    denied = response(1, "position", 1, 1.2, status=403, error="HTTP_403")
    for key, env, label in (("F07", server, "Retryable server error"), ("F08", denied, "Permanent rejection")):
        states, _ = snapshots([env], 2)
        values = states[0]["endpoints"]["position"]
        check(key, label, "SOURCE_OUTAGE" in values["status"] and values["successful"] == 0, values, timing([env]))

    gap = [early, response(2, "location", 1, 1.1, status=503, error="HTTP_503"),
           response(3, "location", 2, 2.1, status=None, error="ReadTimeout")]
    restored_rows = [row | {"date": _at(4).isoformat()} for row in location if row["date"] == max(
        v["date"] for v in location if v["driver_number"] == row["driver_number"])]
    gap.append(response(4, "location", 4, 4.1, restored_rows))
    states, _ = snapshots(gap, .5, 2.2, 4.2, 15)
    statuses = [s["endpoints"]["location"]["status"] for s in states]
    check("F09", "Short outage followed by recovery", "SOURCE_OUTAGE" in statuses[1] and "SOURCE_FRESH" in statuses[2],
          statuses, timing(gap))
    check("F10", "Long outage expires field input", "SOURCE_STALE" in statuses[3] and
          not states[3]["endpoints"]["location"]["field_input_allowed"], statuses[3], timing(gap))
    demos["outage"] = states

    partial = [response(1, "position", 0, .2, [_position(4, 0)])]
    states, _ = snapshots(partial, .3)
    ep = states[0]["endpoints"]["position"]
    check("F11", "Partial driver coverage", ep["missing_or_unverified_drivers"] == [1] and
          not ep["field_input_allowed"], ep, timing(partial))

    sc_delayed = field_responses[:1]+[response(2, "race_control", .1, 7, [_sc(0)])]
    states, _ = snapshots(sc_delayed, .8, 7.1)
    messages = [len(_values(s, "race_control")) for s in states]
    check("F12", "Race-control receipt delay", messages == [0, 1], messages, timing(sc_delayed))
    demos["delayed_sc"] = states

    pos_gap = [early, response(2, "race_control", 4, 4.2, [_sc(4)])]
    states, _ = snapshots(pos_gap, 4.3)
    check("F13", "Location stale while control is current", "SOURCE_STALE" in states[0]["endpoints"]["location"]["status"]
          and "SOURCE_FRESH" in states[0]["endpoints"]["race_control"]["status"], states[0]["endpoints"], timing(pos_gap))

    backfill = [response(1, "position", 0, .1, [_position(1, 0)]),
                response(2, "position", 1, 1.1, [_position(4, -10)])]
    states, index = snapshots(backfill, .5, 2)
    counts = [len(_values(s, "position")) for s in states]
    check("F14", "Late backfill", counts == [1, 2] and any(r["kind"] == "LATE_BACKFILL" for r in index.audit),
          counts, timing(backfill))

    odd = [response(1, "position", 0, .1, [_position(1, 1)]),
           response(2, "position", 1, 1.1, [_position(4, 0)], clock=CLOCK_ANOMALY)]
    states, index = snapshots(odd, 2)
    check("F15", "Inconsistent source and local clocks", not states[0]["observations"] and bool(index.unordered) and
          any(r["kind"] == "SOURCE_EVENT_AFTER_RECEIPT" for r in index.audit),
          {"unordered": index.unordered, "audit": index.audit}, timing(odd))

    check("F16", "Intention without committed response survives interruption", recovery["orphaned"] == 1 and
          recovery["before"] == 1, recovery)
    check("F17", "Resume preserves earlier committed evidence", recovery["after"] == 2 and recovery["stable_prefix"],
          recovery)

    controlled = [response(1, "position", 0, .1), response(2, "position", 1, 1.1, status=503, error="HTTP_503"),
                  response(3, "position", 2, 2.2, [_position(1, 1.5)])]
    states, index = snapshots(controlled, 3)
    window = states[0]["observations"][0]["appearance"]
    check("F18", "Appearance bounds leave provider publication unknown",
          window["bounded_both_sides"] and window["failed_polls_between"] == 1 and
          window["provider_publication_utc"] is None, window, timing(controlled))

    original, _ = _replay([early], .5, policy)
    appended, _ = _replay([early, *gap[1:]], .5, policy)
    if canonical(original) != canonical(appended):
        raise ValueError("Future response changed the canonical earlier prefix.")
    field_states, _ = snapshots(field_responses, .1, .3, 10)
    for name, state in zip(("missing_pit", "receipt_filtered_field", "stale_field"), field_states, strict=True):
        demos[name] = reconstruct_received_field(session, canonical(state))
    if not (demos["missing_pit"]["field"] is None and demos["stale_field"]["field"] is None and
            demos["receipt_filtered_field"]["field"] is not None):
        raise ValueError("Field receipt gating failed.")
    demos["prefix_stability"] = True
    demos["appearance"] = window
    demos["index_sample"] = {"response_schedule": timing(controlled), "snapshot": states[0]}
    demos["fixture_inputs"] = {
        "outage": [{"envelope": e.document(), "rows": loads(raw)} for e, raw in gap],
        "correction": [{"envelope": e.document(), "rows": loads(raw)} for e, raw in rev],
        "race_control": [{"envelope": e.document(), "rows": loads(raw)} for e, raw in sc_delayed],
    }
    return result, demos
