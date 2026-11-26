from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
from urllib.parse import parse_qs, urlparse
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .treasury_yields import TENORS, TreasuryYieldService, analytics, api_download, csv_records, download, poll_interval, validate_rows
from .config import load_backend_env

CSV = ('observation_date,DGS3MO,DGS2,DGS5,DGS10,DGS30\n'
       '2026-10-27,4.22,4.70,4.82,4.99,5.30\n'
       '2026-10-28,4.18,4.67,4.85,5.05,5.37\n')
NOW = datetime(2026, 10, 30, 7, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch):
    monkeypatch.delenv('FRED_API_KEY', raising=False)
    monkeypatch.delenv('BONDS_FRED_API_KEY', raising=False)


def service(tmp_path, transport=None):
    clock = [NOW]
    calls = []
    def fetch(url):
        calls.append(url)
        return transport(url) if transport else CSV
    return TreasuryYieldService(tmp_path/'treasury.json', transport=fetch, clock=lambda: clock[0]), clock, calls


def test_exact_spreads_butterflies_and_yield_change_units(tmp_path):
    source, _, _ = service(tmp_path)
    result = source.view()
    assert result['status'] == 'ready' and result['latest_observation_date'] == '2026-10-28'
    assert {item['key']: item['value'] for item in result['metrics']} == {
        '2s5s': 18, '2s10s': 38, '3m10s': 87, '5s30s': 52, '5s10s': 20, '10s30s': 32,
        '2s5s10s': -2, '2s10s30s': 6}
    assert {item['key']: item['change_bp'] for item in result['quotes']} == {'m3': -4, 'y2': -3, 'y5': 3, 'y10': 6, 'y30': 7}
    assert result['curve_date'] == '2026-10-28' and result['previous_curve_date'] == '2026-10-27'
    assert [item['years'] for item in result['curve']] == [.25, 2, 5, 10, 30]
    assert result['metrics'][0]['change_bp'] == 9


def test_missing_legs_and_holidays_never_get_filled(tmp_path):
    extra = '2026-10-29,,4.70,4.88,5.10,\n2026-10-30,,,,,\n'
    source, _, _ = service(tmp_path, lambda _: CSV+extra)
    result = source.view();partial, holiday = result['rows'][-2:]
    assert partial['2s5s'] == 18
    assert partial['2s10s'] == 40 and partial['2s5s10s'] == -4
    assert all(partial[key] is None for key in ('3m10s', '5s30s', '10s30s', '2s10s30s'))
    assert all(holiday[key] is None for key in ('m3', 'y2', 'y5', 'y10', 'y30', '2s5s', '2s10s', '2s5s10s'))
    assert result['curve_date'] == '2026-10-28'
    assert result['quotes'][0]['date'] == '2026-10-28'
    assert result['quotes'][1]['date'] == '2026-10-29'


def test_2s5s_preserves_zero_inversion_and_gaps_in_either_leg():
    levels = [(4, 4.2), (None, 4.2), (4, None), (4, 4), (4, 3.9)]
    rows = [{'date': f'2026-10-{day}', 'm3': None, 'y2': y2, 'y5': y5,
             'y10': None, 'y30': None} for day, (y2, y5) in enumerate(levels, 26)]
    view = analytics(rows)
    assert [row['2s5s'] for row in view['rows']] == [20, None, None, 0, -10]
    metric = next(item for item in view['metrics'] if item['key'] == '2s5s')
    assert metric['value'] == -10 and metric['change_bp'] == -10
    assert metric['date'] == '2026-10-30' and metric['previous_date'] == '2026-10-29'


def test_parallel_yield_shift_leaves_curve_shapes_unchanged():
    rows, _ = download(lambda _: CSV, NOW.date())
    shifted = [{**row, **{key: row[key]+1 for key, *_ in TENORS}} for row in rows]
    a, b = analytics(rows), analytics(shifted)
    assert [metric['value'] for metric in a['metrics']] == [metric['value'] for metric in b['metrics']]


def test_single_series_fallback_keeps_native_dates_and_missing_observations():
    calls = []
    def fetch(url):
        ids = parse_qs(urlparse(url).query)['id'][0].split(',');calls.extend(ids)
        if len(ids)>1: raise OSError('TEST bundle interruption')
        identifier = ids[0]
        all_ids = [item[2] for item in TENORS]
        index = all_ids.index(identifier)+1
        lines = CSV.strip().splitlines()
        return 'observation_date,'+identifier+'\n'+'\n'.join(','.join([cells[0], cells[index]]) for cells in (line.split(',') for line in lines[1:]))+'\n'
    rows, route = download(fetch, NOW.date())
    assert len(rows)==2 and rows[-1]['y10']==5.05
    assert 'individual' in route and len(calls)==10


@pytest.mark.parametrize('body', [CSV.replace('DGS30','WRONG'), CSV+CSV.split('\n')[1]+'\n', CSV.replace('4.67','NaN'), CSV.replace('4.67','Infinity'), CSV+'2026-11-01,4,4,4,4,4\n', CSV+'2026-10-29,4\n'])
def test_invalid_schema_dates_and_values_are_rejected(body):
    with pytest.raises((ValueError, TypeError)):
        csv_records(body, [item[2] for item in TENORS], NOW.date())


def test_shared_cache_force_refresh_revisions_and_outage_backoff(tmp_path):
    source, clock, calls = service(tmp_path)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: source.view(), range(5)))
    assert len(calls)==1 and all(value['status']=='ready' for value in results)
    original = source.view()['last_checked_at']
    clock[0] += timedelta(seconds=1)
    source.transport = lambda _: CSV.replace('4.67','4.66')
    assert source.view(refresh=True)['rows'][-1]['y2']==4.66
    assert source.view()['last_checked_at'] != original
    checked = source.view()['last_checked_at']
    clock[0] += timedelta(minutes=5)
    failed = []
    def fail(_):
        failed.append(True)
        raise OSError('TEST secret proxy configuration')
    source.transport = fail
    result = source.view()
    assert result['status']=='stale' and result['last_checked_at']==checked
    assert result['rows'][-1]['y2']==4.66 and 'secret' not in json.dumps(result)
    count = len(failed)
    source.view(refresh=True)
    assert len(failed)==count
    clock[0] += timedelta(seconds=30)
    source.transport = lambda _: CSV
    assert source.view()['status']=='ready'


def test_restart_preserves_original_dates_and_rejects_regressed_source(tmp_path):
    source, _, _ = service(tmp_path);first=source.view()
    def fail(_):raise OSError('TEST outage')
    restarted=TreasuryYieldService(source.path,transport=fail,clock=lambda: NOW+timedelta(hours=1))
    assert restarted.view()['last_checked_at']==first['last_checked_at']
    assert restarted.view()['status']=='stale'
    restarted.transport=lambda _: CSV.replace('2026-10-28','2026-10-26')
    restarted.last_finished=NOW
    result=restarted.view(refresh=True)
    assert result['status']=='stale' and result['latest_observation_date']=='2026-10-28'


def test_download_latency_does_not_delay_the_next_scheduled_source_check(tmp_path):
    source, clock, calls = service(tmp_path)
    def slow(_):
        calls.append('slow')
        clock[0] += timedelta(seconds=10)
        return CSV
    source.transport=slow
    source.view()
    clock[0]=NOW+timedelta(minutes=5)
    source.view()
    assert len(calls)==2


@pytest.mark.parametrize('saved',['null','[]','not json','{"schema":1,"rows":null}'])
def test_damaged_cache_recovers_from_official_download(tmp_path,saved):
    (tmp_path/'treasury.json').write_text(saved)
    source, _, _=service(tmp_path)
    assert source.view()['status']=='ready'


def test_read_only_endpoint_no_store_and_isolated_network_disabled(tmp_path):
    app=create_app(str(tmp_path/'app.sqlite3'));client=TestClient(app)
    assert not app.state.treasury_yields.enabled
    response=client.get('/api/bonds/treasury')
    assert response.status_code==200 and response.headers['cache-control']=='no-store'
    assert response.json()['status']=='disabled' and response.json()['rows']==[]
    assert client.post('/api/bonds/treasury').status_code==405


@pytest.mark.parametrize('stamp,seconds', [
    ('2026-07-06T20:15:00+00:00', 60), ('2026-12-07T21:15:00+00:00', 60),
    ('2026-10-30T19:59:59+00:00', 300), ('2026-10-30T20:00:00+00:00', 60),
    ('2026-10-30T22:59:59+00:00', 60), ('2026-10-30T23:00:00+00:00', 300),
    ('2026-10-31T21:15:00+00:00', 300), ('2026-10-31T04:15:00+08:00', 60),
])
def test_source_cadence_covers_release_in_both_dst_seasons(stamp, seconds):
    assert poll_interval(datetime.fromisoformat(stamp)) == seconds


def test_scheduled_endpoint_poll_picks_up_new_release_and_recalculates_every_chart(tmp_path):
    source, clock, calls = service(tmp_path)
    clock[0] = NOW.replace(hour=19, minute=59)
    app = create_app(str(tmp_path/'app.sqlite3'))
    endpoint_source = app.state.treasury_yields
    endpoint_source.enabled = True
    endpoint_source.transport = source.transport
    endpoint_source.clock = source.clock
    source = endpoint_source
    client = TestClient(app)
    before = client.get('/api/bonds/treasury').json()
    assert before['poll_interval_seconds'] == 300 and len(calls) == 1
    # A new official row becomes available after the initial page load.
    source.transport = lambda _: CSV+'2026-10-29,4.16,4.65,4.83,5.02,5.35\n'
    clock[0] += timedelta(seconds=59)
    assert client.get('/api/bonds/treasury').json()['latest_observation_date'] == '2026-10-28'
    clock[0] += timedelta(seconds=1)
    after = client.get('/api/bonds/treasury').json()  # No manual refresh flag.
    assert after['poll_interval_seconds'] == 60 and after['latest_observation_date'] == '2026-10-29'
    assert len(after['rows']) == len(before['rows'])+1
    assert all(item['date'] == '2026-10-29' for item in after['quotes']+after['metrics'])
    assert after['curve_date'] == '2026-10-29'
    assert [item['value'] for item in after['quotes']] == [4.16, 4.65, 4.83, 5.02, 5.35]
    assert {item['key']:item['value'] for item in after['metrics']} == {
        '2s5s':18, '2s10s':37, '3m10s':86, '5s30s':52, '5s10s':19, '10s30s':33,
        '2s5s10s':-1, '2s10s30s':4}


def api_fixture(url):
    query = parse_qs(urlparse(url).query)
    assert urlparse(url).netloc == 'api.stlouisfed.org'
    assert query['api_key'] == ['test-fred-key']
    assert query['units'] == ['lin'] and 'frequency' not in query
    assert query['observation_start'] == ['2000-01-01'] and query['offset'] == ['0']
    identifier = query['series_id'][0]
    values = csv_records(CSV, [identifier], NOW.date())
    return {'count':len(values), 'offset':0, 'units':'lin',
            'observations':[{'date':stamp, 'value':str(row[identifier])} for stamp,row in values.items()]}


def test_authenticated_api_drives_all_charts_and_credentials_never_enter_output(tmp_path):
    calls = []
    def fetch(url):
        calls.append(url)
        return json.dumps(api_fixture(url))
    source = TreasuryYieldService(tmp_path/'api.json', transport=fetch, clock=lambda:NOW, api_key='test-fred-key')
    result = source.view()
    assert result['source_route'] == 'FRED API' and result['source_warning'] is None
    assert len(calls) == 5 and {parse_qs(urlparse(url).query)['series_id'][0] for url in calls} == {item[2] for item in TENORS}
    assert result['rows'] == analytics(download(lambda _:CSV, NOW.date())[0])['rows']
    assert 'test-fred-key' not in json.dumps(result) and 'test-fred-key' not in source.path.read_text()
    restarted = TreasuryYieldService(source.path, enabled=False, clock=lambda:NOW, api_key='test-fred-key')
    assert restarted.view()['source_route'] == 'FRED API'


def test_api_keeps_native_missing_dates_and_missing_values():
    def fetch(url):
        response = api_fixture(url)
        identifier = parse_qs(urlparse(url).query)['series_id'][0]
        if identifier == 'DGS30': response['observations'].append({'date':'2026-10-29','value':'.'})
        else: response['observations'].append({'date':'2026-10-29','value':'4.5'})
        response['count'] += 1
        return json.dumps(response)
    rows,route = api_download(fetch,NOW.date(),'test-fred-key')
    assert route == 'FRED API' and rows[-1]['y30'] is None
    view = analytics(rows)
    assert view['curve_date'] == '2026-10-28' and view['rows'][-1]['10s30s'] is None
    assert view['rows'][-1]['2s10s'] == 0


@pytest.mark.parametrize('problem',['count','offset','units','duplicate','future','nan','missing_value'])
def test_invalid_api_response_falls_back_as_one_complete_dataset(problem):
    public = []
    def fetch(url):
        if urlparse(url).netloc != 'api.stlouisfed.org':
            public.append(url)
            return CSV
        response = api_fixture(url)
        if problem == 'count': response['count'] += 1
        elif problem == 'offset': response['offset'] = 1
        elif problem == 'units': response['units'] = 'chg'
        elif problem == 'duplicate': response['observations'][1]['date'] = response['observations'][0]['date']
        elif problem == 'future': response['observations'][1]['date'] = '2026-11-01'
        elif problem == 'nan': response['observations'][1]['value'] = 'NaN'
        elif problem == 'missing_value': del response['observations'][1]['value']
        return json.dumps(response)
    rows,route = download(fetch,NOW.date(),'test-fred-key')
    assert rows[-1]['y10'] == 5.05 and public and '(API unavailable)' in route


def test_api_failure_warning_and_total_outage_preserve_saved_dates_without_secrets(tmp_path):
    clock = [NOW]
    source = TreasuryYieldService(tmp_path/'api.json', transport=lambda url:json.dumps(api_fixture(url)), clock=lambda:clock[0], api_key='test-fred-key')
    checked = source.view()['last_checked_at']
    def fail_api(url):
        if 'api.stlouisfed.org' in url: raise OSError('Secret authenticated URL: '+url)
        return CSV
    source.transport = fail_api
    clock[0] += timedelta(minutes=5)
    result = source.view()
    assert result['status'] == 'ready' and result['source_warning'] and '(API unavailable)' in result['source_route']
    assert 'test-fred-key' not in json.dumps(result)
    checked = result['last_checked_at']
    def fail_all(url): raise OSError('Secret authenticated URL: '+url)
    source.transport = fail_all
    clock[0] += timedelta(minutes=5)
    result = source.view()
    assert result['status'] == 'stale' and result['last_checked_at'] == checked
    assert 'test-fred-key' not in json.dumps(result) and 'api_key=' not in json.dumps(result)


def test_scoped_bonds_key_loads_without_changing_other_section_credentials(tmp_path,monkeypatch):
    (tmp_path/'.env').write_text('BONDS_FRED_API_KEY=test-fred-key\nBONDS_ENABLED=true\n')
    configured = {}
    load_backend_env(tmp_path,environ=configured)
    monkeypatch.setenv('BONDS_FRED_API_KEY',configured['BONDS_FRED_API_KEY'])
    assert TreasuryYieldService(tmp_path/'api.json',enabled=False).api_key == 'test-fred-key'
    import os
    assert not os.environ.get('FRED_API_KEY')
