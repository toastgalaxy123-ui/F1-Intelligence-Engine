import json
from pathlib import Path

import pytest

from f1ri.ingestion.raw_cache import read_snapshot, save_snapshot, snapshot_path


def test_snapshot_is_immutable_without_refresh(tmp_path: Path):
    path = snapshot_path(tmp_path, 2025, "laps", 123)
    payload = b'[{"session_key":123,"driver_number":7,"lap_number":1}]'
    save_snapshot(path, payload, url="https://api.openf1.org/v1/laps", params={"session_key":123})
    assert read_snapshot(path)[0]["lap_number"] == 1
    assert json.loads(path.with_suffix(".meta.json").read_text())["records"] == 1
    with pytest.raises(FileExistsError):
        save_snapshot(path, b"[]", url="https://example.com", params={})
    save_snapshot(path, b"[]", url="https://example.com", params={}, refresh=True)
    assert read_snapshot(path) == []
    assert len([f for f in (path.parent / "history").glob("laps-*.json")
                if not f.name.endswith(".meta.json")]) == 1


def test_reject_non_list_payload(tmp_path: Path):
    with pytest.raises(ValueError):
        save_snapshot(tmp_path / "invalid.json", b'{"a":1}', url="u", params={})
