"""Pace sources indexed by driver, tyre set and stint age."""
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite


class PaceMode(StrEnum):
    FULL_MODEL = "FULL_MODEL"
    FROZEN_PACE_REPLAY = "FROZEN_PACE_REPLAY"
    DECLARED_SYNTHETIC = "DECLARED_SYNTHETIC"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class PaceCurve:
    driver: int
    set_id: str
    initial_age: int
    lap_seconds: tuple[float, ...]
    support: str
    start_lap: int | None = None

    def __post_init__(self):
        if (self.driver < 1 or self.initial_age < 0 or not self.set_id or not self.support
                or not self.lap_seconds or any(not isfinite(x) or x <= 0 for x in self.lap_seconds)
                or self.start_lap is not None and self.start_lap < 1):
            raise ValueError("Invalid pace curve")

    def seconds(self, age, lap):
        i = age-self.initial_age
        if not 0 <= i < len(self.lap_seconds):
            raise ValueError(f"Pace horizon exhausted for car {self.driver}, set {self.set_id}, age {age}")
        if self.start_lap is not None and lap != self.start_lap + i:
            raise ValueError(f"Forecast start lap mismatch for car {self.driver}, set {self.set_id}")
        return self.lap_seconds[i]


@dataclass(frozen=True, slots=True)
class PaceSource:
    mode: PaceMode
    curves: tuple[PaceCurve, ...]
    source_hashes: tuple[tuple[str, str], ...] = ()
    stream_id: str | None = None
    provenance: str = ""
