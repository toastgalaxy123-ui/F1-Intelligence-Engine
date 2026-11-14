"""Load a repository-approved successor for its validated dashboard modes."""
import json
import pickle
from datetime import datetime
from hashlib import sha256

from f1ri.models.stint_forecast import PaceModelV2
from f1ri.simulator.pace_adapter import PaceBundle

REPORT = "artifacts/reports/stage3_r1_model_release.json"
BUNDLE = "artifacts/models/PaceV2_D8R1/pace_v2_d8r1.pkl"
REQUIRED_MEAN_GATES = (
    "G0_data_provenance", "G1_numerical_integrity", "G2_mean_quality",
    "G4_replacement_forecasting", "G5_strategy_integration", "G6_reproducibility",
)


def load_successor(root, *, correlated):
    path = root/REPORT
    if not path.is_file():
        return None, "Stage 3-R release evidence is unavailable."
    spec = json.loads(path.read_bytes())
    if spec["release"] not in {"MEAN_ONLY_VALIDATED", "STRATEGY_INTEGRATED"}:
        return None, spec.get("reason", "Stage 3-R held its successor from operational release.")
    if correlated and spec["release"] != "STRATEGY_INTEGRATED":
        return None, "Correlated strategy forecasts are unavailable because long-horizon coverage missed its gate."
    flags = spec["promotion_gates"]
    required = (*REQUIRED_MEAN_GATES, "G3_uncertainty_quality") if correlated else REQUIRED_MEAN_GATES
    if any(flags.get(k) is not True for k in required):
        return None, "The requested model mode has unmet validation gates."
    bundle_path = root/BUNDLE
    if not bundle_path.is_file():
        return None, "The versioned model bundle is missing or its checksum differs."
    # The artifact path and approving manifest are repository controlled, with no upload input.
    payload = bundle_path.read_bytes()
    if sha256(payload).hexdigest() != spec["model_sha256"]:
        return None, "The versioned model bundle is missing or its checksum differs."
    try:
        model = pickle.loads(payload)
    except (ImportError, AttributeError, pickle.UnpicklingError):
        return None, "The model bundle requires the recorded modeling dependencies."
    trained = datetime.fromisoformat(spec["trained_through_utc"])
    calibrated = datetime.fromisoformat(spec["calibrated_through_utc"])
    if (not isinstance(model, PaceModelV2) or model.model_version != "PaceV2_D8R1"
            or trained != model.trained_through_utc_ or not model.is_fitted_
            or correlated and not model.trajectory_sampler_.is_calibrated_):
        return None, "The bundle's model identity or training cutoff differs from its manifest."
    return PaceBundle(model, spec["model_sha256"], trained, calibrated, tuple(spec["train_sessions"]),
                      tuple(spec["calibration_sessions"]), spec["regulation_era"]), spec["release"]
