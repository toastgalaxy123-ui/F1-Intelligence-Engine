# Stage 5.3 — Physical track proximity and following

## Scope and gates

1. Audit source timing at Zandvoort and Baku. Preserve the S0 replay,
   record per-driver discrepancies, and mask local conflicts.
2. Version a racing-loop reference and check localization against independent
   timing and Stage 1 proximity. Keep telemetry acquisition bounded.
3. Verify synthetic circular order, lapped encounters and following with
   explicit support and one owner of each delay.
4. Require independently supported physical labels and a chronological
   evaluation cohort before fitting traffic-loss models. Separate a clean-air
   residual proxy from causal following loss and test against a deterministic
   baseline. Passing remains a later stage.

## Physical state

Classification, completed laps, and physical location are separate fields.
On a loop of length `L`, a car has wrapped coordinate `s` in `[0, L)`.
Its unwrapped progress is `completed_laps * L + s` when its lap alignment
is supported. The forward gap to another car is `(other.s - s) mod L`.
The difference in unwrapped progress distinguishes a seam crossing between
same-race-distance competitors from a genuine lap advantage.

Represent uncertainty as a symmetric radius about the wrapped coordinate.
If the plausible arc overlaps the subject or a competing neighbor, return
unresolved order. Unknown racing-route cars limit full-field claims. Pit,
retired and off-track cars remain in the snapshot without entering the
racing-route neighbor chain. A coarse or unresolved S2 rejoin remains unknown.

Store each estimate's source, as-of time, sample age, route, geometry version,
and quality. A passage-time gap uses two passages at the same control point.
A distance divided by local speed is an approximate gap with separate
provenance and uncertainty.

## Singapore pilot

Use a single closed racing reference built from a screened completed lap,
aligned to its observed control-line anchors. Use the FIA centreline length
for the scale. The fitted path is a sampled racing line with bounded
projection error. Calibrate no traffic model on this pilot. Check a
different driver's lap and two blue-flag windows. A blue flag identifies the
recipient and does not identify the approaching driver.

Retrospective reconstruction may interpolate between bracketing locations.
Point-in-time reconstruction truncates by both observation time and recorded
availability when present. Its geometry must already be available. Extrapolate
from recent on-track observations for a bounded interval with growing
uncertainty. Archive timestamps are availability proxies. Report missing,
stale, impossible and ambiguous measurements without imputing physical order.

Projection compares alternative segments and checks direction, continuity,
maximum plausible speed, and timing-line compatibility. Route evidence comes
from lap/pit records or declared synthetic geometry. A pit lane position
never projects into the ordinary racing-route order.

The pilot groups descriptive exposures on a 0.5-second grid. Each episode
requires complete field coverage, a green track interval, a resolved lapping
relationship or same-lap neighbor, a stable neighbor identity, and a gap upper
bound no greater than the configured threshold. Episodes end at the first
missing or ineligible sample. The archived track intervals are a descriptive
screen. A training target would require an independently evaluated clean-air
Pace V2 reference with uncertainty, controls for pace confounders, and
chronological splits by race.

## Deterministic following

For a declared circular synthetic field, constrain a car's passage at a
common checkpoint behind its physical predecessor, including across the
start/finish seam and across laps. Use one adjustment per traversal:

```text
effective time = max(clean time + compatible behavior delay, minimum admissible time)
committed traffic delay = effective time − clean time
```

Use zero behavioral delay until fitted evidence is available. Preserve the
clean timing and the final adjustment in the ledger. Every car remains
present if pace support is absent. A traffic-constrained run requires
localized order and an explicit pace policy for every participating car.
The S2 rejoin adapter uses the common first-sector checkpoint and declared
linear travel between synthetic checkpoints. Resolved merges can initialize a
neighborhood. Approximate and coarse net-loss merges have no precise snapshot.
Following runs within a declared green window of stable circular order.
Event-driven changes of the following chain require a new window.
