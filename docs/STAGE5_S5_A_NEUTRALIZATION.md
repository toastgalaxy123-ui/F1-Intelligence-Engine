# Stage 5 S5-A race-control mechanics

The simulator uses the existing event scheduler, checkpoint transitions, physical predecessor chain, resolved S2 pit itinerary, and S4-A pass executor. The S0 clock is elapsed wall time from the declared scenario origin. The research trace also records cumulative suspension and active elapsed time. Classification remains a function of committed crossings, race phase, and driver status.

A canonical race-control command uses the same scheduler as pit and crossing events. Commands run before retirements, pit merges, crossings and passes at the same timestamp. The engine applies accepted commands through the existing track-state and race-phase transition. Rejected commands are retained in the control ledger. Ordinary passes require GREEN. Local caution leaves pace and field behavior on the green path while vetoing ordinary passes conservatively across the field.

## Physical execution

Movement uses the modeled prefix at the intervention clock and preserves committed checkpoint history. Three equal-distance racing segments per lap bound the interpolation. Their positions and modeled segment rates describe a declared scenario. They have no independently estimated real-world location error. At a control command coinciding with a predicted checkpoint, neutralization takes precedence and the crossing may commit once at the same clock if the car already reached the boundary.

VSC applies a common forward speed capped by the slowest declared clean pace in the racing field. This preserves gaps between route changes. Full SC uses a virtual boundary that stays ahead of its pacing car. Each racing path is bounded by the car's catch-up line and predecessor movement translated by circular separation. Train formation requires both proximity to the virtual boundary and support for the predecessor gaps. The state can remain in collection across multiple groups. Race laps and lapped-car offsets retain their meaning.

SC withdrawal places the field in restart pending, with an explicit leader pace and a release request. S5-A conservatively releases the field together after the active racing cars have crossed the timing line following withdrawal. The real rule applies to each driver separately. The virtual boundary is a mathematical constraint with no driver number or competitive laps. RED holds fractional progress stationary until an authorized rolling resumption. Standing restarts, regulatory reordering, unlapping, red service, and RED with an active pit itinerary carry explicit unsupported results.

## Route integration and validation

A resolved S2 itinerary supplies route travel, service, box scheduling, and merge. The neutralized field supplies the other cars' movement and a matching stay-out reference. A missed declared entry prerequisite and a coarse unresolved route receive explicit unsupported results. Retiming a missed entry together with box wait, pit-line crossings, and merge remains a separate integration issue.

Singapore and Imola replays use the archived race-control clocks and the active on-track field inferred from S0 timing at the cutoff. Initial positions and clean pace are declared. Archived lap crossings provide an independent comparison. Archived pit and retirement events remain comparison evidence in those windows. The run applies no empirical intervention probabilities.

The FIA archive lists Issue 5 of the 2025 sporting regulations as its latest 2025 issue, published before both races. The movement profile is a mechanical approximation with its scope in the rules manifest. [FIA regulations archive](https://www.fia.com/regulation/category/110) · [2025 sporting regulations Issue 5](https://www.fia.com/system/files/documents/fia_2025_formula_1_sporting_regulations_-_issue_5_-_2025-04-30.pdf)
