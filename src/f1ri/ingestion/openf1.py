"""HTTP-only source adapter. No data transformation or modeling belongs here."""
from __future__ import annotations

import os
import time
from pathlib import Path

import httpx

from f1ri.ingestion.raw_cache import read_snapshot, save_snapshot, snapshot_path


class OpenF1Client:
    def __init__(self, base_url: str, min_interval_seconds: float = 2.1):
        self.base_url = base_url.rstrip("/")
        self.min_interval_seconds = min_interval_seconds
        self._last_request_monotonic: float | None = None
        headers = {"User-Agent": "f1-race-intelligence/0.1 (personal research)"}
        if os.getenv("OPENF1_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['OPENF1_TOKEN']}"
        self.client = httpx.Client(timeout=90.0, headers=headers, follow_redirects=True)

    def __enter__(self) -> "OpenF1Client":
        return self

    def __exit__(self, *_: object) -> None:
        self.client.close()

    def _wait_for_rate_limit(self) -> None:
        if self._last_request_monotonic is not None:
            delta = time.monotonic() - self._last_request_monotonic
            if delta < self.min_interval_seconds:
                time.sleep(self.min_interval_seconds - delta)

    def fetch(self, endpoint: str, params: dict) -> tuple[bytes, str]:
        if endpoint not in {
            "meetings", "sessions", "drivers", "laps", "stints", "pit", "weather",
            "race_control", "session_result", "starting_grid", "position", "intervals",
            "car_data", "location", "overtakes",
        }:
            raise ValueError(f"Unsupported endpoint: {endpoint}")
        url = f"{self.base_url}/{endpoint}"
        for attempt in range(5):
            self._wait_for_rate_limit()
            try:
                self._last_request_monotonic = time.monotonic()
                response = self.client.get(url, params=params)
                if response.status_code in (401, 403):
                    raise RuntimeError(
                        f"Access denied ({response.status_code}) for {response.url}. "
                        "Live data requires authenticated OpenF1 access; use finished sessions."
                    )
                if response.status_code == 404:
                    try:
                        detail = response.json().get("detail")
                    except ValueError:
                        detail = None
                    if detail == "No results found.":
                        return b"[]", str(response.url)
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After", "60")
                    try:
                        wait = max(float(retry_after), 5.0)
                    except ValueError:
                        wait = 60.0
                    if attempt == 4:
                        response.raise_for_status()
                    time.sleep(wait)
                    continue
                if response.status_code >= 500 and attempt < 4:
                    time.sleep(min(2 ** attempt, 16))
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list):
                    raise ValueError(f"Expected a JSON array from {response.url}")
                return response.content, str(response.url)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
                if attempt == 4:
                    raise
                time.sleep(min(2 ** attempt, 16))
        raise RuntimeError(f"All attempts failed: {url}")

    def cached_get(
        self, root: Path, *, endpoint: str, year: int,
        session_key: int | None = None, params: dict | None = None,
        refresh: bool = False,
    ) -> list[dict]:
        path = snapshot_path(root, year, endpoint, session_key)
        if path.exists() and not refresh:
            return read_snapshot(path)
        query = params or ({"session_key": session_key} if session_key is not None
                           else {"year": year})
        content, url = self.fetch(endpoint, query)
        save_snapshot(path, content, url=url, params=query, refresh=refresh)
        return read_snapshot(path)
