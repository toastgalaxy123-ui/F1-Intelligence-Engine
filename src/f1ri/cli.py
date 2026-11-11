"""Project CLI. Run `f1ri --help` after `pip install -e .`."""
from __future__ import annotations

import argparse
from pathlib import Path

from f1ri.config import load_config
from f1ri.ingestion.openf1 import OpenF1Client


def _client(config: dict) -> OpenF1Client:
    return OpenF1Client(
        config["data"]["openf1_base_url"],
        config["data"]["min_seconds_between_requests"],
    )


def discover(root: Path, year: int, country: str | None, refresh: bool) -> None:
    cfg = load_config(root)
    with _client(cfg) as client:
        meetings = client.cached_get(root, endpoint="meetings", year=year, refresh=refresh)
        sessions = client.cached_get(root, endpoint="sessions", year=year, refresh=refresh)
    by_key = {m["meeting_key"]: m for m in meetings}
    entries = [s for s in sessions if s.get("session_name") == "Race"]
    entries.sort(key=lambda s: s.get("date_start") or "")
    for session in entries:
        event = by_key.get(session.get("meeting_key"), {})
        location = event.get("country_name") or session.get("country_name") or "?"
        if country and country.casefold() not in location.casefold():
            continue
        print(f"{year} | {location:24} | session_key={session['session_key']} "
              f"| {session.get('date_start')}")


def ingest(root: Path, year: int, session_key: int, full: bool, refresh: bool) -> None:
    cfg = load_config(root)
    with _client(cfg) as client:
        info = client.cached_get(root, endpoint="sessions", year=year,
                                 session_key=session_key,
                                 params={"session_key": session_key}, refresh=refresh)
        if len(info) != 1:
            raise ValueError(f"Expected one session, got {len(info)}")
        if int(info[0]["year"]) != year or info[0].get("session_name") != "Race":
            raise ValueError("This command expects a year-matched Grand Prix Race session")
        meeting_key = info[0].get("meeting_key")
        endpoints = list(cfg["data"]["core_endpoints"])
        if full:
            endpoints += cfg["data"]["additional_endpoints"]
        for ep in endpoints:
            params = None
            if ep == "starting_grid" and meeting_key is not None:
                # OpenF1 keys starting_grid by the Qualifying session_key of the meeting.
                sessions = client.cached_get(root, endpoint="sessions", year=year, refresh=False)
                qual = [
                    s for s in sessions
                    if s.get("meeting_key") == meeting_key and s.get("session_name") == "Qualifying"
                ]
                if len(qual) == 1:
                    params = {"session_key": qual[0]["session_key"]}
            records = client.cached_get(root, endpoint=ep, year=year,
                                        session_key=session_key, params=params, refresh=refresh)
            print(f"{ep:16} {len(records):>6} records")
    print("Raw snapshots saved. Next: f1ri build --year ... --session-key ...")


def main() -> None:
    parser = argparse.ArgumentParser(prog="f1ri", description="F1 Race Intelligence starter CLI")
    parser.add_argument("--root", type=Path, default=Path.cwd(),
                        help="Project root directory (default: working directory)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("discover", help="Find official OpenF1 numerical race session keys")
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--country")
    p.add_argument("--refresh", action="store_true")
    p = sub.add_parser("ingest", help="Save source JSON snapshots for a completed GP race")
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--session-key", type=int, required=True)
    p.add_argument("--full", action="store_true", help="Also ingest position and intervals")
    p.add_argument("--refresh", action="store_true", help="Explicitly replace local source snapshots")
    p = sub.add_parser("build", help="Build typed silver tables and observational gold lap mart")
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--session-key", type=int, required=True)
    p = sub.add_parser("check", help="Run core data-quality checks")
    p.add_argument("--session-key", type=int, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "discover":
        discover(root, args.year, args.country, args.refresh)
    elif args.command == "ingest":
        ingest(root, args.year, args.session_key, args.full, args.refresh)
    elif args.command == "build":
        from f1ri.warehouse.build import build_warehouse
        output = build_warehouse(root, args.year, args.session_key)
        print(f"Built {output}")
    elif args.command == "check":
        from f1ri.validation.quality import assert_valid, check_session
        result = check_session(root, args.session_key)
        for key, val in result.items():
            print(f"{key}: {val}")
        assert_valid(result)
        print("Passed critical invariants; see docs for remaining labeling limitations")


if __name__ == "__main__":
    main()
