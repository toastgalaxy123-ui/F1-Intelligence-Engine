# Architecture decision record (ADR)

ADR-001: Python source package (`src/f1ri`); install editable (`pip -e`).
Reason: import stability and test isolation, no sys.path hacks.

ADR-002: Raw source JSON is read-only by default. Explicit `--refresh`
replaces it and records a new SHA256 + retrieval timestamp.
Reason: backtests remain auditable.

ADR-003: OpenF1 session_key is canonical within project; Jolpica IDs stay
namespaced. Reason: no fake cross-provider ID equivalence.

ADR-004: DuckDB for small typed tables, Parquet for later 3.7Hz telemetry.
Reason: avoid unnecessary service infrastructure and huge JSON scans.

ADR-005: All timestamps UTC and timezone-aware; all lap durations seconds.
Reason: no ambiguous cross-timezone joins.

ADR-006: Distinguish observed, estimated and simulated data and never silently
label a potentially neutralized lap as clean green.
Reason: protect validity of pace research.

ADR-007: Simulation consumes model output protocols rather than imports
of specific model libraries. Reason: models evolve independently.

ADR-008: Race-level chronological backtests, with point-in-time cutoffs.
Reason: avoid race-level and time leakage.

ADR-009: historical race replays before live support; no upstream auth needed
for MVP. Reason: control costs, completeness, reproducibility.

ADR-010: season/regulation era modeled explicitly. Reason: 2026 may change
mechanisms; do not assume stable behavior from pre-2026 data.
