# Stage 5 S4-B.1 evidence stabilization

The experiment replays the frozen S4-B pipeline and verifies the full generated file set against the original run. It uses the original opportunities, historical brackets, cache cohorts, and archived reconstruction. S4-B.1 writes a separate evidence ledger and leaves the original labels available for comparison. The simulator is outside the historical evidence path.

## Evidence contract

An event dossier retains the S4-B ID and UTC bracket, projected and raw location samples, physical uncertainty, archived marker times, local pit and race-control context, and published lap-chart ranks for both cars. Location, the projected gap and derived adjacency belong to the same evidence family. Official timing and published lap-chart ranks are independent of the projection. They may share an upstream timing feed with the OpenF1 timing and position records. Their agreement does not supply two independent measurements of a within-sector pass.

The original Tier A width gate is four source grid periods. Full field, compatible independent rank and timing evidence, and coherent route support are also required. A case with a material physical-versus-timing conflict is ambiguous. The evidence audit records whether a target's independently timed sector passage predates a bracket while the projected uncertainty bounds keep it behind that marker. A stationary projection and lap-chart continuity add context. An isolated control-line order cannot rule out a transient within-sector exchange.

## Censoring and exposure

The first-loss ledger starts from every frozen 10-second localization censor. It distinguishes the frozen censoring tick from the strictly verified prefix. Driver-specific reconstruction reasons and source clocks identify missing, stale, ambiguous, or unusable observations. An exact overlapping-window tick can support a recovery candidate. A resolved alternative outcome requires full native-grid coverage of the interval under the original label rules.

The 30-second event-driven view retains contiguous supported exposure up to the first unsupported sample or competing event, a verified pass bracket, or the configured maximum. Exposure before a pass ends at the lower bound. An interruption between sampled times retains timing bounds. Resumption after loss is diagnostic and cannot restore the earlier risk prefix. Risk rows and the fixed outcomes use the same cutoff population. Pair-wise time unions keep overlapping cutoffs from multiplying exposure time.

Race, unordered race pair, directed tire-stint pair, original linked encounter and consecutive selected samples are reported separately. Consecutive samples split on gaps in the selected opportunities. The resulting components measure fragmentation and can overcount independent encounters. Tire-stint context is used for grouping. Its archive receipt clock is unknown.

The primary descriptive analysis compares cutoff-visible features. Gap, pace, closing, weather availability, lap, and field density can describe differences in observation support. Extreme bounds assign unresolved groups both ways for sensitivity. These bounds describe the selected archive and do not calibrate future probabilities.

## Source scope

Austria and Hungary retain development roles. Singapore is the frozen engineering pilot. Imola is reconstruction-only. The gap census inventories the existing raw payloads and timing proxy in two exact development intervals. An upper bound on timing-line candidate pairs does not identify on-track neighbors or independent event yield. The frozen request budget is zero.

[Experiment protocol](../config/experiments/stage5_s4_b1_evidence_stabilization.yaml)
