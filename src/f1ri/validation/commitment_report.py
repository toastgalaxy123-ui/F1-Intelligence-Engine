"""Figures and text from the S6-B.1 execution ledgers."""
from matplotlib import pyplot as plt

from f1ri.simulator.strategy_commitment import FIXED, ORIGINAL, REPLAN, REVALIDATE, SCHEDULED
from f1ri.validation.optimization_report import seal

BLUE, ORANGE, GREEN = "#246687", "#bd7439", "#438170"
LABELS = {FIXED: "Fixed lap 31", ORIGINAL: "P0 original", REVALIDATE: "P2 stay out",
          REPLAN: "P2 replan", SCHEDULED: "P5 scheduled"}


def _s(value):
    return value/1e6 if value is not None else None


def _text(ax, value):
    ax.text(.03, .94, value, transform=ax.transAxes, va="top", fontsize=8, linespacing=1.4)
    ax.set_xticks([])
    ax.set_yticks([])


def _bars(ax, rows, field, policies):
    indexed = {r["policy"]: r for r in rows}
    ids = [p for p in policies if indexed[p][field] is not None]
    vals = [_s(indexed[p][field]) for p in ids]
    ax.barh([LABELS.get(p, p) for p in ids], vals, color=BLUE)
    ax.scatter(vals, [LABELS.get(p, p) for p in ids], s=11, color=BLUE)
    ax.set_xlabel("Seconds")


def _lines(ax, family, case_ids, xs, field, policies):
    for p in policies:
        ax.plot(xs, [_s(family["outcomes"][k][p][field]) for k in case_ids], "o-", label=LABELS.get(p, p))
    ax.set_ylabel("Seconds")
    ax.legend(fontsize=6)


def figures(out, base, families, transitions, metrics, contract):
    titles = ("Frozen failure", "Decision and entry timeline", "SC deployment versus entry", "GREEN vs commitment",
              "Elapsed to marker", "Regret by SC deployment", "Regret by duration", "VSC timing boundary",
              "Pit states", "Cancel or continue", "Rank across families", "Primary and stress worst regrets",
              "Exact and near-equal outcomes", "Following-constrained GREEN", "Traffic controls",
              "Receding decision", "Horizon sensitivity", "Search coverage", "Verification", "Recommendation support")
    fig, grid = plt.subplots(5, 4, figsize=(22, 22), layout="constrained")
    a = grid.flatten()
    for ax, title in zip(a, titles, strict=True):
        ax.set_title(title, loc="left", fontweight="bold")
        ax.grid(alpha=.16)
    bs = base["outcomes"]
    orig, fixed = (bs[k]["pit"][0] for k in (ORIGINAL, FIXED))
    clock = fixed["decision_us"]
    stress = families["B-stress"]
    focus = stress["outcomes"]["sc-deploy-58"]
    policies = (FIXED, ORIGINAL, REVALIDATE, REPLAN, SCHEDULED)
    a[0].plot([r["checkpoint"] for r in base["marker_gap"]],
              [_s(r["adaptive_minus_fixed_us"]) for r in base["marker_gap"]], "o-", color=BLUE)
    a[0].set(xlabel="Common checkpoint", ylabel="Original minus fixed (s)")
    commands = [e for e in bs[ORIGINAL]["control"] if e["outcome"] == "COMMITTED"]
    green = next(e["event"]["at_us"] for e in commands if e["event"]["command"] == "GREEN_RELEASE")
    for y, p in enumerate((FIXED, ORIGINAL)):
        visit = bs[p]["pit"][0]
        keys = ("decision_us", "entry_us", "service_start_us", "exit_us", "merge_us")
        a[1].plot([_s(visit[k]-clock) for k in keys], [y]*len(keys), "o-", color=(BLUE, ORANGE)[y])
    a[1].set(yticks=(0, 1), yticklabels=(LABELS[FIXED], LABELS[ORIGINAL]), xlabel="Seconds after cutoff")
    for e in commands:
        a[1].axvline(_s(e["event"]["at_us"]-clock), color=GREEN, alpha=.45)
    timing = families["C-sc-boundary"]
    offsets = sorted((int(k.split("-")[-1]), k) for k in timing["outcomes"])
    for p in (FIXED, ORIGINAL):
        a[2].plot([_s(x) for x,_ in offsets], [_s(r[0]["entry_us"]-clock) if (
            r := timing["outcomes"][k][p]["visited_pits"]) else None for _,k in offsets], "o-", label=LABELS[p])
    a[2].set(xlabel="Deployment offset (s)", ylabel="Entry offset (s)")
    a[2].legend(fontsize=7)
    exact = families["C-exact-event-order"]
    for flag, color in (("sc",BLUE), ("vsc",ORANGE)):
        a[3].plot((-1,0,1),[len(exact["outcomes"][f"{flag}-release-{k}"][REVALIDATE]["visited_pits"])
                             for k in ("before","equal","after")], "o-", label=flag.upper(), color=color)
    a[3].set(xlabel="GREEN minus pinned entry (µs)", ylabel="P2 completed stops", xticks=[-1,0,1])
    a[3].legend(fontsize=7)
    a[4].barh([LABELS[p] for p in policies], [_s(focus[p]["objective_us"]) for p in policies], color=BLUE)
    a[4].set_xlabel("Elapsed seconds")
    for ax, name, series, prefix in ((a[5],"C-sc-boundary",offsets,"SC"),
                             (a[7],"C-vsc-boundary",sorted((int(k.split("-")[-1]),k)
                                for k in families["C-vsc-boundary"]["outcomes"]),"VSC")):
        _lines(ax, families[name], [k for _,k in series], [_s(x) for x,_ in series], "regret_us", policies[:3])
        ax.set_xlabel(f"{prefix} deployment offset (s)")
    duration = families["C-duration"]
    ds = sorted((int(k.split("-")[-1]),k) for k in duration["outcomes"] if k.startswith("sc-duration-"))
    _lines(a[6], duration, [k for _,k in ds], [x for x,_ in ds], "regret_us", policies[:3])
    a[6].set_xlabel("SC duration (s)")
    seq = bs[ORIGINAL]["pit_events"]
    a[8].scatter([_s(r["at_us"]-clock) for r in seq], range(len(seq)), color=ORANGE)
    a[8].set(yticks=range(len(seq)), yticklabels=[r["status"].replace("_"," ").title() for r in seq],
             xlabel="Original stop seconds after cutoff")
    a[8].tick_params(axis="y", labelsize=7)
    a[9].barh([LABELS[p] for p in policies], [_s(focus[p]["delta_us"]) for p in policies],color=BLUE)
    a[9].set_xlabel("Seconds vs stay out")
    keys=("A-primary","B-stress","C-sc-boundary","D-clean-air","E-wider-gap-stress")
    for p in (FIXED, ORIGINAL, REVALIDATE, SCHEDULED):
        a[10].plot(range(len(keys)), [_s(next(r["worst_regret_us"] for r in families[k]["rankings"] if r["policy"]==p))
                                      for k in keys], "o-", label=LABELS[p])
    a[10].set(xticks=range(len(keys)),xticklabels=keys,ylabel="Worst regret (s)")
    a[10].tick_params(axis="x",rotation=24,labelsize=7)
    a[10].legend(fontsize=6)
    _bars(a[11], stress["rankings"],"worst_regret_us",policies)
    primary = families["A-primary"]
    _bars(a[12], [r for r in primary["rankings"] if r["policy"] in {*primary["selected"], *primary["near"]}],
          "worst_regret_us", (*primary["selected"], *primary["near"])[:8])
    a[12].tick_params(axis="y",labelsize=6)
    a[13].barh([LABELS[p] for p in policies],[_s(primary["outcomes"]["green"][p]["following_us"]) for p in policies],color=BLUE)
    a[13].set_xlabel("Following beyond other bounds (s)")
    keys=("A-primary","D-clean-air","D-wider-gap","D-original-opponent")
    for p in (ORIGINAL,FIXED,REVALIDATE):
        a[14].plot(range(len(keys)),[_s(families[k]["outcomes"]["green"][p]["delta_us"]) for k in keys],
                   "o-",label=LABELS[p])
    a[14].set(xticks=range(len(keys)),xticklabels=keys,ylabel="GREEN vs stay out (s)")
    a[14].tick_params(axis="x",rotation=24,labelsize=7)
    a[14].legend(fontsize=6)
    r = focus[REPLAN]["visited_pits"][0]
    _text(a[15],f"Observed SC: +{_s(orig['decision_us']-clock):.6f} s\n"
          f"Observed GREEN and canceled: +{_s(green-clock):.6f} s\n"
          f"New plan lap {r['target_lap']}: +{_s(focus[REPLAN]['committed_actions'][-1]['at_us']-clock):.6f} s\n"
          f"Entry: +{_s(r['entry_us']-clock):.6f} s")
    for p in (FIXED,ORIGINAL,REVALIDATE):
        a[16].plot((3,5,10),[_s(next(r["worst_regret_us"] for r in families[f"H{h}"]["rankings"] if r["policy"]==p))
                             for h in (3,5,10)],"o-",label=LABELS[p])
    a[16].set(xlabel="Horizon (laps)", ylabel="Worst regret (s)")
    a[16].legend(fontsize=7)
    a[17].barh(keys,[families[k]["search"]["supported_cells"] for k in keys],color=BLUE)
    a[17].set_xlabel("Supported cells")
    _text(a[18],f"Frozen regret: {_s(metrics['original_regret_us']):.6f} s\n"
          f"Physical invariant failures: {len(metrics['invariant_failures'])}\n"
          f"Observed boundary brackets: {len(transitions)}\n"
          f"Timing probes: {metrics['boundary_probes']}")
    _text(a[19],"\n".join(contract["statuses"]))
    (out/"diagnostics").mkdir()
    fig.savefig(out/"diagnostics/timing_commitment.png",dpi=120)
    plt.close(fig)


def finish_report(out, baseline, summaries, transitions, metrics, contract):
    figures(out, baseline, summaries, transitions, metrics, contract)
    raw=baseline["outcomes"]
    original, fixed = (raw[k]["pit"][0] for k in (ORIGINAL,FIXED))
    cutoff=fixed["decision_us"]
    green=next(r["event"]["at_us"] for r in raw[ORIGINAL]["control"]
               if r["event"]["command"]=="GREEN_RELEASE" and r["outcome"]=="COMMITTED")
    stress=summaries["B-stress"]
    fail=stress["outcomes"]["sc-deploy-58"]
    lines=[
        "# F1 Race Intelligence Engine — Stage 6 S6-B.1", "## Timing boundaries and pit commitment", "",
        f"**`{metrics['decision']}`**", "",
        ("The 46.754185 s failure reproduced exactly. It is expected behavior for the frozen commitment rule. "
         "The policy observed the Safety Car before requesting lap 32, but the stop stayed pending after GREEN. "
         "The fixed lap-31 stop entered before deployment. No scheduling, geometry or forecast-support defect "
         "was needed to explain the original result."), "",
        "## Replay at microsecond precision","",
        "| Event | Fixed lap 31 (s from cutoff) | Original adaptive (s from cutoff) |","| --- | ---: | ---: |"]
    for label,k in (("Decision","decision_us"),("Entry / irreversible route transition","entry_us"),("Service starts","service_start_us"),
                    ("Tyre fitted / service completes","service_end_us"),("Pit exit","exit_us"),("Rejoin","merge_us")):
        lines.append(f"| {label} | {_s(fixed[k]-cutoff):.6f} | {_s(original[k]-cutoff):.6f} |")
    lines.extend(["",
        (f"SC committed at +58.000000 s and GREEN at +{_s(green-cutoff):.6f} s. "
         f"The original entered {_s(original['entry_us']-green):.6f} s after GREEN. "
         "Both pit routes took 57.000000 s with no queue wait. They entered from GREEN and the same soft set "
         "was fitted. The common checkpoint table tracks where their timing paths separated."),
        "",
        "| Matched checkpoint | Adaptive minus fixed (s) |","| ---: | ---: |"])
    for row in baseline["marker_gap"]:
        lines.append(f"| {row['checkpoint']} | {_s(row['adaptive_minus_fixed_us']):+.6f} |")
    lines.extend(["","## Alternatives and physical commitment","",
        ("The scheduling layer owns the pit route. An instruction stays revisable while it is pending and the "
         "car is strictly before the geometric line. The SC and VSC fixtures verified cancellation one "
         "microsecond before entry and preserved entry at equality and one microsecond after. "
         "Race control commits before dynamic entry at a shared clock. The geometric guard still refuses revision "
         "at equality. The VSC-ending signal leaves the pending instruction in place until GREEN commits."),"",
        "| Policy | Delta in sc-deploy-58 (s) | Worst regret on expanded stress family (s) |",
        "| --- | ---: | ---: |"])
    for r in stress["rankings"]:
        p=r["policy"]
        if p in (FIXED,ORIGINAL,REVALIDATE,REPLAN,SCHEDULED):
            lines.append(f"| {LABELS[p]} | {_s(fail[p]['delta_us']):+.6f} | {_s(r['worst_regret_us']):.6f} |")
    lines.extend(["",
        ("The stay-out variant discarded the missed stop under GREEN. It avoided the original +5.262026 s "
         "loss against the stay-out reference but could not recover the earlier lap-31 opportunity. "
         "The one-revision demonstration canceled lap 32 and later executed a new lap-33 instruction. "
         "Replanning incurred more regret in this case. The original and active-at-decision control use the same "
         "live trigger and provide an intentional behavior check."),"",
        "## Declared family and boundary coverage","",
        "| Family | Winners or tied policies | Supported / expected cells |","| --- | --- | ---: |"])
    for k,v in summaries.items():
        s=v["search"]
        lines.append(f"| {k} | {', '.join(v['selected']) or 'Withheld'} | {s['supported_cells']}/{s['expected_policy_scenario']} |")
    lines.extend(["",
        ("The original 37-policy comparator was replayed separately for the primary and stress families. "
         "Their expanded 42-policy comparator admits the five new variants. Its oracle also includes "
         "the S6-A schedule-informed review clocks. Other families record their own candidate counts. "
         "Regret is calculated within each version. Narrowed intervals bracket changes observed at tested "
         "endpoints and do not establish continuity or exhaustive timing coverage."),
        "", "| Flag | Refined deployment bracket (s) | Width (s) |",
        "| --- | --- | ---: |"])
    for r in transitions:
        lo,hi=r["observed_interval_us"]
        lines.append(f"| {r['flag']} | {_s(lo):.6f} to {_s(hi):.6f} | {_s(r['width_us']):.6f} |")
    lines.extend(["","## Recommendation support","",", ".join(f"`{s}`" for s in contract["statuses"])+".","",
        ("The primary following fixture can tie materially different tyre actions. The clean-air, gap and "
         "opponent controls preserve that distinction. Horizons have separate common markers and regret "
         "benchmarks. Only two archived, cutoff-valid model contexts support the declared multi-car field. "
         "No independent pit-closure or historical announcement-latency model was used."),
        "",
        ("S6-C can use the timing-risk contract and show recommendation scope, while withholding a unique action "
         "where the family or support diagnostics require it. This stage keeps the D8R1 mean-only release and "
         "assigns no neutralization probabilities."),
        "",
        "`f1ri stage6-s6-b1 --output artifacts/runs/stage6_s6_b1_reproduced` recreates the study.",""])
    (out/"validation_report.md").write_text("\n".join(lines),encoding="utf-8",newline="\n")
    seal(out)
