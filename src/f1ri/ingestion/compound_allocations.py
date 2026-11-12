"""Load, validate, and version Pirelli weekend compound allocations (C1-C6)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

VALID_RELATIVE_COMPOUNDS = {"HARD", "MEDIUM", "SOFT"}
VALID_ABSOLUTE_2023_2025 = {"C1", "C2", "C3", "C4", "C5", "C6"}
VALID_ABSOLUTE_2026_PLUS = {"C1", "C2", "C3", "C4", "C5"}
COMPOUND_HARDNESS_ORDER = {"C1": 1, "C2": 2, "C3": 3, "C4": 4, "C5": 5, "C6": 6}


def default_allocations_csv_path(root: Path) -> Path:
    return root / "data" / "raw" / "reference" / "pirelli_allocations.csv"


def load_and_validate_compound_allocations(csv_path: Path) -> pd.DataFrame:
    """Load curated Pirelli weekend compound mappings and enforce physical/temporal invariants."""
    if not csv_path.exists():
        raise FileNotFoundError(f"Pirelli compound allocation file not found: {csv_path}")

    df = pd.read_csv(csv_path)
    required_cols = {
        "meeting_key",
        "season",
        "relative_compound",
        "absolute_compound",
        "regulation_era",
        "source_url",
        "source_publication_date",
        "mapping_confidence",
        "available_as_of_utc",
    }
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns in {csv_path}: {sorted(missing)}")

    if df.empty:
        return df

    df = df.copy()
    df["meeting_key"] = df["meeting_key"].astype(int)
    df["season"] = df["season"].astype(int)
    df["relative_compound"] = df["relative_compound"].astype(str).str.strip().str.upper()
    df["absolute_compound"] = df["absolute_compound"].astype(str).str.strip().str.upper()
    df["regulation_era"] = df["regulation_era"].astype(str).str.strip()
    df["mapping_confidence"] = df["mapping_confidence"].astype(str).str.strip().str.upper()
    df["available_as_of_utc"] = pd.to_datetime(df["available_as_of_utc"], utc=True)

    invalid_rel = set(df["relative_compound"]) - VALID_RELATIVE_COMPOUNDS
    if invalid_rel:
        raise ValueError(f"Invalid relative_compound values: {sorted(invalid_rel)}")

    # Check duplicates on (meeting_key, relative_compound)
    dups = df.duplicated(subset=["meeting_key", "relative_compound"], keep=False)
    if dups.any():
        bad_keys = df.loc[dups, ["meeting_key", "relative_compound"]].to_dict(orient="records")
        raise ValueError(f"Duplicate (meeting_key, relative_compound) rows: {bad_keys}")

    # Validate era-specific compound codes and hardness ordering within each meeting
    for meeting_key, grp in df.groupby("meeting_key"):
        era = str(grp["regulation_era"].iloc[0])
        allowed_abs = (
            VALID_ABSOLUTE_2026_PLUS if era == "2026_PLUS" else VALID_ABSOLUTE_2023_2025
        )
        bad_abs = set(grp["absolute_compound"]) - allowed_abs
        if bad_abs:
            raise ValueError(
                f"Meeting {meeting_key} (era={era}) has invalid absolute_compound: {sorted(bad_abs)}"
            )

        rel_to_abs = dict(zip(grp["relative_compound"], grp["absolute_compound"], strict=False))
        if {"HARD", "MEDIUM", "SOFT"}.issubset(rel_to_abs.keys()):
            h_idx = COMPOUND_HARDNESS_ORDER[rel_to_abs["HARD"]]
            m_idx = COMPOUND_HARDNESS_ORDER[rel_to_abs["MEDIUM"]]
            s_idx = COMPOUND_HARDNESS_ORDER[rel_to_abs["SOFT"]]
            if not (h_idx < m_idx < s_idx):
                raise ValueError(
                    f"Meeting {meeting_key} violates hardness order HARD < MEDIUM < SOFT: {rel_to_abs}"
                )

    return df


def populate_silver_compound_allocations(
    con: duckdb.DuckDBPyConnection,
    root: Path,
    csv_path: Path | None = None,
) -> int:
    """Create and populate silver.compound_allocations from the curated reference CSV."""
    con.execute("CREATE SCHEMA IF NOT EXISTS silver")
    con.execute("""
        CREATE TABLE IF NOT EXISTS silver.compound_allocations (
            meeting_key BIGINT,
            season INTEGER,
            circuit_short_name VARCHAR,
            relative_compound VARCHAR,
            absolute_compound VARCHAR,
            regulation_era VARCHAR,
            source_url VARCHAR,
            source_publication_date VARCHAR,
            mapping_confidence VARCHAR,
            available_as_of_utc TIMESTAMPTZ
        )
    """)
    target_csv = csv_path or default_allocations_csv_path(root)
    if not target_csv.exists():
        return 0

    df = load_and_validate_compound_allocations(target_csv)
    if "circuit_short_name" not in df.columns:
        df["circuit_short_name"] = None

    ordered_cols = [
        "meeting_key",
        "season",
        "circuit_short_name",
        "relative_compound",
        "absolute_compound",
        "regulation_era",
        "source_url",
        "source_publication_date",
        "mapping_confidence",
        "available_as_of_utc",
    ]
    df = df[ordered_cols]

    con.execute("DELETE FROM silver.compound_allocations")
    if not df.empty:
        con.register("_incoming_allocations", df)
        try:
            con.execute("""
                INSERT INTO silver.compound_allocations
                SELECT
                    TRY_CAST(meeting_key AS BIGINT),
                    TRY_CAST(season AS INTEGER),
                    CAST(circuit_short_name AS VARCHAR),
                    CAST(relative_compound AS VARCHAR),
                    CAST(absolute_compound AS VARCHAR),
                    CAST(regulation_era AS VARCHAR),
                    CAST(source_url AS VARCHAR),
                    CAST(source_publication_date AS VARCHAR),
                    CAST(mapping_confidence AS VARCHAR),
                    TRY_CAST(available_as_of_utc AS TIMESTAMPTZ)
                FROM _incoming_allocations
            """)
        finally:
            con.unregister("_incoming_allocations")
    return len(df)


def resolve_compound_allocation(
    allocations_df: pd.DataFrame,
    meeting_key: int | None,
    relative_compound: str | None,
    as_of_utc: datetime | pd.Timestamp | None = None,
) -> dict[str, Any]:
    """Point-in-time lookup of actual Pirelli compound code (C1-C6) for a meeting and relative tire.

    Never infers missing mappings from observed lap times; returns None for unknown or future-dated
    mappings.
    """
    if (
        allocations_df is None
        or allocations_df.empty
        or meeting_key is None
        or pd.isna(meeting_key)
        or not relative_compound
    ):
        return {
            "compound_absolute": None,
            "compound_mapping_source": "UNMAPPED",
            "compound_mapping_confidence": "MISSING",
        }

    rel = str(relative_compound).strip().upper()
    if rel not in VALID_RELATIVE_COMPOUNDS:
        return {
            "compound_absolute": None,
            "compound_mapping_source": "NON_DRY_TIRE",
            "compound_mapping_confidence": "NOT_APPLICABLE",
        }

    sub = allocations_df[
        (allocations_df["meeting_key"] == int(meeting_key))
        & (allocations_df["relative_compound"] == rel)
    ]
    if sub.empty:
        return {
            "compound_absolute": None,
            "compound_mapping_source": "UNMAPPED",
            "compound_mapping_confidence": "MISSING",
        }

    row = sub.iloc[0]
    avail_ts = pd.Timestamp(row["available_as_of_utc"])
    if avail_ts.tzinfo is None:
        avail_ts = avail_ts.tz_localize("UTC")
    else:
        avail_ts = avail_ts.tz_convert("UTC")

    if as_of_utc is not None and pd.notna(as_of_utc):
        query_ts = pd.Timestamp(as_of_utc)
        if query_ts.tzinfo is None:
            query_ts = query_ts.tz_localize("UTC")
        else:
            query_ts = query_ts.tz_convert("UTC")
        if avail_ts > query_ts:
            return {
                "compound_absolute": None,
                "compound_mapping_source": "FUTURE_PUBLICATION_BLOCKED",
                "compound_mapping_confidence": "MISSING",
            }

    conf = str(row.get("mapping_confidence") or "VERIFIED").upper()
    if conf != "VERIFIED":
        return {
            "compound_absolute": None,
            "compound_mapping_source": str(row.get("source_url") or "UNVERIFIED"),
            "compound_mapping_confidence": conf,
        }

    return {
        "compound_absolute": str(row["absolute_compound"]).upper(),
        "compound_mapping_source": str(row.get("source_url") or "PIRELLI_REFERENCE"),
        "compound_mapping_confidence": conf,
    }


def era_qualified_compound(
    regulation_era: str | None,
    compound_absolute: str | None,
    compound_relative: str | None,
) -> str:
    """Return an era-separated compound key so 2025 C3 and 2026 C3 are never pooled as identical."""
    era = str(regulation_era or "2023_2025")
    if compound_absolute and str(compound_absolute).upper() not in ("NONE", "UNKNOWN", "NAN"):
        return f"{era}__{str(compound_absolute).upper()}"
    rel = str(compound_relative or "MEDIUM").upper()
    return f"{era}__REL_{rel}"
