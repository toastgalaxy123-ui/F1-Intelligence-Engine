"""HTTP-only source adapter. No data transformation or modeling belongs here."""
from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

import httpx

from f1ri.ingestion.raw_cache import read_snapshot, save_snapshot, snapshot_path


class OpenF1Client:
    ENDPOINTS = frozenset({
        "meetings", "sessions", "drivers", "laps", "stints", "pit", "weather",
        "race_control", "session_result", "starting_grid", "position", "intervals",
        "car_data", "location", "overtakes",
    })
    def __init__(self, base_url: str, min_interval_seconds: float = 2.1):
        self.base_url = base_url.rstrip("/")
        self.min_interval_seconds = min_interval_seconds
        self._last_request_monotonic: float | None = None
        headers = {"User-Agent": "f1-race-intelligence/0.1 (personal research)"}
        if os.getenv("OPENF1_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['OPENF1_TOKEN']}"
        self.client = httpx.Client(timeout=90.0, headers=headers, follow_redirects=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.client.close()

    def _wait_for_rate_limit(self) -> None:
        if self._last_request_monotonic is not None:
            delta = time.monotonic() - self._last_request_monotonic
            if delta < self.min_interval_seconds:
                time.sleep(self.min_interval_seconds - delta)

    def fetch(self, endpoint: str, params: dict) -> tuple[bytes, str]:
        if endpoint not in self.ENDPOINTS:
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
                    raise ValueError(f"Expected a JSON array from {response.url}")  # noqa: TRY004
                return response.content, str(response.url)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
                if attempt == 4:
                    raise
                time.sleep(min(2 ** attempt, 16))
        raise RuntimeError(f"All attempts failed: {url}")

    def capture_attempt(self, endpoint, params, *, deadline, max_bytes, started):
        """Stream one bounded attempt through the existing OpenF1 transport."""
        if endpoint not in self.ENDPOINTS:
            raise ValueError(f"Unsupported endpoint: {endpoint}")
        delay = max(0., self.min_interval_seconds-(time.monotonic()-self._last_request_monotonic)) \
            if self._last_request_monotonic is not None else 0.
        if time.monotonic()+delay >= deadline:
            return None
        if delay:
            time.sleep(delay)
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            return None
        start = datetime.now(UTC)
        start_ns = time.monotonic_ns()
        started(start, start_ns)
        self._last_request_monotonic = start_ns/1e9
        timeout = min(3., remaining)
        received = bytearray()
        status = first_byte = error = None
        headers = {}
        try:
            # Do not follow redirects here: every transport attempt must be
            # accounted for in the capture request budget.
            with self.client.stream("GET", f"{self.base_url}/{endpoint}", params=params,
                                    headers={"Accept-Encoding": "identity"}, timeout=timeout,
                                    follow_redirects=False) as response:
                status = response.status_code
                headers = {k: response.headers[k] for k in (
                    "date", "age", "etag", "last-modified", "retry-after", "content-type",
                    "content-encoding", "content-length", "content-range", "link",
                ) if k in response.headers}
                for chunk in response.iter_raw():
                    if first_byte is None and chunk:
                        first_byte = datetime.now(UTC)
                    if time.monotonic() >= deadline:
                        error = "CAPTURE_DEADLINE"
                        break
                    if len(received)+len(chunk) > max_bytes:
                        received.extend(chunk[:max_bytes-len(received)])
                        error = "BODY_LIMIT"
                        break
                    received.extend(chunk)
        except KeyboardInterrupt:
            error = "INTERRUPTED"
        except httpx.RequestError as exc:
            error = type(exc).__name__
        return {"request_utc": start, "request_ns": start_ns,
                "first_byte_utc": first_byte, "completed_utc": datetime.now(UTC),
                "completed_ns": time.monotonic_ns(), "http_status": status,
                "headers": headers, "error": error}, bytes(received)

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
