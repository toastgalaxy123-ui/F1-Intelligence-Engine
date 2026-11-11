from __future__ import annotations

from pathlib import Path

import yaml


def load_config(root: Path) -> dict:
    path = root / "config" / "default.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Cannot find {path}. Run commands from the project root.")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)
