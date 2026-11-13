# Stage 5.3-C4 independent clean-sector validation

C4 reuses C3's R4-B hyperparameters, four driver folds, calibration
partition and feasibility thresholds. Austria and Hungary 2025 are
the only approved development outcome sources. The original Hungary
continuation and its coverage extension are the two locked expansion
sources. C4 replays H1 against their hash-verified cached telemetry.
It adopts new observations where C2 lacked a row or left its cutoff
or traversal unresolved. The frozen C2 following flags take precedence.
Tier A still requires the C3 physical and timing screens. Tier B
weights 0.10, 0.25 and 0.50 are development sensitivities.

U0 pools chronological absolute residuals with one latest score per
stint. U1 standardizes those scores by the forecast's support-dependent
scale. U2 uses the worst standardized eligible score per calibration
stint. Each uses the same disjoint fold, observation clock and
eight-stint minimum. Overlap between the two models' fitting prefixes
is recorded. A calibration observation cannot be an outcome used in
either fit. The methods estimate empirical widths without a
finite-sample coverage guarantee. The separate ten-stint evaluation
minimum remains in force.

H1 controls must match the C2 context tolerances and come from three
distinct earlier calibration stints. Control opportunities may
overlap with the calibration set, and the ledgers retain those IDs.
The acquisition bound allows context-matched Tier B seeds to obtain
physical confirmation and eligible independent forecasts, while
preserving the calibration fold. It gives each stint at most one
control per target and does not count Tier C outcomes as recoverable.

2024 candidates receive metadata-only review. A transfer run would
require explicit approval, layout and timing-marker equivalence, and
separate source roles. A 2024-to-2025 transfer run would be exploratory.
The new-request budget is zero. Candidate query spans in the manifest
describe unmet evidence and carry no authorization to download.
[Frozen C4 plan](../config/experiments/stage5_s3_c4_clean_reference_validation.yaml)
