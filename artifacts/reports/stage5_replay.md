# Stage 5 deterministic engine and replay

The S0 engine ran both historical fixtures from the existing OpenF1 warehouse
in read-only mode. Scheduled distances were explicit inputs. The engine
reconstructed timing-line crossings from lap starts and durations, replayed
pit windows and track-status transitions, and compared the resulting field
with the stored session results.

| Check | Singapore 2025 | Imola 2025 |
| --- | ---: | ---: |
| Session key | 9896 | 9987 |
| Field | 20 | 20 |
| Scheduled race distance | 62 | 63 |
| Lap completions reconstructed | 1,229 | 1,205 |
| Cars matching official lap count | 20/20 | 20/20 |
| Classified finishers matching official position | 20/20 | 18/18 |
| Pit windows replayed | 23/23 | 37/37 |
| Retirements reconstructed | 0 | 2 |
| Lap boundaries compared | 1,209 | 1,187 |
| Maximum boundary disagreement | 0.637 s | 0.460 s |
| Timing tier | B | B |

Singapore recorded 11 finishers one lap down. The reconstructed crossing
order put Lewis Hamilton seventh, 0.437 s ahead of Fernando Alonso. The
independently sourced five-second penalty moved Hamilton to eighth in the
reconstructed classification and preserved his observed crossing time.
[FIA penalty decision](https://www.fia.com/system/files/decision-document/2025_singapore_grand_prix_-_infringement_-_car_44_-_leaving_the_track_without_a_justifiable_reason_multiple_times.pdf)
and [FIA final race classification](https://www.fia.com/system/files/decision-document/2025_singapore_grand_prix_-_final_race_classification.pdf).

Imola replayed VSC and Safety Car transitions from the warehouse race-control
timeline. The two retirements preserved 27 completed laps for Esteban Ocon and
44 for Kimi Antonelli. Each retirement event uses last observed progress as
a lower bound. The replay inherits the observed lap times through each
neutralization.

The deterministic tests cover analytical elapsed-time conservation,
checkpoint following, pit route and tire changes, retirements, same-time
chequered flags, and reproducible event ordering. Tier B reflects approximate
sector and pit timing. These audits measure replay fidelity. The next
forecasting increment requires correlated Pace V2 trajectories and
point-in-time validation.

With a warehouse containing these sessions:

```bash
f1ri stage5-replay --session-key 9896 --scheduled-laps 62
f1ri stage5-replay --session-key 9987 --scheduled-laps 63
```
