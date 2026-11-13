# Stage 5.3-C3 latent clean-sector reference

Use the frozen H1 sector cutoffs and following labels for target
readiness. Broaden clean-sector training evidence with official
timing from the same two development races. Tier A uses the C2
physical clean screen and independently reconciled timing. Tier B
uses Stage 1 clean-air evidence and reconciled official sector sums,
with no cached physical contradiction. Tier C and excluded rows
never train the reference. Keep Tier B training at weight 0.25 as a
separate sensitivity. Online adaptation and primary evaluation always
use Tier A.

The archive assumes official sector passage observations at their
recorded passage clocks. Historical feed arrival remains unverified.
Training and online updates wait for lap completion, the next line
and the existing lap-wide availability checks. Tire context uses the
observed stint start and, for a replacement stint, the relevant
observed stop. The weather lookup uses the last record at cutoff.
Neither model sees the target's elapsed time, physical exposure
outcome or later pit evidence.

R4-A fits one penalized model per race and sector. An unpenalized
intercept captures sector pace. Tire age, compound, progress and
temperature use regularized effects. Driver offsets shrink toward
zero. Race and layout are confounded in this sample. R4-B adds an
offset from earlier clean observations of the same driver, stint and
sector. Four prior-equivalent observations shrink it toward zero.
Missing context or extrapolation beyond the allowed training range
returns unsupported. A time-evolving R4-C requires a separately
justified follow-up. R4-D is the chronological calibration layer on
the fixed R4 mean models.

Evaluation A replays each race chronologically with the target driver
excluded from global fitting. Evaluation B rotates four driver folds
with a separate calibration fold for each test fold. Each primary
clean observation is held out once. The target driver's earlier Tier A
sectors may update the stint effect. R0–R3 reuse their existing
estimators on the same chronological Tier A evidence. Evaluate both
common support and each model's own support. Resample stints for
descriptive uncertainty and calibrate interval widths using one
earlier residual per calibration stint. Evaluation C requires an
approved repeat-layout or transfer cohort and remains unevaluated.

R4-B is the preregistered gate candidate. It must pass point and
calibration requirements in each development race. The following
readiness ledger tracks chronological comparable clean residuals
from distinct calibration stints. The unchanged C2 minimum of
three controls and a calibrated reference gate every segment proxy.
The original D8 artifact, H0 benchmark and sealed outcomes retain
their frozen roles.
[Frozen C3 plan](../config/experiments/stage5_s3_c3_latent_sector_pace.yaml)
