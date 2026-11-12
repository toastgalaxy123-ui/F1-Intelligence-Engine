import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from f1ri.ingestion.openf1 import OpenF1Client
from f1ri.ingestion.pit_telemetry import ingest_pit_telemetry


def test_filters_match_provider_url_parser_and_cached_window(tmp_path):
    start = datetime(2025, 1, 1, tzinfo=UTC)
    requests = []
    def handler(request):
        requests.append(request)
        params = request.url.params
        assert params['date>'] == start.isoformat()
        assert params['date<'] == (start+timedelta(seconds=60)).isoformat()
        # The upstream parser joins keys and values with '=' before parsing.
        assert f"date>={params['date>']}" == 'date>=2025-01-01T00:00:00+00:00'
        assert 'date>=' not in params
        return httpx.Response(200, json=[{'session_key': 1, 'driver_number': 2,
                                         'date': (start+timedelta(seconds=20)).isoformat(), 'speed': 80}])
    client = OpenF1Client('https://api.openf1.org/v1', min_interval_seconds=0)
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))
    with client:
        args = {'session_key': 1, 'driver_number': 2, 'pit_event_id': '1:2:1',
                'start_utc': start, 'end_utc': start+timedelta(seconds=60)}
        a = ingest_pit_telemetry(tmp_path, client, **args)
        b = ingest_pit_telemetry(tmp_path, client, **args)
        assert a == b
        assert len(requests) == 2  # second call reads both immutable local snapshots
        assert a['endpoints']['car_data']['records'] == 1
        with pytest.raises(ValueError, match='at most 180'):
            ingest_pit_telemetry(tmp_path, client, **(args | {'end_utc': start+timedelta(hours=1)}))
    for path in tmp_path.rglob('*.json'):
        json.loads(path.read_text())
