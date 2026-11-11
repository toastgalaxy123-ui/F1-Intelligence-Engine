# F1 Race Intelligence Engine

A reproducible, point-in-time Formula 1 analytics project. This starter implements
**historical data ingestion**, a small **typed DuckDB warehouse**, a **provisional
observational lap mart**, basic **data checks**, and **model/simulator contracts**.
It does NOT claim to implement validated tire degradation, strategy optimization,
or real-time race forecasting. Those are successive research milestones.

## Windows / PowerShell quick start

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev,modeling,dashboard]"
f1ri discover --year 2025 --country Singapore
# Replace 12345 with the actual numeric session_key from the command above.
$session = 12345
f1ri ingest --year 2025 --session-key $session
f1ri build --year 2025 --session-key $session
f1ri check --session-key $session
pytest -q
```

On macOS/Linux: `python3 -m venv .venv`, `source .venv/bin/activate`.
Use `python -m pip install -e ".[dev]"` to avoid optional plotting/dashboard packages.

If PowerShell blocks activation: `Set-ExecutionPolicy -Scope Process Bypass`
or call `.\.venv\Scripts\python.exe` and `.\.venv\Scripts\f1ri.exe` directly.

## Useful SQL

```python
import duckdb
con = duckdb.connect('data/warehouse/f1ri.duckdb', read_only=True)
print(con.execute("""
 SELECT driver_number, compound,
        COUNT(*) AS provisional_laps,
        MEDIAN(observed_lap_time_s) AS median_s
 FROM gold.lap_observations
 WHERE provisional_pace_candidate
 GROUP BY 1, 2 ORDER BY 1, 2
""").df())
```

`provisional_pace_candidate` is NOT a verified green-flag/clean-air label.
See `docs/DATA_CONTRACTS.md` before training a pace model.

## Development rules

1. Never edit `data/raw` JSON in place; use `--refresh` to obtain a new snapshot.
2. Dataset primary keys use `session_key`, `driver_number`, `lap_number`.
3. UTC for all stored timestamps; seconds for times; °C for temperatures.
4. Do not use post-race features in models intended for a mid-race cutoff.
5. Hold out future races as complete sessions; don't randomly split individual laps.
6. Never silently coerce an unverified race-control status into GREEN.
7. Don't download high-frequency telemetry as part of normal session ingestion.
8. Keep source adapters separate from models, and models separate from simulation.

Read `docs/ARCHITECTURE.md`, `docs/DATA_SOURCES.md`, `docs/DATA_CONTRACTS.md`,
`docs/BUILD_ORDER.md`, `docs/MODELING.md`, and `docs/DECISIONS.md`.

## What is deliberately deferred?

- F1 TV/FastF1 authentication (not necessary for the first milestones)
- Racing-status interval reconstruction and verified clean-lap labels
- Green-flag pace, tire degradation, fuel and traffic models
- Pit-loss calibration, race-state reconstruction, overtake model
- Monte Carlo optimizer, dashboard charts, live ingestion

## Attribution and licenses

OpenF1 is an unofficial third-party service. Jolpica's source data is
non-commercial under CC BY-NC-SA 4.0 and has attribution requirements.
Check each provider's current terms before redistribution or commercialization.
Do not commit large source datasets or third-party branded graphics.
