"""S5-C.1 contracts. Stubbed forecasts exercise wiring without impersonating D8."""
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import f1ri.simulator.strategy_forecast as forecasts
from f1ri.models.contracts import StintForecastRequest
from f1ri.simulator.strategy import StrategyInputError, compare_strategies
from f1ri.simulator.strategy_constraints import CheckpointConstraints
from f1ri.simulator.strategy_pace import PaceMode
from f1ri.validation.strategy_discrimination import cases, discriminate, evaluate

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def engineering():
    return cases(4909.)


@pytest.mark.parametrize(("family", "lo", "hi"), [
    ("clean-air", 20_666_665, 20_666_667),
    ("green", 0, 0),
    ("unblocked", 14_305_941, 14_305_943),
])
def test_clean_difference_and_constraint_removal(engineering, family, lo, hi):
    s, ref, _ = engineering[family]
    r = evaluate(s, ref, ["pit-now-hard", "pit-now-medium"])
    pair = discriminate(r, s, "pit-now-medium", "pit-now-hard")["drivers"][0]
    gap = pair["right_marker"]["clock_us"] - pair["left_marker"]["clock_us"]
    assert lo <= gap <= hi and pair["different_latent_checkpoints"] == 10
    if family == "green":
        assert len(pair["following_equal_crossing_checkpoints"]) == 9
        assert pair["left_summary"]["following_traversals"] == 10
    if family == "clean-air":
        assert pair["left_summary"]["following_traversals"] == 0
    for run in r["results"].values():
        assert all(row["category"] != "UNSUPPORTED" for row in run["checkpoint_constraints"])


def test_attribution_has_no_effect_on_base_execution_and_multi_car(engineering):
    s, ref, _ = engineering["multi-car"]
    normal = compare_strategies(s, ref)
    audited = evaluate(s, ref)
    assert normal["pairs"] == audited["pairs"]
    for name, run in audited["results"].items():
        assert run["trajectory"] == normal["results"][name]["trajectory"]
        assert len({r["driver"] for r in run["trajectory"]}) == 6
        assert all(row["event"] != "ON_TRACK_PASS" for row in run["checkpoint_constraints"])
    assert any(row["category"] == "SERVICE_WAIT" for row in audited["results"]["stack"]["checkpoint_constraints"])


def _features(s):
    c = s.initial.cars[0]
    start = s.physical.as_of_utc
    request = StintForecastRequest(
        s.initial.session_key, c.driver_number, c.completed_laps, c.completed_laps+1, 10,
        c.tyres.compound, c.tyres.age_laps, start,
        scheduled_race_laps=s.initial.ruleset.scheduled_laps, season=2025, circuit_short_name="Imola", circuit_key=1)
    return forecasts.ForecastInput(request, tuple((k, start) for k in ("identity", "timing", "tyres")), "a"*64)


class _StubBatch:
    def __init__(self, request):
        means = {"HARD": 90., "MEDIUM": 76., "SOFT": 78.}
        values = np.asarray([means[request.compound_relative]+.2*k for k in range(request.horizon_laps)])
        draw = np.random.default_rng(request.random_seed).normal(0, .1, size=(request.n_trajectory_samples, 1))
        self.lap_samples_s = values[None, :] + draw
        self.forecast = _forecast_object(request, values)
        self.artifact_sha256 = "b"*64
        self.random_stream_id = str(request.random_seed)


def _forecast_object(request, values):
    from dataclasses import dataclass

    @dataclass
    class Row:
        expected_lap_time_s: float

    @dataclass
    class Forecast:
        driver_number: int
        tyre_age_at_start: int
        support_status: str
        planned_start_lap: int
        lap_forecasts: tuple

    return Forecast(request.driver_number, request.tyre_age_at_start, "SUPPORTED", request.planned_start_lap,
                    tuple(Row(float(x)) for x in values))


def test_actual_fit_forecasts_fresh_used_delayed_and_distinct_stints(engineering, monkeypatch):
    s, ref, _ = engineering["clean-air"]
    resources = tuple(replace(r, tyres=tuple(replace(t, compound_absolute={"MEDIUM": "C3", "HARD": "C2", "SOFT": "C4"}
                                                   [t.compound]) for t in r.tyres)) for r in s.resources)
    s = replace(s, resources=resources)
    monkeypatch.setattr(forecasts, "forecast_pace", lambda _, req: _StubBatch(req))
    monkeypatch.setattr(forecasts, "forecast_new_stint", lambda _, req: _StubBatch(req))
    adapter = forecasts.StrategyForecastAdapter(SimpleNamespace(artifact_sha256="b"*64), (_features(s),),
                                                seed=7, samples=12, sample=2)
    result = compare_strategies(s, ref, forecast_adapter=adapter, recorder_factory=CheckpointConstraints)
    again = compare_strategies(replace(s, policies=tuple(reversed(s.policies))), ref,
                               ["pit-now-medium", "two-stop"], forecast_adapter=adapter,
                               recorder_factory=CheckpointConstraints)
    assert result["pace_mode"] == PaceMode.FULL_MODEL
    assert result["results"]["two-stop"] == again["results"]["two-stop"]
    assert result["results"]["pit-now-medium"] == again["results"]["pit-now-medium"]
    stops = result["results"]["two-stop"]["forecast_requests"][1:]
    assert [row["request"]["tyre_age_at_start"] for row in stops] == [0, 2]
    assert [row["request"]["compound_relative"] for row in stops] == ["MEDIUM", "SOFT"]
    assert len({r["stream_id"] for r in stops}) == 2
    hard = result["results"]["pit-now-hard"]["forecast_requests"][1]
    assert hard["stream_id"] == stops[0]["stream_id"]
    assert result["results"]["extend-one"]["forecast_requests"][1]["stream_id"] != stops[0]["stream_id"]
    assert all(row["fit_clock_us"] is not None for row in stops)
    assert all(row["request"]["as_of_utc"] == s.physical.as_of_utc for row in stops)


@pytest.mark.parametrize("defect", ["future", "missing", "age", "horizon"])
def test_cutoff_and_mandatory_inputs_are_gated_before_calling_model(engineering, defect):
    s, _, _ = engineering["clean-air"]
    feature = _features(s)
    if defect == "future":
        feature = replace(feature, evidence_as_of=(("identity", s.physical.as_of_utc+timedelta(seconds=1)),
                                                  *feature.evidence_as_of[1:]))
    elif defect == "missing":
        feature = replace(feature, evidence_as_of=feature.evidence_as_of[1:])
    elif defect == "age":
        feature = replace(feature, request=replace(feature.request, tyre_age_at_start=100))
    else:
        feature = replace(feature, request=replace(feature.request, horizon_laps=1))
    adapter = forecasts.StrategyForecastAdapter(None, (feature,), seed=1, samples=12, sample=None)
    with pytest.raises(StrategyInputError, match="cutoff|context|horizon"):
        adapter.attach(s)


def test_missing_and_wrong_hash_stop_before_unpickling(tmp_path, monkeypatch):
    import f1ri.simulator.pace_adapter as loading

    monkeypatch.setattr(loading.pickle, "loads", lambda _: pytest.fail("Unverified pickle was loaded"))
    with pytest.raises(FileNotFoundError):
        loading.load_pace_bundle(ROOT, tmp_path)
    candidate = tmp_path/"artifacts/models/degradation_v1/pace_v2_degradation_v1.pkl"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"wrong hash")
    with pytest.raises(ValueError, match="digest differs"):
        loading.load_pace_bundle(ROOT, tmp_path)
