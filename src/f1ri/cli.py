"""Project CLI. Run `f1ri --help` after `pip install -e .`."""
from __future__ import annotations

import argparse
from datetime import datetime
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
    p = sub.add_parser("ingest-batch", help="Ingest, build, and validate the configured race cohort")
    p.add_argument("--config", type=Path, default=None, help="Experiment YAML path")
    p.add_argument("--refresh", action="store_true")
    p = sub.add_parser("stage2", help="Run Stage 2 dataset audit, examples build, and E0-E10 backtest")
    p.add_argument("--config", type=Path, default=None, help="Experiment YAML path")
    p = sub.add_parser("stage3", help="Run Stage 3 stint dataset audit, examples build, and D0-D10 open-loop backtest")
    p.add_argument("--config", type=Path, default=None, help="Experiment YAML path")
    p = sub.add_parser("stage4", help="Reconstruct pit events and run chronological pit-loss backtests")
    p.add_argument("--config", type=Path, default=None, help="Stage 4 experiment YAML path")
    p = sub.add_parser("stage4-stabilize", help="Stage 4.1 development, locked selection and separate reserved tests")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--phase", choices=("prepare", "validate", "evaluate", "report", "all"), default="all")
    p.add_argument("--ingest", action="store_true", help="Fetch missing snapshots; tests only after selection lock")
    p = sub.add_parser("stage5-replay", help="Reconstruct and audit a completed historical race")
    p.add_argument("--session-key", type=int, required=True)
    p.add_argument("--scheduled-laps", type=int, required=True, help="Prescribed race distance")
    p.add_argument("--warehouse", type=Path, default=Path("data/warehouse/f1ri.duckdb"))
    p.add_argument("--output", type=Path, help="Save the JSON audit at this path")
    p = sub.add_parser("stage5-s1", help="Verify and backtest frozen Pace V2 field integration")
    p.add_argument("--stage3-root", type=Path, required=True, help="Project with the saved Stage 3 run")
    p.add_argument("--warehouse", type=Path, help="Override the Stage 3 warehouse path")
    p.add_argument("--holdout-warehouse", type=Path, default=Path("data/warehouse/f1ri.duckdb"))
    p.add_argument("--config", type=Path, default=Path("config/experiments/stage5_s1_pace.yaml"))
    p.add_argument("--output", type=Path, default=Path("artifacts/runs/stage5_s1_pace"))
    p = sub.add_parser("stage5-s2", help="Validate pit execution and frozen replacement-tire pace")
    p.add_argument("--stage3-root", type=Path, required=True, help="Project with the saved Stage 3 run")
    p.add_argument("--warehouse", type=Path, help="Override the Stage 3 warehouse path")
    p.add_argument("--local-warehouse", type=Path, default=Path("data/warehouse/f1ri.duckdb"))
    p.add_argument("--config", type=Path, default=Path("config/experiments/stage5_s2_pits.yaml"))
    p.add_argument("--output", type=Path, default=Path("artifacts/runs/stage5_s2_pits"))
    p.add_argument("--p1-artifact", type=Path, help="Optional original locked Stage 4.1 P1 pickle")
    p = sub.add_parser("pit-reference-import", help="Verify and import an official FIA pit-speed directive")
    p.add_argument("--metadata", type=Path, required=True, help="Source identity, date evidence and exact source quote JSON")
    p = sub.add_parser("pit-telemetry", help="Fetch bounded optional traces for one historical pit event")
    p.add_argument("--session-key", type=int, required=True)
    p.add_argument("--driver-number", type=int, required=True)
    p.add_argument("--pit-event-id", required=True)
    p.add_argument("--start", type=datetime.fromisoformat, required=True)
    p.add_argument("--end", type=datetime.fromisoformat, required=True)
    p.add_argument("--refresh", action="store_true")
    sub.add_parser("pit-telemetry-audit", help="Optional bounded trace audit at five representative circuits")
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
    elif args.command == "ingest-batch":
        from f1ri.ingestion.batch import load_experiment_cohort, run_batch_ingestion
        cohort = load_experiment_cohort(root, args.config)
        manifest_out = None
        if args.config is not None:
            import yaml
            experiment_path = args.config if args.config.is_absolute() else root/args.config
            experiment = yaml.safe_load(experiment_path.read_text())
            name = experiment.get("experiment", {}).get("name", "stage2_pace_v1")
            manifest_out = root/"artifacts/runs"/name/"ingestion_manifest.json"
        manifest = run_batch_ingestion(root, cohort, full=True, refresh=args.refresh,
                                       manifest_out=manifest_out)
        print(
            f"Batch complete: {manifest['ready_sessions']}/{manifest['total_sessions']} ready."
        )
    elif args.command == "stage2":
        from f1ri.validation.backtest import run_stage2_pipeline
        res = run_stage2_pipeline(root, args.config)
        for exp_id, m in res["experiments"].items():
            v = m.get("val_h1", {}).get("race_weighted_mae_s")
            t = m.get("test_h1", {}).get("race_weighted_mae_s")
            tr = m.get("transfer_2026_h1", {}).get("race_weighted_mae_s")
            print(f"{exp_id:32} | val_mae={v!s:<7} | test_mae={t!s:<7} | 2026_mae={tr!s:<7}")
    elif args.command == "stage3":
        from f1ri.validation.stint_backtest import run_stage3_pipeline
        res = run_stage3_pipeline(root, args.config)
        for exp_id, m in res["experiments"].items():
            val_a = m.get("val", {}).get("dataset_a_individual_laps", {})
            test_b = m.get("test_all", {}).get("dataset_b_complete_trajectories", {})
            tr_b = m.get("transfer_2026", {}).get("dataset_b_complete_trajectories", {})
            v1 = val_a.get("h1", {}).get("race_weighted_mae_s")
            t5 = test_b.get("h5", {}).get("cum_race_weighted_mae_s")
            t10 = test_b.get("h10", {}).get("cum_race_weighted_mae_s")
            t15 = test_b.get("h15", {}).get("cum_race_weighted_mae_s")
            tr5 = tr_b.get("h5", {}).get("cum_race_weighted_mae_s")
            print(
                f"{exp_id:36} | val_h1_lap={v1!s:<6} | test_h5_cum={t5!s:<6} "
                f"| test_h10_cum={t10!s:<6} | test_h15_cum={t15!s:<6} | 2026_h5_cum={tr5!s:<6}"
            )
    elif args.command == "stage5-s2":
        from f1ri.validation.pit_integration_backtest import run_stage5_s2

        stage3 = root / args.stage3_root
        warehouse = root / args.warehouse if args.warehouse else stage3 / "data/warehouse/f1ri.duckdb"
        result = run_stage5_s2(root, stage3, warehouse, root / args.local_warehouse,
                               root / args.config, root / args.output,
                               root / args.p1_artifact if args.p1_artifact else None)
        print(f"Stage 5.2 | D8 replacement parity {result['s2_replacement_parity']} | "
              f"P1 {result['p1_status']}")
        print(root / args.output / "metrics.json")
    elif args.command == "stage5-s1":
        from f1ri.validation.pace_integration_backtest import run_stage5_s1

        stage3 = root / args.stage3_root
        warehouse = root / args.warehouse if args.warehouse else stage3 / "data/warehouse/f1ri.duckdb"
        result = run_stage5_s1(root, stage3, warehouse, root / args.holdout_warehouse,
                               root / args.config, root / args.output)
        print(f"Stage 5.1 | {len(result['parity'])} frozen parity cases | "
              f"{len(result['opportunities'])} field cutoffs | {len(result['skipped'])} skips")
        print(root / args.output / "metrics.json")
    elif args.command == "stage5-replay":
        import json

        from f1ri.simulator.replay import replay_session, replay_summary

        result = replay_session(root / args.warehouse, args.session_key, args.scheduled_laps,
                                annotations_root=root)
        audit = json.dumps(replay_summary(result), indent=2) + "\n"
        if args.output:
            (root / args.output).write_text(audit, encoding="utf-8")
        else:
            print(audit, end="")
    elif args.command == "stage4":
        from f1ri.validation.pit_backtest import run_stage4_pipeline
        result = run_stage4_pipeline(root, args.config)
        print(f"Stage 4: {result['events']} events; {result['primary_labels']} primary labels; "
              f"validation selection={result['selected_model']}; {result['completion_status']}")
        print(f"Artifacts: {result['run_directory']}")
    elif args.command == "stage4-stabilize":
        from f1ri.validation.pit_stabilization import run_stage4_stabilization
        print(run_stage4_stabilization(root, args.config, phase=args.phase, ingest=args.ingest))
    elif args.command == "pit-reference-import":
        import json

        from f1ri.ingestion.pit_reference_documents import import_fia_pit_reference
        print(import_fia_pit_reference(root, json.loads(args.metadata.read_text())))
    elif args.command == "pit-telemetry":
        from f1ri.ingestion.pit_telemetry import ingest_pit_telemetry
        if args.start.tzinfo is None or args.end.tzinfo is None:
            raise ValueError("Telemetry boundaries must include UTC offsets")
        with _client(load_config(root)) as client:
            result = ingest_pit_telemetry(
                root, client, session_key=args.session_key, driver_number=args.driver_number,
                pit_event_id=args.pit_event_id, start_utc=args.start, end_utc=args.end,
                refresh=args.refresh,
            )
        print(result)
    elif args.command == "pit-telemetry-audit":
        from f1ri.ingestion.pit_telemetry import audit_representative_pit_telemetry
        cfg = load_config(root)
        audit_representative_pit_telemetry(root, base_url=cfg["data"]["openf1_base_url"])


if __name__ == "__main__":
    main()
