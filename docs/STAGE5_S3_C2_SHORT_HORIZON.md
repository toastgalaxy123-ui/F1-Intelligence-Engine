# Stage 5.3-C2 short-horizon protocol

H0 is the frozen lap-level benchmark. H1 uses official elapsed sector
times and reconciles both boundaries against geometry projected from
cached fixes. The archive timing clock is an availability proxy. H2 uses
four equal-distance quarters of each frozen circuit geometry and adjacent
forward fixes separated by at most one second. Its cutoff is when the
right-hand start fix is available. The measured traversal starts at the
interpolated crossing, and the ledger records the elapsed start portion
at cutoff. Two disjoint location-fix subsequences give a repeatability
check. Their shared geometry and receiver leave systematic error
unmeasured. H3 uses clock-aligned 5, 10, 20 and 30 second diagnostics
and records observed progress.

The candidate ledger uses the latest point-in-time reconstruction at
cutoff. The retrospective ledger records route continuity, later
opponent changes, gaps and exposure. Conservative lap-wide pass, pit and
weather exclusions apply even when the precise event time within the
lap is unknown. The same leader must remain close for the first two
seconds, and an observed neighbor change censors the following label.
At least 90% usable support and a maximum two-second gap apply.
Windows deduplicate by planned time margin. The unit for independence
is the race, driver, leader and stint. Episode IDs and overlapping
intervals remain traceable.

The four traversal references use chronological clean observations.
R0 is a circuit/checkpoint median. R1 scales the frozen D8 lap prediction
using a median traversal share from earlier, independently clean Stage 1
laps and propagates D8 and share uncertainty. R2 is a context-matched
same-driver, same-stint median. It allows earlier clean observations
from the evaluation driver as online adaptation. R3 shrinks a
context-matched median toward the circuit/checkpoint median. Global
training excludes the seeded held-out driver group. For all references,
training and interval-calibration evidence must be available strictly
before cutoff. Conservative source clocks include lap-wide censoring
evidence. Matching requires three earlier comparable residuals from
distinct driver–stint units, a feasibility minimum for a median robust
to one extreme observation. H0 retains its original five-lap rule.

The sample, accuracy, uncertainty and conversion thresholds in the
versioned plan are research feasibility gates. Ten independent groups
and ten clean validation stints per race are minimum diversity checks.
The 0.30 s planning effect is a design scale. It does not assume a
known traffic effect. Short-horizon metrics report achieved precision
and coverage against these thresholds. No reserved outcomes are
opened. The experiment runs on cached Austria and Hungary sources.
[Frozen plan](../config/experiments/stage5_s3_c2_short_horizon.yaml)
