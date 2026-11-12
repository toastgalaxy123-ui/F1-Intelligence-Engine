import json

import duckdb

from f1ri.ingestion.raw_cache import snapshot_path
from f1ri.warehouse.build import build_warehouse
from f1ri.warehouse.schema import SILVER_SCHEMAS


def test_pit_tables_rebuild_idempotently_and_deprecated_alias(pit_frames, tmp_path):
    frames = pit_frames
    # Serialize a real source-shaped minimal session; all model context is rebuilt.
    for endpoint in SILVER_SCHEMAS:
        source = frames.get(endpoint)
        records = source.to_dict('records') if source is not None else []
        if endpoint == 'pit':
            records[0]['pit_duration'] = records[0].pop('lane_duration')
            records[0]['stop_duration'] = None
        path = snapshot_path(tmp_path, 2025, endpoint, 701)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records, default=str))
    database = build_warehouse(tmp_path, 2025, 701)
    def read():
        with duckdb.connect(str(database), read_only=True) as con:
            return con.execute('SELECT pit_event_id, resolved_in_lap, resolved_out_lap, lane_duration_s, stop_duration_s FROM gold.pit_events').fetchall()
    first = read()
    assert first == [('701:1:1', 10, 11, 26., None)]
    build_warehouse(tmp_path, 2025, 701)
    assert first == read()
