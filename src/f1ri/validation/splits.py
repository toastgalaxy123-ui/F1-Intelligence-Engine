"""Chronological race-level dataset splits and rolling-origin split generators."""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class SessionSplitEntry:
    session_key: int
    year: int
    split: str
    label: str
    inspected: bool = False
    session_start_utc: datetime | None = None
    session_end_utc: datetime | None = None


@dataclass(frozen=True)
class TemporalSplitManifest:
    train: tuple[SessionSplitEntry, ...]
    val: tuple[SessionSplitEntry, ...]
    test: tuple[SessionSplitEntry, ...]
    transfer_2026: tuple[SessionSplitEntry, ...]

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        buckets = {
            "train": self.train,
            "val": self.val,
            "test": self.test,
            "transfer_2026": self.transfer_2026,
        }
        seen: dict[int, str] = {}
        for split_name, entries in buckets.items():
            for entry in entries:
                if entry.session_key in seen:
                    raise ValueError(
                        f"Session {entry.session_key} appears in both '{seen[entry.session_key]}' and '{split_name}'"
                    )
                seen[entry.session_key] = split_name

        # Verify chronological ordering across train -> val -> test -> transfer_2026
        ordered_pairs = [
            ("train", self.train, "val", self.val),
            ("val", self.val, "test", self.test),
            ("test", self.test, "transfer_2026", self.transfer_2026),
        ]
        for earlier_name, earlier, later_name, later in ordered_pairs:
            if not earlier or not later:
                continue
            max_early_year = max(e.year for e in earlier)
            min_late_year = min(e.year for e in later)
            if max_early_year > min_late_year:
                raise ValueError(
                    f"Chronological violation: '{earlier_name}' has year {max_early_year} > '{later_name}' year {min_late_year}"
                )
            early_ends = [
                e.session_end_utc or e.session_start_utc
                for e in earlier
                if (e.session_end_utc or e.session_start_utc) is not None
            ]
            late_starts = [
                e.session_start_utc
                for e in later
                if e.session_start_utc is not None
            ]
            if early_ends and late_starts and max(early_ends) > min(late_starts):
                raise ValueError(
                    f"Chronological timestamp overlap between '{earlier_name}' ({max(early_ends)}) "
                    f"and '{later_name}' ({min(late_starts)})"
                )

    @property
    def train_keys(self) -> list[int]:
        return [e.session_key for e in self.train]

    @property
    def val_keys(self) -> list[int]:
        return [e.session_key for e in self.val]

    @property
    def test_keys(self) -> list[int]:
        return [e.session_key for e in self.test]

    @property
    def untouched_test_keys(self) -> list[int]:
        return [e.session_key for e in self.test if not e.inspected]

    @property
    def transfer_2026_keys(self) -> list[int]:
        return [e.session_key for e in self.transfer_2026]

    def to_dict(self) -> dict[str, Any]:
        def _ser(items: Sequence[SessionSplitEntry]) -> list[dict[str, Any]]:
            out = []
            for item in items:
                d = asdict(item)
                for k in ("session_start_utc", "session_end_utc"):
                    if isinstance(d[k], datetime):
                        d[k] = d[k].isoformat()
                out.append(d)
            return out

        return {
            "train": _ser(self.train),
            "val": _ser(self.val),
            "test": _ser(self.test),
            "transfer_2026": _ser(self.transfer_2026),
        }


def build_split_manifest(
    cohort_entries: Sequence[dict[str, Any]],
    session_bounds: dict[int, tuple[datetime | None, datetime | None]] | None = None,
) -> TemporalSplitManifest:
    bounds = session_bounds or {}
    by_split: dict[str, list[SessionSplitEntry]] = {
        "train": [],
        "val": [],
        "test": [],
        "transfer_2026": [],
    }
    for item in cohort_entries:
        sk = int(item["session_key"])
        sp = str(item["split"])
        if sp not in by_split:
            raise ValueError(f"Unsupported split name: {sp}")
        start_utc, end_utc = bounds.get(sk, (None, None))
        by_split[sp].append(
            SessionSplitEntry(
                session_key=sk,
                year=int(item["year"]),
                split=sp,
                label=str(item.get("label", f"{item['year']}_{sk}")),
                inspected=bool(item.get("inspected", False)),
                session_start_utc=start_utc,
                session_end_utc=end_utc,
            )
        )
    return TemporalSplitManifest(
        train=tuple(by_split["train"]),
        val=tuple(by_split["val"]),
        test=tuple(by_split["test"]),
        transfer_2026=tuple(by_split["transfer_2026"]),
    )


def iter_rolling_origin_splits(
    base_train_keys: Sequence[int],
    sequential_eval_keys: Sequence[int],
) -> Iterator[tuple[list[int], int]]:
    """Yield (expanding_train_session_keys, next_eval_session_key) in chronological order."""
    history = list(base_train_keys)
    seen = set(history)
    for target_sk in sequential_eval_keys:
        if target_sk in seen:
            raise ValueError(f"Target session {target_sk} is already in training history")
        yield list(history), int(target_sk)
        history.append(int(target_sk))
        seen.add(int(target_sk))


def validate_stage4_holdout(manifest: TemporalSplitManifest, previously_inspected_keys: set[int]) -> None:
    """Stage 3 test races are already inspected when designing Stage 4."""
    manifest.validate()
    repeated = set(manifest.test_keys) & previously_inspected_keys
    if repeated or any(entry.inspected for entry in manifest.test):
        raise ValueError(f"Stage 4 final test is not newly reserved: {sorted(repeated)}")
