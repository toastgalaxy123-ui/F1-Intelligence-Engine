"""Compare supported research behind the authenticated edge with the frozen release."""

from argparse import ArgumentParser
from ipaddress import ip_address
from json import loads
from pathlib import Path
from re import fullmatch
from ssl import create_default_context
from sys import stdin
from urllib.parse import urlsplit
from zipfile import ZipFile

import httpx
from verify_s8a import REPORT_DIR, measure, parity
from verify_s8a import _request as request
from verify_s8a import _wait as wait
from verify_s8b import TEAM

from f1ri.validation.historical_field_study import write_json

STAGING_ORIGIN = "https://localhost:8443"


def external_https_origin(raw):
    value = urlsplit(raw)
    hostname = value.hostname
    if (value.scheme != "https" or not hostname or value.username or value.password or
            raw != f"https://{value.netloc}" or value.netloc != value.netloc.lower() or
            any(char.isspace() for char in raw)):
        raise ValueError("Use an exact HTTPS origin without credentials, a path or query.")
    # Validate the port even if the caller supplied only a hostname.
    if value.port == 0 or value.netloc.endswith(":"):
        raise ValueError("Use a valid HTTPS port.")
    try:
        address = ip_address(hostname)
    except ValueError:
        address = None
    if hostname.rstrip(".") == "localhost" or (address and (address.is_loopback or address.is_unspecified)):
        raise ValueError("External validation requires an approved non-loopback origin.")
    return raw


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--external", action="store_true", help="Use only for an approved external origin.")
    parser.add_argument("--ca", type=Path, help="Approved CA bundle for a private trust model.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.external:
        try:
            external_https_origin(args.origin)
        except ValueError as error:
            parser.error(str(error))
    elif args.origin != STAGING_ORIGIN or not args.ca:
        parser.error("Staging uses the exact loopback HTTPS origin and its fixture CA.")
    secret = stdin.read(256).strip()
    if not fullmatch(r"[A-Za-z0-9_-]{43}", secret):
        parser.error("Supply the short-lived application session through standard input.")
    tls = create_default_context(cafile=str(args.ca) if args.ca else None)
    with ZipFile(REPORT_DIR/"stage8_s8_b_evidence.zip") as saved:
        expected = loads(saved.read("api_parity.json"))
        team_expected = loads(saved.read("team_strategy_validation.json"))["parity"]["result_sha256"]
    api = "/api/v1"
    with httpx.Client(base_url=args.origin, verify=tls, trust_env=False, timeout=18,
                      headers={"Origin": args.origin}, cookies={"__Host-f1ri-session": secret}) as client:
        _, _, performance, comparisons = measure(client)
        comparisons["export_replay"].pop("replay_job_id")
        for name, verified in comparisons.items():
            stable = ("analysis_id", "snapshot_id", "result_sha256", "completion", "recommendation", "state", "code")
            assert {k: verified[k] for k in stable if k in verified} == {
                k: expected[name][k] for k in stable if k in expected[name]}, name
        snap = request(client, "GET", f"{api}/presets/shared-box/snapshot")
        query = {"context": snap["context"], "snapshot_id": snap["snapshot_id"], "operation": "manual",
                 "max_stops": 2, "policies": TEAM}
        job = wait(client, request(client, "POST", f"{api}/analyses", json=query))
        team = parity(job["result"])
        assert team["result_sha256"] == team_expected
        traces = request(client, "GET", f"{api}/analyses/{job['id']}/details")
        assert traces["result_sha256"] == team_expected
        payload = job["result"]["payload"]["decision"]["timelines"]
        waits = [[row["wait_us"] for row in payload[k]["pit"]] for k in TEAM]
        assert waits == [[0, 2_903_848], [0, 0]]
        assert all(not row["cache_hit"] for row in performance["analysis"].values()) and not job["cache_hit"]
        write_json(args.output, {"exact_frozen_results": True, "comparisons": comparisons,
                                 "team": team | {"service_wait_us": waits},
                                 "timing_regret_us": 46_754_185,
                                 "performance": performance | {"team": job["measurements"]}})


if __name__ == "__main__":
    main()
