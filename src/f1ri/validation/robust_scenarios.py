"""Predeclared S6-B study families and reusable dashboard fixtures."""

from dataclasses import replace

from f1ri.simulator.race_control import ControlCommand as C
from f1ri.simulator.strategy_robust import (
    FUTURE,
    INITIAL,
    PACE,
    PHYSICAL,
    RobustPaceSensitivity,
    ScenarioFamily,
    ScenarioVariant,
)
from f1ri.validation.neutralization_scenarios import controls
from f1ri.validation.optimization_scenarios import fixture_request, perturbations, scenario_requests


def variant(base, key, explanation, *, category=FUTURE, request=None, **updates):
    request = request or base
    scene = replace(request.scenario, scenario_id=key, **updates)
    return ScenarioVariant(key, category, explanation, "S6_B_DECLARED_SINGAPORE_CUTOFF", replace(
        request, request_id=key, scenario=scene))


def control_script(base, *, state, deployment, release):
    clock = base.scenario.initial.race_clock_us/1e6
    if state == "VSC":
        script = [(C.VSC, deployment), (C.VSC_ENDING, release-11), (C.GREEN, release)]
    elif state == "SC":
        script = [(C.SC, deployment), (C.SC_IN, release-12), (C.SC_WITHDRAWN, release-1), (C.GREEN, release)]
    else:
        raise ValueError("Use a declared VSC or SC.")
    return controls(base.scenario.initial, [(command, clock+at) for command, at in script])


def neutralization(base, *, vsc=(5, 89), sc=(5,), longer=True, restart=True, green=True,
                   release_offset=97, vsc_duration=75):
    rows = [variant(base, "green", "Continue under GREEN")] if green else []
    for i, delay in enumerate(vsc):
        key = ("early-vsc", "late-vsc")[i] if len(vsc) == 2 else f"vsc-{delay}"
        rows.append(variant(base, key, f"VSC deployment +{delay} s and green +{delay+vsc_duration} s",
                            controls=control_script(base, state="VSC", deployment=delay, release=delay+vsc_duration)))
    for delay in sc:
        key = "early-sc" if len(sc) == 1 else f"sc-{delay}"
        rows.append(variant(base, key, f"SC deployment +{delay} s, green +{delay+release_offset-5} s",
                            controls=control_script(base, state="SC", deployment=delay, release=delay+release_offset-5)))
    if sc and longer:
        delay = sc[0]
        rows.append(variant(base, "longer-sc", "Same SC deployment, green delayed 35 s",
                            controls=control_script(base, state="SC", deployment=delay, release=delay+release_offset+30)))
    if sc and restart:
        delay = sc[0]
        rows.append(variant(base, "restart-pace", "Identical SC clocks and 10% higher declared restart speed",
                    category=PHYSICAL, controls=control_script(base, state="SC", deployment=delay,
                                                               release=delay+release_offset-5),
                    profile=replace(base.scenario.profile, restart_speed_mps=base.scenario.profile.restart_speed_mps*1.1)))
    return tuple(rows)


def _study(root):
    fixtures = scenario_requests(root)
    scenes, _ = fixtures
    def make(name, **options):
        return fixture_request(root, name, fixtures=fixtures, **options)

    base = make("two-car")
    primary = ScenarioFamily("primary", base, neutralization(base))
    timing = tuple(variant(base, f"vsc-deploy-{offset}", f"VSC +{offset} s for 75 s",
                           controls=control_script(base, state="VSC", deployment=offset, release=offset+75))
                   for offset in (5, 28, 58, 89))
    timing += tuple(variant(base, f"sc-deploy-{offset}", f"SC +{offset} s for 92 s",
                            controls=control_script(base, state="SC", deployment=offset, release=offset+92))
                    for offset in (5, 28, 58))
    timing += tuple(variant(base, f"sc-release-{release}", f"SC +5 s with GREEN at +{release} s",
                            controls=control_script(base, state="SC", deployment=5, release=release))
                    for release in (66, 97, 132))
    timing += (variant(base, "vsc-pace", "VSC −10% pace ceiling", category=PHYSICAL,
                       controls=control_script(base, state="VSC", deployment=5, release=80),
                       profile=replace(base.scenario.profile, vsc_speed_mps=base.scenario.profile.vsc_speed_mps*.9)),)
    yield primary
    yield ScenarioFamily("neutralization-timing", base, timing)
    # Frozen S6-A GREEN entry for the lap-31 soft policy, relative to cutoff.
    boundary = 29_967_958
    yield ScenarioFamily("pit-entry-boundary", base, tuple(
        variant(base, tag, f"VSC at frozen GREEN pit-entry clock {change:+d} μs",
                controls=control_script(base, state="VSC", deployment=(boundary+change)/1e6, release=100))
        for tag, change in (("before", -1), ("boundary", 0), ("after", 1))))
    gap = tuple(variant(base, name, explanation, category=INITIAL, physical=scenes[key].physical)
                for name, explanation, key in (
                    ("close-gap", "Declared 24.6 m opening gap", "two-car"),
                    ("wider-gap", "Declared 689.8 m opening gap", "wider-gap")))
    yield ScenarioFamily("initial-gap", base, gap)
    traffic = [variant(base, "predecessor-stays", "Declared slower predecessor stays on track")]
    for key, label, attr in (("opponent-pits", "Declared predecessor stop", "opponents"),
                             ("retirement", "Declared predecessor retirement", "retirements"),
                             ("scripted-pass", "Authorized deterministic pass attempt", "passes")):
        traffic.append(variant(base, key, label, **{attr: getattr(scenes[key], attr)}))
    yield ScenarioFamily("future-traffic", base, tuple(traffic))
    clean = make("clean-air")
    yield ScenarioFamily("separate-clean-field", clean, (
        variant(clean, "clean-air", "Separate one-car field under GREEN"),
        variant(clean, "clean-early-vsc", "Early VSC from the same one-car cutoff",
                controls=control_script(clean, state="VSC", deployment=5, release=80)),
        variant(clean, "clean-late-vsc", "Frozen S6-A single-car late VSC",
                controls=scenes["late-vsc"].controls)))
    pace = make("wider-gap")
    varied = [variant(pace, "baseline-pace", "Released D8R1 mean")]
    labels = {"mean-pace-plus-0.3s": "All mean laps +0.3 s", "soft-plus-0.3s": "SOFT mean laps +0.3 s",
              "degradation-plus-0.06s-per-age-lap": "Age slope +0.06 s per lap of age"}
    for label, case_req in perturbations(pace):
        category = PACE if label in labels else PHYSICAL
        varied.append(variant(pace, label, labels.get(label, label), category=category, request=case_req))
    used = tuple(replace(r, tyres=tuple(replace(t, initial_age_laps=6) if t.set_id == "used-soft" else t
                                       for t in r.tyres)) for r in pace.scenario.resources)
    varied += [
        variant(pace, "new-stint-plus-0.3s", "Replacement forecasts +0.3 s per lap",
                category=PACE, request=replace(pace, sensitivity=RobustPaceSensitivity(new_stint_s=.3))),
        variant(pace, "older-used-soft", "Declared used-SOFT starting age 6 laps", category=PACE, resources=used),
        variant(pace, "faster-opponent", "Saved predecessor pace observations rather than slower synthetic history",
                category=PACE, request=replace(pace, features=make("original-opponent").features)),
    ]
    yield ScenarioFamily("pace-and-route-sensitivity", pace, tuple(varied))
    for h in (3, 5, 10):
        short = make("two-car", horizon=h, sets=("medium", "soft"), max_stops=2, budget=128)
        long = make("two-car", horizon=10, sets=("medium", "soft"), max_stops=2, budget=128)
        yield ScenarioFamily(f"horizon-{h}", short, neutralization(short, longer=False, restart=False),
                             library_reference=long)
    for objective, tag in (("TEAM_COMBINED", "team-combined"), ("LEAD_DRIVER", "team-lead"),
                           ("TEAM_WEIGHTED", "team-weighted")):
        team = make("shared-box", sets=("medium", "soft"), stop_options=2, max_stops=2, budget=128,
                    objective=objective)
        yield ScenarioFamily(tag, team, neutralization(team, vsc=(5,), longer=True, restart=False))
    yield replace(primary, family_id="budget-limited", total_budget=12)
    yield replace(primary, family_id="weighted-demonstration", objective="DECLARED_WEIGHTED_DELTA",
                  weights=tuple((v.scenario_id, i+1) for i, v in enumerate(primary.scenarios)))
