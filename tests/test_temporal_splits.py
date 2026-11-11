from __future__ import annotations

from datetime import UTC, datetime

import pytest

from f1ri.validation.splits import build_split_manifest, iter_rolling_origin_splits


def test_split_manifest_rejects_duplicate_session():
    entries = [
        {"year": 2024, "session_key": 9472, "split": "train", "label": "2024 Bahrain"},
        {"year": 2024, "session_key": 9472, "split": "val", "label": "2024 Bahrain Dup"},
    ]
    with pytest.raises(ValueError, match="appears in both"):
        build_split_manifest(entries)


def test_split_manifest_rejects_chronological_violation():
    entries = [
        {"year": 2025, "session_key": 9987, "split": "train", "label": "2025 Imola"},
        {"year": 2024, "session_key": 9472, "split": "val", "label": "2024 Bahrain"},
    ]
    with pytest.raises(ValueError, match="Chronological violation"):
        build_split_manifest(entries)


def test_split_manifest_and_rolling_origin_integrity():
    entries = [
        {"year": 2023, "session_key": 7953, "split": "train", "label": "2023 Bahrain"},
        {"year": 2024, "session_key": 9472, "split": "train", "label": "2024 Bahrain"},
        {"year": 2025, "session_key": 10014, "split": "val", "label": "2025 Bahrain", "inspected": False},
        {"year": 2025, "session_key": 9912, "split": "test", "label": "2025 Monza", "inspected": False},
        {"year": 2025, "session_key": 9896, "split": "test", "label": "2025 Singapore", "inspected": True},
        {"year": 2026, "session_key": 11253, "split": "transfer_2026", "label": "2026 Suzuka"},
        {"year": 2026, "session_key": 11361, "split": "transfer_2026", "label": "2026 Monza"},
    ]
    bounds = {
        7953: (datetime(2023, 3, 5, 15, tzinfo=UTC), datetime(2023, 3, 5, 17, tzinfo=UTC)),
        9472: (datetime(2024, 3, 2, 15, tzinfo=UTC), datetime(2024, 3, 2, 17, tzinfo=UTC)),
        10014: (datetime(2025, 4, 13, 15, tzinfo=UTC), datetime(2025, 4, 13, 17, tzinfo=UTC)),
        9912: (datetime(2025, 9, 7, 13, tzinfo=UTC), datetime(2025, 9, 7, 15, tzinfo=UTC)),
        9896: (datetime(2025, 10, 5, 12, tzinfo=UTC), datetime(2025, 10, 5, 14, tzinfo=UTC)),
        11253: (datetime(2026, 3, 29, 5, tzinfo=UTC), datetime(2026, 3, 29, 7, tzinfo=UTC)),
        11361: (datetime(2026, 9, 6, 13, tzinfo=UTC), datetime(2026, 9, 6, 15, tzinfo=UTC)),
    }
    manifest = build_split_manifest(entries, bounds)
    assert manifest.train_keys == [7953, 9472]
    assert manifest.val_keys == [10014]
    assert manifest.test_keys == [9912, 9896]
    assert manifest.untouched_test_keys == [9912]

    rolling = list(iter_rolling_origin_splits([7953, 9472], [11253, 11361]))
    assert rolling == [
        ([7953, 9472], 11253),
        ([7953, 9472, 11253], 11361),
    ]
