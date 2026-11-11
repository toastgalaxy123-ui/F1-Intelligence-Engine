# Sources and rate limits (verified November 2026; recheck before use)

OpenF1 docs: https://openf1.org/docs/
OpenF1 pricing/terms: https://openf1.org/
- Historical sessions from 2023 onward are free and unauthenticated.
- Free tier currently up to 3 requests/sec AND 30 requests/minute.
- Live window runs from 30 minutes before through 30 minutes after session.
  Live data requires a paid subscription; `OPENF1_TOKEN` optional later.
- Mandatory core data endpoints: meetings, sessions, drivers, laps, stints,
  pit, race_control, weather, session_result, starting_grid.
- Optional soon: position, intervals. Later: car_data, location.
- Pit: prefer `lane_duration`; `pit_duration` deprecated at end of 2026.
- `stop_duration` can be delayed during 2026 live sessions.
- `laps.date_start` is approximate. Race lap mini-sector arrays unavailable.
- `stints.tyre_age_at_start` counts already-used tire laps, important for reuse.
- `starting_grid` is keyed in OpenF1 by the meeting's `Qualifying` `session_key`
  (or `Sprint Qualifying` for Sprint grids), not the `Race` `session_key`.
  Querying an endpoint with zero matching rows returns HTTP 404
  `{"detail": "No results found."}`.
- `intervals.gap_to_leader` may be `'+1 LAP'`; preserve strings and parse
  separately, never cast missing/strings silently to zero.
- `overtakes` includes pit/penalty changes and may be incomplete.
- Telemetry sampling, available fields and data completeness vary by session.

Jolpica API: https://api.jolpi.ca/docs/
Old Ergast-compatible path: https://api.jolpi.ca/ergast/f1/
- Use results, qualifying, schedule, standings after v1.
- Free unauthenticated limit listed as 4 requests/sec, 500/hour.
- Noncommercial CC BY-NC-SA 4.0 terms: https://github.com/jolpica/jolpica-f1/blob/main/TERMS.md
- Keep Jolpica IDs in a separate namespace; join by curated identity map,
  not round/date guesswork without checks.

FastF1: https://docs.fastf1.dev/
- Optional future adapter; in Oct 2026 archive endpoints began requiring an
  F1 TV Access account or higher through FastF1 3.9+. Not a foundation dependency.

## Ingestion cautions

- Never request all high-frequency telemetry in one unlimited call.
- Store raw response, URL/query, retrieval UTC, SHA-256.
- Cache and retry 429 based on Retry-After, and back off on 5xx/timeouts.
- No concurrent HTTP workers under free tier by default.
- Confirm actual end-of-session data completeness; re-fetch explicitly if
  delayed fields (e.g., stationary stop duration) were missing.
- Providers are unofficial and may change upstream data retroactively.
