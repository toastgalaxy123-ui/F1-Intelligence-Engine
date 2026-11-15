"""Receipt/processing-time replay with stable first-seen and version history."""

from bisect import bisect_right
from collections import Counter, defaultdict
from functools import lru_cache
from json import loads

from f1ri.ingestion.source_receipt import (
    CLOCK_ANOMALY,
    CLOCK_FIXTURE,
    DRIVER_FEEDS,
    EXPERIMENT,
    OBSERVED,
    RETROSPECTIVE,
    canonical,
    digest,
    identity,
    parse_payload,
    utc,
)


class ReplayIndex:
    """Parse each response once and index effective state changes by receipt clock."""

    def __init__(self, capture_id, session, endpoints, responses, *, mode=OBSERVED, roster=()):
        if mode not in (OBSERVED, RETROSPECTIVE, EXPERIMENT):
            raise ValueError("An event-time archive cannot enter a receipt replay index.")
        self.capture_id = capture_id
        self.session = session
        self.endpoints = tuple(sorted(set(endpoints)))
        self.roster = tuple(sorted(set(roster)))
        self.mode = mode
        self.polls = defaultdict(list)
        self.history = defaultdict(list)
        self.versions = {}
        self.audit = []
        self.unordered = []
        self._compile(responses)
        self._cached_replay = lru_cache(maxsize=8)(self._replay_bytes)

    def _compile(self, responses):
        epochs, usable, identifiers = {}, [], set()
        sequences, unstable_epochs = set(), set()
        prior_epoch = None
        preceding_receipt = None
        for env, raw in sorted(responses, key=lambda r: r[0].sequence):
            if (env.request_id in identifiers or env.sequence in sequences or
                    env.capture_id != self.capture_id or env.session != self.session
                    or env.endpoint not in self.endpoints or env.mode != self.mode or env.body_sha256 != digest(raw)
                    or env.body_bytes != len(raw)):
                raise ValueError("Invalid receipt replay scope or raw body identity.")
            identifiers.add(env.request_id)
            sequences.add(env.sequence)
            previous = epochs.get(env.clock_epoch)
            unstable = env.clock_quality == CLOCK_ANOMALY or env.processed_utc < env.completed_utc
            if preceding_receipt and env.clock_epoch != prior_epoch and env.request_utc < preceding_receipt:
                unstable = True
            if previous:
                # Request sequence is an ordering anchor only within one host
                # monotonic epoch, never across capture process restarts.
                elapsed = (env.request_ns-previous.request_ns)/1e9
                wall = (env.request_utc-previous.request_utc).total_seconds()
                unstable |= elapsed < 0 or abs(elapsed-wall) > .25
            epochs[env.clock_epoch] = env
            if unstable:
                unstable_epochs.add(env.clock_epoch)
            unstable |= env.clock_epoch in unstable_epochs
            if unstable:
                self.unordered.append(env.request_id)
            else:
                usable.append((env, raw))
                preceding_receipt = max(preceding_receipt or env.available_utc, env.available_utc)
            prior_epoch = env.clock_epoch

        latest_event = {}
        latest_revision_request = {}
        for env, raw in sorted(usable, key=lambda row: (row[0].available_utc, row[0].sequence)):
            previous = self.polls[env.endpoint]
            keys = self._apply(env, raw, latest_event, latest_revision_request, previous) if not env.error else None
            previous.append((env.available_utc, env, keys))
        self._history_clocks = {key: [row[0] for row in history] for key, history in self.history.items()}
        self._poll_clocks = {key: [row[0] for row in polls] for key, polls in self.polls.items()}

    def _apply(self, env, raw, latest_event, latest_request, previous):
        if env.http_status != 200:
            raise ValueError("A non-success response cannot contain replayable observations.")
        groups = defaultdict(dict)
        original = parse_payload(raw)
        if len(original) != env.record_count:
            raise ValueError("Receipt record count differs.")
        for data in original:
            try:
                key, event = identity(env.session, env.endpoint, data)
            except ValueError:
                self._audit(env, "IDENTITY_UNRESOLVED", None)
                continue
            if env.clock_quality == CLOCK_FIXTURE and event and event > env.completed_utc:
                self._audit(env, "SOURCE_EVENT_AFTER_RECEIPT", key)
                continue
            version = digest(canonical(data))
            if version in groups[key]:
                self._audit(env, "DUPLICATE_ROW", key)
            groups[key][version] = (data, event)
        earlier_watermark = latest_event.get(env.endpoint)
        for key, candidates in sorted(groups.items()):
            history = self.history[key]
            old = history[-1][1] if history else None
            previous_row = self.versions.get((key, old))
            for version, (data, event) in sorted(candidates.items()):
                index_key = key, version
                if index_key in self.versions:
                    self._audit(env, "IDENTICAL_REDELIVERY", key)
                    continue
                changed = sorted(k for k in data.keys() | previous_row["payload"].keys()
                                 if data.get(k) != previous_row["payload"].get(k)) if previous_row else []
                self.versions[index_key] = {
                    "key": key, "version": version, "endpoint": env.endpoint, "payload": data,
                    "source_event_utc": event, "source_timestamp": data.get("date", data.get("date_start")),
                    "first_seen_utc": env.completed_utc, "processed_utc": env.processed_utc,
                    "usable_utc": env.available_utc, "body_sha256": env.body_sha256, "request_id": env.request_id,
                    "clock_quality": env.clock_quality, "provider_publication_utc": None,
                    "changed_fields": changed, "appearance": _appearance(env, key, previous) if not history else None,
                }
                if old:
                    self._audit(env, "NEW_VERSION", key)
                elif event and earlier_watermark and event < earlier_watermark:
                    self._audit(env, "LATE_BACKFILL", key)
            freshness = (env.request_utc, env.sequence)
            if key in latest_request and freshness < latest_request[key]:
                self._audit(env, "OUT_OF_ORDER_RESPONSE_SUPPRESSED", key)
                continue
            latest_request[key] = freshness
            if len(candidates) != 1:
                history.append((env.available_utc, None, "SOURCE_CONFLICT"))
                self._audit(env, "SOURCE_CONFLICT", key)
                continue
            version = next(iter(candidates))
            if version != old and any(entry[1] == version for entry in history):
                history.append((env.available_utc, None, "REVISION_PENDING"))
                self._audit(env, "VERSION_REVERSION_UNRESOLVED", key)
                continue
            if version != old:
                history.append((env.available_utc, version, "RECEIPT_OBSERVED"))
            event = candidates[version][1]
            if event and (env.endpoint not in latest_event or event > latest_event[env.endpoint]):
                latest_event[env.endpoint] = event
        if previous and _comparable(previous[-1][1], env):
            old_keys = previous[-1][2]
            if old_keys is not None:
                for key in sorted(old_keys-groups.keys()):
                    self._audit(env, "OMISSION_NOT_RETRACTION", key)
        return set(groups)

    def _audit(self, env, what, key):
        self.audit.append({"at": env.available_utc, "endpoint": env.endpoint, "request_id": env.request_id,
                           "key": key, "kind": what})

    def replay(self, decision_utc, *, event_cutoff, receipt_cutoff, policy):
        decision, event, receipt = map(utc, (decision_utc, event_cutoff, receipt_cutoff))
        if not decision or not event or not receipt or receipt > decision or event > decision:
            raise ValueError("Event and receipt cutoffs must be at or before the decision clock.")
        return self._cached_replay(decision, event, receipt, tuple(sorted(policy.items())))

    def _replay_bytes(self, decision, event, receipt, rules):
        policy = dict(rules)
        observations = []
        by_endpoint = defaultdict(list)
        conflicts = defaultdict(int)
        for key, changes in sorted(self.history.items()):
            i = bisect_right(self._history_clocks[key], receipt)
            if not i:
                continue
            _, version, _ = changes[i-1]
            if not version:
                endpoint = parse_key_endpoint(key)
                conflicts[endpoint] += 1
                continue
            row = self.versions[key, version]
            if row["source_event_utc"] is None or row["source_event_utc"] <= event:
                observations.append(row)
                by_endpoint[row["endpoint"]].append(row)
        stats = {}
        received = []
        for ep in self.endpoints:
            end = bisect_right(self._poll_clocks.get(ep, ()), receipt)
            prefix = self.polls[ep][:end]
            received.extend((p[1].request_id, p[1].body_sha256, p[1].error) for p in prefix)
            stats[ep] = _freshness(ep, prefix, by_endpoint[ep], conflicts[ep], decision, event, policy, self.roster,
                                   self.mode)
        return canonical({"schema": "s7-b-replay-v1", "capture_id": self.capture_id, "session": self.session,
                          "mode": self.mode, "decision_utc": decision, "event_cutoff": event,
                          "receipt_cutoff": receipt, "observations": observations, "endpoints": stats,
                          "prefix_sha256": digest(canonical(sorted(received))), "publication_utc": None,
                          "strategy_support": "HISTORICAL_STRATEGY_UNSUPPORTED", "live": "LIVE_UNSUPPORTED"})


def parse_key_endpoint(key):
    return loads(key)[1]


def _comparable(a, b):
    return a.endpoint == b.endpoint and a.parameters == b.parameters and not a.error and b.http_status == 200


def _appearance(env, key, previous):
    absence = None
    check_complete = False
    failed_checks = 0
    for _, poll, keys in reversed(previous):
        if poll.parameters != env.parameters:
            continue
        if keys is None:
            failed_checks += 1
            continue
        if key in keys:
            break
        if poll.coverage == env.coverage == "COMPLETE_CONTROLLED_QUERY" and not (
            set(poll.headers) | set(env.headers)
        ) & {"content-range", "link"} and poll.request_utc <= env.request_utc:
            absence = poll
            check_complete = True
            break
    return {"last_confirmed_absence_request_utc": absence.request_utc if absence else None,
            "absence_response_utc": absence.completed_utc if absence else None,
            "first_observed_receipt_utc": env.completed_utc, "failed_polls_between": failed_checks if absence else None,
            "polling_gap_s": (env.completed_utc-absence.request_utc).total_seconds() if absence else None,
            "bounded_both_sides": bool(absence), "query_completeness_verified": check_complete,
            "provider_publication_utc": None}


def _freshness(ep, prefix, rows, conflicts, decision, event, policy, roster, mode):
    successful = [p[1] for p in prefix if p[2] is not None]
    order = lambda env: (env.request_utc, env.sequence)
    last = max(successful, key=order, default=None)
    latest_request = max((p[1] for p in prefix), key=order, default=None)
    clocks = [r["source_event_utc"] for r in rows if r["source_event_utc"] is not None]
    source_time = max(clocks, default=None)
    response_age = (decision-last.completed_utc).total_seconds() if last else None
    comparable = bool(last and last.clock_quality == CLOCK_FIXTURE and mode == EXPERIMENT)
    event_age = (event-source_time).total_seconds() if source_time and comparable else None
    current = set()
    driver_ages = {}
    if ep in DRIVER_FEEDS:
        by_driver = defaultdict(list)
        for row in rows:
            if row["source_event_utc"] is not None:
                by_driver[row["payload"]["driver_number"]].append(row["source_event_utc"])
        for driver in sorted(set(roster) | by_driver.keys()):
            observed = max(by_driver.get(driver, ()), default=None)
            age = (event-observed).total_seconds() if observed and comparable else None
            driver_ages[driver] = age
            if age is not None and age <= policy.get(ep, 0):
                current.add(driver)
    missing = sorted(set(roster)-current) if ep in {"location", "position", "intervals", "car_data"} else []
    statuses = []
    if not last:
        statuses.append("SOURCE_OUTAGE" if prefix else "RECEIPT_UNVERIFIED")
    else:
        statuses.append("RECEIPT_OBSERVED")
        stale = response_age > policy["response_max_s"] or event_age is not None and event_age > policy.get(ep, 0)
        if stale:
            statuses.append("SOURCE_STALE")
        elif event_age is not None:
            statuses.append("SOURCE_FRESH")
        else:
            statuses.append("CLOCK_UNCERTAIN" if not comparable else "INSUFFICIENT_SUPPORT")
    if latest_request and latest_request.error:
        statuses.append("SOURCE_OUTAGE")
    if missing:
        statuses.append("SOURCE_PARTIAL")
    if conflicts:
        statuses.append("SOURCE_CONFLICT")
    return {"status": sorted(set(statuses)), "latest_request_utc": latest_request.request_utc if latest_request else None,
            "last_success_request_utc": last.request_utc if last else None,
            "last_success_request_age_s": (decision-last.request_utc).total_seconds() if last else None,
            "last_response_complete_utc": last.completed_utc if last else None, "response_age_s": response_age,
            "latest_source_event_utc": source_time, "event_age_s": event_age,
            "latest_first_seen_age_s": (decision-max(r["first_seen_utc"] for r in rows)).total_seconds() if rows else None,
            "driver_age_s": driver_ages, "missing_or_unverified_drivers": missing, "conflicts": conflicts,
            "responses": len(prefix), "successful": len(successful), "errors": len(prefix)-len(successful),
            "error_kinds": dict(Counter(p[1].error for p in prefix if p[1].error)),
            "clock_quality": last.clock_quality if last else None,
            "field_input_allowed": "SOURCE_FRESH" in statuses and not set(statuses) & {
                "SOURCE_OUTAGE", "SOURCE_PARTIAL", "SOURCE_CONFLICT", "SOURCE_STALE"}}
