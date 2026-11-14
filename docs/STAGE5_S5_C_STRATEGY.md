# Stage 5 S5-C deterministic strategy comparisons

The strategy layer supplies pit decisions and clean pace to the existing S5-A.1 event loop. The scheduler commits physical entry, service, rejoin, and racing crossings. A comparison hashes the shared field, pace, clocked interventions, opponent policies, and rules. Every branch starts with its own copy of the tyre inventory and policy progress.

## Run locally

Install the project with the existing dashboard extra, then run Streamlit on localhost.

```powershell
python -m pip install -e ".[modeling,dashboard]"
python -m streamlit run src/f1ri/dashboard/app.py --server.address 127.0.0.1
```

Choose a scenario and pace source, then select a reference and alternatives. Inspect the progress, order, pit and support views. The interface can download the exact scenario and its result. A downloaded JSON scenario can be reopened in the dashboard or passed to the CLI.

```powershell
python -m f1ri.cli stage5-s5-c --list
python -m f1ri.cli stage5-s5-c --fixture sc --reference continue --candidate react-sc --output artifacts/runs/sc_pair_v1
python -m f1ri.cli stage5-s5-c --scenario artifacts/runs/sc_pair_v1/scenario.json --validate
python -m f1ri.cli stage5-s5-c --output artifacts/runs/stage5_s5_c_strategy_scenarios_v1
```

Use a new output directory each time. Programmatic callers can construct a `StrategyScenario`, call `validate_scenario`, and call `run_strategy` or `compare_strategies`. An exported scenario document is the `s5-c-strategy-v1` input format.

## Interpreting the result

- Pace curves give clean lap seconds by tyre-set age. A replacement changes the curve when service fits the new set. Tyre usage continues under neutralization using the existing lap accounting. The executed time already includes movement, route and scheduler constraints.
- Reactive policies see committed race-control signals at their decision clock. Fixture times explicitly declared with a policy are calendar-clock commitments. A fixture named “after-green” uses a declared review clock. Changing the intervention timing can change the state at that review.
- The default objective minimizes time to the same racing checkpoint, summed for multiple target drivers. The reported classification belongs to the target crossing. The horizon order belongs to the end of the simulated window. Finish order is only available when the engine finishes the race.
- The supported 2025 dry, non-Monaco finish check requires distinct dry compounds and a mandatory race specification supplied in the scenario. Tactical comparisons do not establish race-wide tyre compliance. Wet allocations, Monaco exceptions, and unsupported RED service rules are outside this profile. [2025 FIA Sporting Regulations, Issue 5](https://www.fia.com/system/files/documents/fia_2025_formula_1_sporting_regulations_-_issue_5_-_2025-04-30.pdf)
- `FROZEN_PACE_REPLAY` uses a pinned saved Singapore continuation sample on a declared two-car field. The same saved draw is read by both branches. The sample artifact is an existing local S1 dependency, listed with its SHA-256 in the experiment protocol.
- `FULL_MODEL` currently reports unsupported. Live strategy forecasts need the verified D8 bundle and candidate-specific continuation and replacement requests with paired streams. Neither mode silently uses illustrative curves.
- The cyclic physical predecessor order and the timing classification describe different relationships. A pit merge can change the predecessor before the next classification crossing. The timing engine's “traffic delay” field is the difference between effective and clean traversal time, including other admissibility constraints in a neutralized window.
