"""Exact, support-aware arithmetic for a bounded policy-by-scenario matrix."""

from fractions import Fraction
from itertools import combinations

BEST_REALIZED = "BEST_REALIZED_POLICY"
PERFECT_INFORMATION = "PERFECT_INFORMATION_ORACLE"
OBJECTIVES = ("MINIMAX_REGRET", "WORST_CASE_RELATIVE_LOSS", "PARETO", "DECLARED_WEIGHTED_DELTA")


def _leaders(rows, metric):
    supported = [r for r in rows if r[metric] is not None]
    if not supported:
        return []
    best = min(r[metric] for r in supported)
    return sorted(r["policy"] for r in supported if r[metric] == best)


def analyze_matrix(times, oracle_times, *, complete, oracle_complete, comparator=BEST_REALIZED, weights=(),
                   reference="reference"):
    """Analyze integer microsecond times. Each input row represents one scenario."""
    scenarios = tuple(sorted(times))
    policies = sorted({p for values in times.values() for p in values})
    supplied = dict(weights)
    weighted = {k: Fraction(str(v)) for k, v in supplied.items()}
    if weights and (len(supplied) != len(weights) or supplied.keys() != set(scenarios)
                    or any(w < 0 for w in weighted.values()) or not sum(weighted.values())):
        raise ValueError("Supply one nonnegative weight per scenario with a positive total.")
    total_weight = sum(weighted.values())
    best = {s: min((t for t in values.values() if t is not None), default=None) for s, values in times.items()}
    deltas, regret, regret_oracle = {}, {}, {}
    rows, fully_supported = [], {}
    for p in policies:
        delta, realized, oracle, ranks = {}, {}, {}, {}
        for s in scenarios:
            value = times[s].get(p)
            ref = times[s].get(reference)
            delta[s] = value-ref if value is not None and ref is not None else None
            realized[s] = value-best[s] if complete and value is not None and best[s] is not None else None
            oracle[s] = value-oracle_times[s] if oracle_complete.get(s) and value is not None else None
            ranks[s] = (1+sum(t < value for t in times[s].values() if t is not None)) if value is not None else None
        deltas[p], regret[p], regret_oracle[p] = delta, realized, oracle
        current = realized if comparator == BEST_REALIZED else oracle
        comparable = all(v is not None for v in delta.values())
        regrets_complete = comparable and all(v is not None for v in current.values())
        if comparable:
            fully_supported[p] = tuple(delta.values())
        worst = max(current.values()) if regrets_complete else None
        group = [rank for rank in ranks.values() if rank is not None]
        rows.append({
            "policy": p, "comparable": comparable, "valid_scenarios": len(group), "ranks": ranks,
            "rank_range": [min(group), max(group)] if group else None,
            "worst_regret_us": worst, "minimum_regret_us": min(current.values()) if regrets_complete else None,
            "regret_range_us": worst-min(current.values()) if regrets_complete else None,
            "worst_perfect_information_regret_us": max(oracle.values())
            if comparable and all(v is not None for v in oracle.values()) else None,
            "worst_cases": [s for s in scenarios if worst is not None and current[s] == worst],
            "zero_regret_cases": [s for s in scenarios if current[s] == 0],
            "worst_delta_us": max(delta.values()) if comparable else None,
            "best_delta_us": min(delta.values()) if comparable else None,
            "worst_delta_cases": [s for s in scenarios if comparable and delta[s] == max(delta.values())],
            "weighted_delta_us": sum(weighted[s]*delta[s] for s in scenarios)/total_weight
            if weighted and comparable else None,
        })
    dominated = {p: [] for p in fully_supported}
    for a, b in combinations(fully_supported, 2):
        ab = tuple(zip(fully_supported[a], fully_supported[b], strict=True))
        if all(x <= y for x, y in ab) and any(x < y for x, y in ab):
            dominated[b].append(a)
        elif all(x >= y for x, y in ab) and any(x > y for x, y in ab):
            dominated[a].append(b)
    frontier = sorted(p for p, by in dominated.items() if not by)
    winners = {
        "MINIMAX_REGRET": _leaders(rows, "worst_regret_us") if complete else [],
        "WORST_CASE_RELATIVE_LOSS": _leaders(rows, "worst_delta_us") if complete else [],
        "PARETO": frontier if complete else [],
        "DECLARED_WEIGHTED_DELTA": _leaders(rows, "weighted_delta_us") if complete else [],
    }
    for row in rows:
        value = row["weighted_delta_us"]
        if value is not None:
            row["weighted_delta_us"] = float(value)
            row["weighted_delta_exact"] = str(value)
        row["dominated_by"] = sorted(dominated.get(row["policy"], ()))
    return {"rows": rows, "deltas": deltas, "regret": regret, "perfect_information_regret": regret_oracle,
            "realized_best_us": best, "winners": winners, "pareto": frontier, "dominance": dominated,
            "frontier_scope": "DECLARED_POLICY_SET" if complete else "EVALUATED_SUBSET_ONLY",
            "weights_mode": "USER_DECLARED_SCENARIO_WEIGHTS" if weights else "UNWEIGHTED_SCENARIOS"}
