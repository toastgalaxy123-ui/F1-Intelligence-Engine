"""Stable priority queue for a single race clock."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping
from heapq import heappop, heappush

from f1ri.simulator.events import Event, EventKind


class Scheduler:
    def __init__(self, grid: Mapping[int, int | None], *, scheduled_laps: int | None = None) -> None:
        self._grid = dict(grid)
        self._finish_checkpoint = scheduled_laps * 3 if scheduled_laps is not None else None
        self._queue: list[tuple[tuple, Event]] = []
        self._seen: set[tuple] = set()
        self._generation: dict[int, int] = defaultdict(int)
        self._last_key: tuple | None = None

    def invalidate(self, driver_number: int) -> int:
        if driver_number not in self._grid:
            raise ValueError(f"Unknown driver {driver_number}")
        self._generation[driver_number] += 1
        return self._generation[driver_number]

    def generation(self, driver_number: int) -> int:
        return self._generation[driver_number]

    def schedule(self, event: Event) -> None:
        driver = event.driver_number
        if driver is not None and driver not in self._grid:
            raise ValueError(f"Unknown driver {driver}")
        grid = self._grid.get(driver) if driver is not None else 0
        distance_crossing = (
            event.kind is EventKind.LAP_COMPLETED and event.checkpoint == self._finish_checkpoint
        )
        order = (event.at_us, event.kind, not distance_crossing, grid is None, grid or 0, driver or 0,
                 getattr(event, "attempt_id", ""))
        key = (*order, event.generation if event.generation is not None else -1)
        if self._last_key is not None and order <= self._last_key:
            raise ValueError("Event cannot precede or repeat a committed event")
        if key in self._seen:
            raise ValueError(f"Duplicate or ambiguous event {event}")
        self._seen.add(key)
        heappush(self._queue, (key, event))

    def __iter__(self) -> Iterator[Event]:
        while self._queue:
            key, event = heappop(self._queue)
            if (
                event.generation is not None
                and event.generation != self._generation[event.driver_number]
            ):
                continue
            if self._last_key is not None and key[:-1] <= self._last_key:
                raise ValueError("Ambiguous events at the same race time and precedence")
            self._last_key = key[:-1]
            yield event
