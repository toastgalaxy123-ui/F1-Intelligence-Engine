import hashlib
import json
from pathlib import Path


def test_stage3_dependency_files_are_preserved():
    root = Path(__file__).resolve().parents[1]
    frozen = json.loads((root/'config/experiments/stage3_frozen_reference.json').read_text())
    for file, expected in frozen['files_sha256'].items():
        assert hashlib.sha256((root/file).read_bytes()).hexdigest() == expected, file
