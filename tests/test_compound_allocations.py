from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import duckdb

from f1ri.ingestion.compound_allocations import (
    default_allocations_csv_path,
    era_qualified_compound,
    load_and_validate_compound_allocations,
    populate_silver_compound_allocations,
    resolve_compound_allocation,
)


def test_compound_allocations_weekend_c1_c6_mapping_and_era_separation():
    root = Path(__file__).resolve().parents[1]
    csv_path = default_allocations_csv_path(root)
    df = load_and_validate_compound_allocations(csv_path)
    assert len(df) >= 96
    assert set(df["relative_compound"].unique()) == {"HARD", "MEDIUM", "SOFT"}
    assert set(df["absolute_compound"].unique()).issubset({"C1", "C2", "C3", "C4", "C5", "C6"})

    # Verify era separation between 2023-2025 and 2026+
    eras_pre_2026 = set(df[df["season"] <= 2025]["regulation_era"].unique())
    eras_2026 = set(df[df["season"] == 2026]["regulation_era"].unique())
    assert eras_pre_2026 == {"2023_2025"}
    assert eras_2026 == {"2026_PLUS"}
    assert eras_pre_2026.isdisjoint(eras_2026)

    # Era-qualified compound keys never pool 2025 C3 with 2026 C3
    assert era_qualified_compound("2023_2025", "C3", "MEDIUM") != era_qualified_compound(
        "2026_PLUS", "C3", "MEDIUM"
    )

    # Test populating DuckDB silver.compound_allocations
    con = duckdb.connect(":memory:")
    rows = populate_silver_compound_allocations(con, root)
    assert rows == len(df)

    # Check specific weekend mapping (e.g. 2024 Bahrain meeting_key=1229: C1/C2/C3)
    t_race = datetime(2024, 3, 2, 15, 0, 0, tzinfo=UTC)
    m_hard = resolve_compound_allocation(df, 1229, "HARD", t_race)
    m_med = resolve_compound_allocation(df, 1229, "MEDIUM", t_race)
    m_soft = resolve_compound_allocation(df, 1229, "SOFT", t_race)
    assert m_hard["compound_absolute"] == "C1"
    assert m_med["compound_absolute"] == "C2"
    assert m_soft["compound_absolute"] == "C3"
    assert m_hard["compound_mapping_confidence"] == "VERIFIED"


def test_compound_allocations_missing_and_future_dated_fallback():
    root = Path(__file__).resolve().parents[1]
    df = load_and_validate_compound_allocations(default_allocations_csv_path(root))

    # Query before available_as_of_utc -> must not leak future-dated allocation!
    t_too_early = datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC)
    res_future = resolve_compound_allocation(df, 1229, "SOFT", t_too_early)
    assert res_future["compound_absolute"] is None
    assert res_future["compound_mapping_source"] == "FUTURE_PUBLICATION_BLOCKED"

    # Query missing meeting_key -> safe fallback
    t_valid = datetime(2025, 6, 1, 0, 0, 0, tzinfo=UTC)
    res_missing = resolve_compound_allocation(df, 999999, "MEDIUM", t_valid)
    assert res_missing["compound_absolute"] is None
    assert res_missing["compound_mapping_source"] == "UNMAPPED"

    # Query wet/intermediate compound -> safe fallback
    res_inter = resolve_compound_allocation(df, 1229, "INTERMEDIATE", t_valid)
    assert res_inter["compound_absolute"] is None
    assert res_inter["compound_mapping_source"] == "NON_DRY_TIRE"
