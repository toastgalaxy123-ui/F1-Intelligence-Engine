"""Deterministic one-at-a-time endpoint scheduling under explicit limits."""

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class EndpointRule:
    weight: int
    period_s: float
    max_gap_s: float
    budget: int
    dependency: bool

    def __post_init__(self):
        if (type(self.weight) is not int or not 1 <= self.weight <= 10 or
                type(self.budget) is not int or not 1 <= self.budget <= 100 or
                not all(isfinite(x) for x in (self.period_s, self.max_gap_s)) or
                not 1 <= self.period_s <= self.max_gap_s <= 300):
            raise ValueError("Invalid bounded endpoint priority rule.")


class PriorityScheduler:
    """A shared monotonic scheduler for the collector and controlled experiments."""

    def __init__(self, endpoints, spec, interval_s, *, max_retries=1):
        self.endpoints = tuple(endpoints)
        self.method = spec["method"]
        if self.method not in {"S0", "S1", "S2", "S3", "S4"} or set(spec["rules"]) != set(endpoints):
            raise ValueError("The scheduler must cover exactly the approved endpoints.")
        if not 2.1 <= interval_s <= 60:
            raise ValueError("Global request spacing must be at least 2.1 seconds.")
        self.rules = {ep: EndpointRule(**row) for ep, row in spec["rules"].items()}
        self.interval = interval_s
        self.max_retries = max_retries
        self.next_global = 0.
        self.ready = dict.fromkeys(endpoints, 0.)
        self.counts = dict.fromkeys(endpoints, 0)
        self.last_started = {}
        self.last_usable = {}
        self.disabled = set()
        self.retry = {}
        self.failures = dict.fromkeys(endpoints, 0)
        self.rotation = 0
        self.credit = dict.fromkeys(endpoints, 0)
        self.last_block = None

    def choose(self, now, blockers=()):
        pool = [ep for ep in self.endpoints if ep not in self.disabled and self.counts[ep] < self.rules[ep].budget]
        if not pool:
            return None, None
        ready = max(self.next_global, min(self.ready[ep] for ep in pool))
        if now < ready:
            return None, ready
        eligible = [ep for ep in pool if self.ready[ep] <= now]
        if self.method == "S0":
            cycle = self.endpoints[self.rotation:]+self.endpoints[:self.rotation]
            return next(ep for ep in cycle if ep in eligible), now
        overdue = [ep for ep in eligible if now-self.last_started.get(ep, 0.) >= self.rules[ep].max_gap_s]
        if overdue:
            self.last_block = "STARVATION_GUARD"
            return max(overdue, key=lambda ep: now-self.last_started.get(ep, 0.)), now
        unpolled = [ep for ep in eligible if ep not in self.last_started]
        if unpolled:
            self.last_block = "INITIAL_COVERAGE"
            return unpolled[0], now
        if self.method == "S1":
            self.last_block = "WEIGHTED_ROTATION"
            scores = {ep: self.credit[ep]+self.rules[ep].weight for ep in eligible}
            return max(eligible, key=scores.__getitem__), now
        blocker_set = set(blockers)
        if self.method in ("S3", "S4") and not blocker_set:
            blocker_set = {ep for ep in eligible if ep not in self.last_usable or
                           now-self.last_usable[ep] >= self.rules[ep].period_s}
        self.last_block = "DEPENDENCY_DEADLINE" if self.method in ("S3", "S4") else "DEADLINE"

        def rank(ep):
            priority = self.method in ("S3", "S4") and ep in blocker_set and self.rules[ep].dependency
            deadline = self.last_usable.get(ep, self.last_started[ep])+self.rules[ep].period_s
            return not priority, deadline

        return min(eligible, key=rank), now

    def started(self, endpoint, now):
        if self.method == "S1":
            eligible = [ep for ep in self.endpoints if self.ready[ep] <= now and ep not in self.disabled
                        and self.counts[ep] < self.rules[ep].budget]
            for ep in eligible:
                self.credit[ep] += self.rules[ep].weight
            self.credit[endpoint] -= sum(self.rules[ep].weight for ep in eligible)
        self.last_started[endpoint] = now
        self.next_global = max(self.next_global, now+self.interval)
        self.counts[endpoint] += 1
        self.rotation = (self.endpoints.index(endpoint)+1) % len(self.endpoints)

    def completed(self, endpoint, usable, success, *, retry_after=None, status=None, poll=None, retry=0):
        if success:
            self.failures[endpoint] = 0
            self.retry.pop(endpoint, None)
            self.last_usable[endpoint] = usable
            self.ready[endpoint] = usable
        elif status == 429:
            # No independent quota evidence permits scoping Retry-After to
            # only one feed. The conservative cooldown applies globally.
            wait = retry_after if retry_after is not None else 60.
            self.next_global = max(self.next_global, usable+wait)
            self.ready[endpoint] = max(self.ready[endpoint], usable+wait)
        elif status in (400, 401, 403, 404, 422):
            self.disabled.add(endpoint)
        else:
            wait = retry_after if retry_after is not None else self.interval
            if self.method == "S4":
                self.failures[endpoint] += 1
                wait = max(wait, min(16., self.interval*2**self.failures[endpoint]))
            self.ready[endpoint] = max(self.ready[endpoint], usable+wait)
        if not success and endpoint not in self.disabled and retry_after is not None and retry < self.max_retries:
            self.retry[endpoint] = (poll, retry+1)
        elif not success:
            self.retry.pop(endpoint, None)
