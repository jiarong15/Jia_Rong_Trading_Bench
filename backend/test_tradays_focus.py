from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone, timedelta
import json
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .tradays_focus import TradaysFocusService, parse_targets


def widget(today=date(2026, 10, 30), rows=None):
    before, after = today-timedelta(days=1), today+timedelta(days=1)
    start = int(datetime.combine(before, datetime.min.time(), timezone.utc).timestamp()*1000)
    end = int(datetime.combine(after, datetime.max.time().replace(microsecond=0), timezone.utc).timestamp()*1000)
    if rows is None:
        rows = [{'Id': 10, 'Importance': 'high', 'ReleaseDate': int(datetime(2026, 10, 29, 16, 30, tzinfo=timezone.utc).timestamp()*1000)},
                {'Id': 9, 'Importance': 'high', 'ReleaseDate': int(datetime(2026, 10, 29, 1, tzinfo=timezone.utc).timestamp()*1000)}]
    return f'<script>Calendar.checkMode("selectInterval", 0,{start},{end});Calendar.Data = {json.dumps(rows)};</script>'


def test_targets_use_singapore_dates_and_first_chronological_release():
    rows = json.loads(widget().split('Calendar.Data = ')[1].split(';</script>')[0])
    rows.append({**rows[0], 'Id': 11, 'ReleaseDate': rows[0]['ReleaseDate']+1000})
    assert parse_targets(widget(rows=rows), date(2026, 10, 30)) == {'2026-10-29': 'prevValue9', '2026-10-30': 'prevValue10', '2026-10-31': None}


@pytest.mark.parametrize('body', ['<html>access denied</html>', widget(today=date(2026, 10, 31)),
                                    widget(rows=[{'Id': 'javascript:bad', 'Importance': 'high', 'ReleaseDate': 1}]),
                                    widget(rows=[{'Id': 1, 'Importance': 'low', 'ReleaseDate': 1}])])
def test_invalid_source_never_becomes_an_empty_date(body):
    with pytest.raises(ValueError): parse_targets(body, date(2026, 10, 30))


def test_shared_cache_midnight_rollover_and_failure_retry():
    now = [datetime(2026, 10, 30, 15, 59, tzinfo=timezone.utc)]
    calls = []
    def transport(today):
        calls.append(today)
        return widget(today=today, rows=[])
    service = TradaysFocusService(transport=transport, clock=lambda: now[0])
    with ThreadPoolExecutor(max_workers=5) as pool:
        assert all(value['status'] == 'ready' for value in pool.map(lambda _: service.view(), range(5)))
    assert len(calls) == 1
    result = service.view();result['targets'].clear()
    assert len(service.view()['targets']) == 3
    now[0] += timedelta(minutes=1)
    assert service.view()['today'] == '2026-10-31' and len(calls) == 2
    now[0] += timedelta(minutes=5)
    def fail(_):
        calls.append('failed')
        raise OSError('TEST sensitive proxy error')
    service.transport = fail
    assert service.view()['status'] == 'unavailable'
    assert 'sensitive' not in service.view()['error']
    assert len(calls) == 3
    now[0] += timedelta(seconds=30)
    service.view()
    assert len(calls) == 4


def test_read_only_endpoint_no_store_and_isolated_app_has_no_network(tmp_path):
    app = create_app(str(tmp_path/'test.sqlite3'))
    assert not app.state.tradays_focus.enabled
    client = TestClient(app)
    result = client.get('/api/calendar/tradays/focus')
    assert result.status_code == 200 and result.json()['status'] == 'disabled'
    assert result.headers['cache-control'] == 'no-store'
    assert client.post('/api/calendar/tradays/focus').status_code == 405
