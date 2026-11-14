"""Fill only the immutable Imola S5-B source windows."""
from pathlib import Path

import yaml

from f1ri.ingestion.openf1 import OpenF1Client
from f1ri.ingestion.raw_cache import read_snapshot, save_snapshot


def main():
    root = Path(__file__).resolve().parents[1]
    plan = root / "config/experiments/stage5_s5_b_historical_neutralization_validation.yaml"
    spec = yaml.safe_load(plan.read_bytes())["acquisition"]
    windows = spec["windows"]
    if len(windows) != spec["logical_request_budget"] or spec["session"] != 9987:
        raise ValueError("Acquisition exceeds the frozen Imola study")
    with OpenF1Client("https://api.openf1.org/v1") as client:
        for endpoint, name, clock, start, end in windows:
            query = {"session_key": spec["session"], clock + ">": start, clock + "<": end}
            path = root / "data/telemetry/s5_b/9987" / name / f"{endpoint}.json"
            if not path.exists():
                payload, url = client.fetch(endpoint, query)
                save_snapshot(path, payload, url=url, params=query)
            print(name, len(read_snapshot(path)), flush=True)


if __name__ == "__main__":
    main()
