"""Construct tire-degradation features, support classifications, and Stage 3.2 dataset audits."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from f1ri.ingestion.compound_allocations import era_qualified_compound
from f1ri.models.contracts import (
    SCENARIO_CONTINUE_STINT,
    SUPPORT_EXTRAPOLATED,
    SUPPORT_LIMITED,
    SUPPORT_SUPPORTED,
    SUPPORT_UNSUPPORTED,
    StintForecastRequest,
)

DRY_COMPOUNDS = ("HARD", "MEDIUM", "SOFT")


@dataclass
class HistoricalSupportTable:
    """Empirical historical support lookup for (era, circuit, compound, tire_age) combinations."""

    # Maps (era, circuit_short_name, compound_relative) -> (n_stints, p90_age, max_age)
    circuit_compound_stats_: dict[tuple[str, str, str], tuple[int, float, float]] = field(
        default_factory=dict
    )
    # Maps (era, compound_absolute) -> (n_stints, p90_age, max_age)
    abs_compound_stats_: dict[tuple[str, str], tuple[int, float, float]] = field(
        default_factory=dict
    )
    # Maps (era, compound_relative) -> (n_stints, p90_age, max_age)
    rel_compound_stats_: dict[tuple[str, str], tuple[int, float, float]] = field(
        default_factory=dict
    )
    is_fitted_: bool = False

    def fit(self, stint_laps_df: pd.DataFrame) -> HistoricalSupportTable:
        clean = stint_laps_df[
            stint_laps_df["is_clean_green_pace_lap"].fillna(False).astype(bool)
            & stint_laps_df["observed_lap_time_s"].notna()
        ].copy()
        if clean.empty:
            self.is_fitted_ = True
            return self

        self.circuit_compound_stats_ = {}
        for (era, circ, rel_c), grp in clean.groupby(
            ["regulation_era", "circuit_short_name", "compound_relative"], dropna=False
        ):
            if pd.isna(circ) or pd.isna(rel_c):
                continue
            ages = grp["tyre_age_at_lap_start"].dropna().to_numpy(dtype=float)
            if len(ages) == 0:
                continue
            n_stints = int(grp["stint_id"].nunique()) if "stint_id" in grp.columns else len(ages) // 8
            self.circuit_compound_stats_[(str(era), str(circ), str(rel_c))] = (
                max(n_stints, 1),
                float(np.quantile(ages, 0.90)),
                float(np.max(ages)),
            )

        self.abs_compound_stats_ = {}
        if "compound_absolute" in clean.columns:
            for (era, abs_c), grp in clean.groupby(
                ["regulation_era", "compound_absolute"], dropna=True
            ):
                ages = grp["tyre_age_at_lap_start"].dropna().to_numpy(dtype=float)
                if len(ages) == 0:
                    continue
                n_stints = int(grp["stint_id"].nunique()) if "stint_id" in grp.columns else len(ages) // 8
                self.abs_compound_stats_[(str(era), str(abs_c))] = (
                    max(n_stints, 1),
                    float(np.quantile(ages, 0.90)),
                    float(np.max(ages)),
                )

        self.rel_compound_stats_ = {}
        for (era, rel_c), grp in clean.groupby(
            ["regulation_era", "compound_relative"], dropna=False
        ):
            ages = grp["tyre_age_at_lap_start"].dropna().to_numpy(dtype=float)
            if len(ages) == 0:
                continue
            n_stints = int(grp["stint_id"].nunique()) if "stint_id" in grp.columns else len(ages) // 8
            self.rel_compound_stats_[(str(era), str(rel_c))] = (
                max(n_stints, 1),
                float(np.quantile(ages, 0.90)),
                float(np.max(ages)),
            )

        self.is_fitted_ = True
        return self

    def classify_support(
        self,
        regulation_era: str | None,
        circuit_short_name: str | None,
        compound_relative: str | None,
        compound_absolute: str | None,
        projected_tyre_age: int,
    ) -> tuple[str, str | None]:
        """Classify historical evidence for a projected lap as SUPPORTED, LIMITED_SUPPORT, EXTRAPOLATED, or UNSUPPORTED."""
        rel = str(compound_relative or "").upper()
        if rel not in DRY_COMPOUNDS or projected_tyre_age < 0:
            return SUPPORT_UNSUPPORTED, f"Non-dry or invalid compound/age ({rel}, age={projected_tyre_age})"

        if projected_tyre_age > 58:
            return (
                SUPPORT_UNSUPPORTED,
                f"Projected tire age {projected_tyre_age} exceeds plausible dry stint limits",
            )

        era = str(regulation_era or "2023_2025")
        circ = str(circuit_short_name or "")
        age = float(projected_tyre_age)

        circ_stat = self.circuit_compound_stats_.get((era, circ, rel))
        abs_stat = (
            self.abs_compound_stats_.get((era, str(compound_absolute)))
            if compound_absolute
            else None
        )
        rel_stat = self.rel_compound_stats_.get((era, rel)) or self.rel_compound_stats_.get(
            ("2023_2025", rel)
        )

        if circ_stat is not None:
            n_stints, p90_age, max_age = circ_stat
            if n_stints >= 3 and age <= max(p90_age + 2.0, 12.0):
                return SUPPORT_SUPPORTED, None
            if age <= max_age + 3.0:
                return (
                    SUPPORT_LIMITED,
                    f"Sparse circuit-compound sample at age {int(age)} (circuit p90={p90_age:.0f}, max={max_age:.0f})",
                )

        ref_stat = abs_stat or rel_stat
        if ref_stat is not None:
            n_stints, p90_age, max_age = ref_stat
            if age <= p90_age + 2.0:
                return (
                    SUPPORT_LIMITED,
                    f"Pooled compound support used for ({circ or 'unseen'}, {rel}, age={int(age)})",
                )
            if age <= max_age + 12.0:
                return (
                    SUPPORT_EXTRAPOLATED,
                    f"Projected tire age {int(age)} exceeds historical p90 ({p90_age:.0f}) and approaches/exceeds max ({max_age:.0f})",
                )
            return (
                SUPPORT_UNSUPPORTED,
                f"Projected tire age {int(age)} far exceeds observed maximum ({max_age:.0f}) for {rel}",
            )

        if age <= 25:
            return SUPPORT_LIMITED, "Fallback prior support"
        return SUPPORT_EXTRAPOLATED, f"Projected tire age {int(age)} extrapolated without historical compound stats"


def basis_warm_up(tyre_age: np.ndarray | float) -> np.ndarray | float:
    """Early-stint tire warm-up / bedding-in basis (decays exponentially over ages 0..3)."""
    a = np.maximum(tyre_age, 0.0)
    return np.exp(-a / 1.5)


def basis_late_cliff(tyre_age: np.ndarray | float, knot_age: float = 18.0) -> np.ndarray | float:
    """Smooth hinge quadratic basis for accelerated late-stint wear beyond knot_age."""
    excess = np.maximum(0.0, np.asarray(tyre_age, dtype=float) - float(knot_age)) / 10.0
    res = excess**2
    if np.ndim(tyre_age) == 0:
        return float(res)
    return res


def row_to_stint_forecast_request(row: pd.Series | dict[str, Any]) -> StintForecastRequest:
    """Convert a gold.stint_training_examples row into a typed StintForecastRequest."""
    get = (
        row.get
        if isinstance(row, dict)
        else lambda k, d=None: row[k] if k in row and pd.notna(row[k]) else d
    )
    as_of = get("forecast_as_of_utc")
    if isinstance(as_of, pd.Timestamp):
        as_of = as_of.to_pydatetime()
    elif not isinstance(as_of, datetime):
        raise TypeError(f"forecast_as_of_utc must be a datetime, got {type(as_of)}")

    def _parse_tuple(json_str: str | None, cast_fn: type) -> tuple[Any, ...]:
        if not json_str or not isinstance(json_str, str):
            return ()
        try:
            vals = json.loads(json_str)
            return tuple(cast_fn(v) for v in vals)
        except (ValueError, TypeError):
            return ()

    return StintForecastRequest(
        session_key=int(get("session_key")),
        driver_number=int(get("driver_number")),
        cutoff_lap_number=int(get("cutoff_lap_number")),
        planned_start_lap=int(get("planned_start_lap")),
        horizon_laps=int(get("requested_horizon") or get("horizon") or 1),
        compound_relative=str(get("compound_relative") or get("compound") or "MEDIUM"),
        tyre_age_at_start=int(get("tyre_age_at_forecast_start") or get("tyre_age_at_lap_start") or 0),
        as_of_utc=as_of,
        scenario_type=str(get("scenario_type") or SCENARIO_CONTINUE_STINT),
        compound_absolute=(
            str(get("compound_absolute")) if get("compound_absolute") is not None else None
        ),
        compound_mapping_confidence=str(get("compound_mapping_confidence") or "UNKNOWN"),
        meeting_key=int(get("meeting_key")) if get("meeting_key") is not None else None,
        season=int(get("season")) if get("season") is not None else None,
        regulation_era=str(get("regulation_era") or "2023_2025"),
        circuit_key=int(get("circuit_key")) if get("circuit_key") is not None else None,
        circuit_short_name=(
            str(get("circuit_short_name")) if get("circuit_short_name") is not None else None
        ),
        driver_id=str(get("driver_id")) if get("driver_id") is not None else None,
        team_id=str(get("team_id")) if get("team_id") is not None else None,
        stint_number=int(get("stint_number") or 1),
        scheduled_race_laps=int(get("scheduled_race_laps") or 58),
        qualifying_lap_time_s=(
            float(get("qualifying_lap_time_s"))
            if get("qualifying_lap_time_s") is not None
            else None
        ),
        grid_position=int(get("grid_position")) if get("grid_position") is not None else None,
        last_clean_lap_time_s=(
            float(get("last_clean_lap_time_s"))
            if get("last_clean_lap_time_s") is not None
            else None
        ),
        median_last_3_clean_laps_s=(
            float(get("median_last_3_clean_laps_s"))
            if get("median_last_3_clean_laps_s") is not None
            else None
        ),
        median_last_5_clean_laps_s=(
            float(get("median_last_5_clean_laps_s"))
            if get("median_last_5_clean_laps_s") is not None
            else None
        ),
        median_stint_clean_laps_s=(
            float(get("median_stint_clean_laps_s"))
            if get("median_stint_clean_laps_s") is not None
            else None
        ),
        clean_laps_observed_current_stint=int(get("clean_laps_observed_current_stint") or 0),
        clean_laps_observed_race=int(get("clean_laps_observed_race") or 0),
        last_clean_lap_age=(
            int(get("last_clean_lap_age")) if get("last_clean_lap_age") is not None else None
        ),
        observed_stint_clean_ages=_parse_tuple(get("observed_stint_clean_ages_json"), int),
        observed_stint_clean_times_s=_parse_tuple(get("observed_stint_clean_times_json"), float),
        observed_stint_clean_progress=_parse_tuple(
            get("observed_stint_clean_progress_json"), float
        ),
        prev_stint_compound_relative=(
            str(get("prev_stint_compound_relative"))
            if get("prev_stint_compound_relative") is not None
            else None
        ),
        prev_stint_compound_absolute=(
            str(get("prev_stint_compound_absolute"))
            if get("prev_stint_compound_absolute") is not None
            else None
        ),
        prev_stint_end_tyre_age=(
            int(get("prev_stint_end_tyre_age"))
            if get("prev_stint_end_tyre_age") is not None
            else None
        ),
        track_temperature_c=(
            float(get("last_known_track_temp_c"))
            if get("last_known_track_temp_c") is not None
            else None
        ),
        air_temperature_c=(
            float(get("last_known_air_temp_c"))
            if get("last_known_air_temp_c") is not None
            else None
        ),
    )


def build_degradation_dataset_audit(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Generate the comprehensive Stage 3.2 tire-degradation and survivorship-bias audit."""
    eff_sample = con.execute("""
        SELECT
            regulation_era,
            COUNT(*) AS total_stint_laps,
            SUM(CASE WHEN is_clean_green_pace_lap THEN 1 ELSE 0 END) AS clean_laps,
            COUNT(DISTINCT CASE WHEN is_clean_green_pace_lap THEN stint_id END) AS distinct_clean_stints,
            COUNT(DISTINCT CASE WHEN is_clean_green_pace_lap THEN (session_key::VARCHAR || '_' || driver_number::VARCHAR) END) AS distinct_driver_races,
            COUNT(DISTINCT session_key) AS distinct_race_weekends,
            COUNT(DISTINCT circuit_short_name) AS distinct_circuits
        FROM gold.stint_lap_observations
        GROUP BY 1
        ORDER BY 1
    """).df()

    by_abs_compound = con.execute("""
        SELECT
            regulation_era,
            COALESCE(compound_absolute, 'UNMAPPED') AS compound_absolute,
            compound_relative,
            COUNT(*) AS total_laps,
            SUM(CASE WHEN is_clean_green_pace_lap THEN 1 ELSE 0 END) AS clean_laps,
            COUNT(DISTINCT CASE WHEN is_clean_green_pace_lap THEN stint_id END) AS clean_stints,
            ROUND(MEDIAN(tyre_age_at_lap_start), 1) AS median_tyre_age,
            ROUND(QUANTILE_CONT(tyre_age_at_lap_start, 0.90), 1) AS p90_tyre_age,
            MAX(tyre_age_at_lap_start) AS max_tyre_age
        FROM gold.stint_lap_observations
        WHERE compound_relative IN ('HARD', 'MEDIUM', 'SOFT')
        GROUP BY 1, 2, 3
        ORDER BY 1, 2, 3
    """).df()

    by_tyre_age = con.execute("""
        SELECT
            CASE
                WHEN tyre_age_at_lap_start <= 5 THEN '00_05'
                WHEN tyre_age_at_lap_start <= 10 THEN '06_10'
                WHEN tyre_age_at_lap_start <= 15 THEN '11_15'
                WHEN tyre_age_at_lap_start <= 20 THEN '16_20'
                WHEN tyre_age_at_lap_start <= 25 THEN '21_25'
                WHEN tyre_age_at_lap_start <= 30 THEN '26_30'
                ELSE '31_PLUS'
            END AS tyre_age_bucket,
            COUNT(*) AS total_laps,
            SUM(CASE WHEN is_clean_green_pace_lap THEN 1 ELSE 0 END) AS clean_laps,
            ROUND(100.0 * AVG(CASE WHEN is_clean_green_pace_lap THEN 1.0 ELSE 0.0 END), 2) AS clean_retention_pct,
            COUNT(DISTINCT CASE WHEN is_clean_green_pace_lap THEN stint_id END) AS surviving_clean_stints
        FROM gold.stint_lap_observations
        WHERE compound_relative IN ('HARD', 'MEDIUM', 'SOFT')
        GROUP BY 1
        ORDER BY 1
    """).df()

    by_termination = con.execute("""
        SELECT
            compound_relative,
            CASE
                WHEN ended_by_retirement THEN 'ENDED_BY_RETIREMENT'
                WHEN was_pit_terminated THEN 'PIT_TERMINATED'
                WHEN ended_by_race_finish THEN 'RACE_FINISH'
                ELSE 'OTHER'
            END AS termination_reason,
            COUNT(*) AS n_stints,
            ROUND(AVG(observed_stint_laps), 1) AS mean_stint_laps,
            ROUND(MEDIAN(observed_stint_laps), 1) AS median_stint_laps,
            ROUND(QUANTILE_CONT(observed_stint_laps, 0.90), 1) AS p90_stint_laps,
            ROUND(AVG(clean_lap_fraction) * 100.0, 1) AS mean_clean_fraction_pct,
            SUM(CASE WHEN used_tyre_set THEN 1 ELSE 0 END) AS used_tyre_stints
        FROM gold.stint_observations
        WHERE compound_relative IN ('HARD', 'MEDIUM', 'SOFT')
        GROUP BY 1, 2
        ORDER BY 1, 2
    """).df()

    by_circuit = con.execute("""
        SELECT
            circuit_short_name,
            COUNT(DISTINCT session_key) AS n_races,
            COUNT(*) AS n_stints,
            SUM(clean_laps_count) AS clean_laps,
            ROUND(AVG(observed_stint_laps), 1) AS mean_stint_laps,
            ROUND(MAX(observed_stint_laps), 0) AS max_stint_laps,
            ROUND(AVG(mean_track_temp_c), 1) AS mean_track_temp_c
        FROM gold.stint_observations
        WHERE compound_relative IN ('HARD', 'MEDIUM', 'SOFT')
        GROUP BY 1
        ORDER BY clean_laps DESC
    """).df()

    return {
        "effective_sample_size": eff_sample.to_dict(orient="records"),
        "by_absolute_compound": by_abs_compound.to_dict(orient="records"),
        "by_tyre_age_bucket": by_tyre_age.to_dict(orient="records"),
        "by_stint_termination": by_termination.to_dict(orient="records"),
        "by_circuit": by_circuit.to_dict(orient="records"),
    }


__all__ = [
    "HistoricalSupportTable",
    "basis_late_cliff",
    "basis_warm_up",
    "build_degradation_dataset_audit",
    "era_qualified_compound",
    "row_to_stint_forecast_request",
]
