from copy import deepcopy
import json
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .bop import (BOPError, BOPService, CENSUS_URL, FIELDS, FRED_URL, SNAPSHOT, SERIES,
                  aggregate, assemble_snapshot, build_view, parse_census, parse_fred, fetch_fred)


def official():
    return json.loads(SNAPSHOT.read_text(encoding='utf-8'))


def sample_csv():
    return ('observation_date,IEABC,IEABCG,IEABCS,IEABCPI,IEABCSI,IEANLC,IEANLF,IEASD,IEAXI,IEAMI,IEABCP\n'
            '2025-01-01,-70000,-100000,40000,10000,-20000,-71000,-76000,-5000,50000,40000,-1000\n'
            '2025-04-01,-70000,-100000,40000,10000,-20000,-71000,-76000,-5000,50000,40000,-1000\n'
            '2025-07-01,-70000,-100000,40000,10000,-20000,-71000,-76000,-5000,50000,40000,-1000\n'
            '2025-10-01,-70000,-100000,40000,10000,-20000,-71000,-76000,-5000,50000,40000,-1000\n')


def sample_html():
    return ('<h3>World, Seasonally Adjusted</h3><table>' + ''.join(
        f'<tr><td>{month} 2025</td><td>10,000</td><td>40,000</td><td>-30,000</td></tr>'
        for month in ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December')) + '</table>')


def test_official_snapshot_reconciles_and_has_independent_source_provenance():
    snapshot = official()
    assert snapshot['series'] and snapshot['retrieved_at']
    for row in snapshot['series']:
        assert row['current_account'] == pytest.approx(sum(row[k] for k in ('goods', 'services', 'primary_income', 'secondary_income')), abs=.005)
        assert row['financial_account'] - row['net_lending'] == pytest.approx(row['statistical_discrepancy'], abs=.005)
        assert row['income_receipts'] - row['income_payments'] == pytest.approx(row['primary_income'], abs=.005)
        assert row['net_financial_inflow'] == -row['financial_account']
    assert snapshot['sources'][0]['download_url'] == FRED_URL
    assert snapshot['sources'][1]['url'] == CENSUS_URL


def test_complete_months_and_millions_to_billions():
    snapshot = assemble_snapshot(sample_csv(), sample_html(), '2026-10-29T00:00:00+00:00')
    latest = snapshot['series'][-1]
    assert latest['customs_goods'] == -90
    assert latest['goods'] == -100
    assert latest['goods_gap'] == 10  # Census minus BOP; never sign-flipped.
    assert latest['capital_account'] == -1
    view = build_view(snapshot)
    assert view['latest']['current_account'] == -280
    assert view['latest']['goods_gap'] == 40
    assert view['latest']['statistical_discrepancy'] == -20
    assert view['period_start'] == '2025 Q1' and view['period_end'] == '2025 Q4'
    assert len(view['contributing_quarters']) == 4
    assert view['latest_quarter']['current_account'] == -70


def test_incomplete_census_quarter_is_not_scaled_or_filled():
    html = sample_html().replace('<tr><td>December 2025</td><td>10,000</td><td>40,000</td><td>-30,000</td></tr>', '')
    quarters, latest_month = parse_census(html)
    assert '2025-10-01' not in quarters and latest_month == '2025-11-01'
    snapshot = assemble_snapshot(sample_csv(), html, '2026-10-29T00:00:00+00:00')
    assert snapshot['series'][-1]['customs_goods'] is None
    assert build_view(snapshot)['latest']['goods_gap'] is None


def test_rolling_sum_requires_four_calendar_quarters_and_complete_inputs():
    rows = official()['series'][:8]
    rows[2]['goods_gap'] = None
    result = aggregate(rows, 'trailing4q')
    assert result[0]['goods_gap'] is None
    assert result[0]['current_account'] == pytest.approx(sum(row['current_account'] for row in rows[:4]))
    missing_quarter = rows[:2] + rows[3:]
    assert all(row['date'] not in ('1999-10-01', '2000-01-01', '2000-04-01') for row in aggregate(missing_quarter, 'trailing4q'))
    assert all(field in result[-1] for field in FIELDS)


@pytest.mark.parametrize('body', ['<html>Access denied</html>', sample_csv().replace('-70000', 'NaN'), sample_csv().replace('2025-04-01', '2025-01-01'), sample_csv().replace('-70000', '-90000'), sample_csv().replace('-5000', '5000')])
def test_invalid_fred_or_accounting_identity_is_rejected(body):
    with pytest.raises((BOPError, ValueError)):
        parse_fred(body)


def test_census_wrong_basis_and_non_reconciling_columns_are_rejected():
    with pytest.raises(BOPError):
        parse_census(sample_html().replace('World, Seasonally Adjusted', 'World, Not Seasonally Adjusted'))
    with pytest.raises(BOPError):
        parse_census(sample_html().replace('-30,000', '-3,000'))


def test_removed_country_and_unknown_country_never_return_us_data():
    with pytest.raises(KeyError): build_view(None, 'EUR')
    with pytest.raises(KeyError): build_view(official(), 'XXX')
    with pytest.raises(BOPError): build_view(None, 'AUS')


def test_source_outage_retains_original_snapshot_and_cools_down(tmp_path):
    calls = []
    def offline(url):
        calls.append(url)
        raise OSError('Network unavailable')
    service = BOPService(tmp_path / 'cache.json', transport=offline)
    initial = service.view()
    failed = service.view(refresh=True)
    assert failed['refresh_error'] and failed['retrieved_at'] == initial['retrieved_at']
    assert failed['latest'] == initial['latest']
    service.view(refresh=True)
    assert len(calls) == 1 and not service.cache_path.exists()
    with pytest.raises(KeyError): service.view('EUR', refresh=True)
    assert len(calls) == 1


def test_valid_refresh_persists_atomically_and_survives_restart(tmp_path):
    snapshot = assemble_snapshot(sample_csv(), sample_html(), '2026-01-01T00:00:00+00:00')
    cache = tmp_path / 'cache.json'
    cache.write_text(json.dumps(snapshot))
    calls = []
    def transport(url):
        calls.append(url)
        return sample_csv() if url == FRED_URL else sample_html()
    service = BOPService(cache, transport=transport)
    view = service.view(refresh=True)
    assert calls == [FRED_URL, CENSUS_URL]
    assert not view['refresh_error'] and view['latest']['current_account'] == -280
    restored = BOPService(cache, enabled=False).view()
    assert restored['retrieved_at'] == view['retrieved_at']
    assert not list(tmp_path.glob('*.tmp'))


def test_partial_refresh_never_mixes_new_fred_with_old_census(tmp_path):
    def partial(url):
        if url == FRED_URL:
            return sample_csv()
        raise OSError('Census unavailable')
    service = BOPService(tmp_path / 'cache.json', transport=partial)
    previous = deepcopy(service.snapshot)
    assert service.view(refresh=True)['refresh_error']
    assert service.snapshot == previous


def test_bop_endpoint_snapshot_validation_and_unconnected_country(tmp_path):
    client = TestClient(create_app(tmp_path / 'test.sqlite3'))
    data = client.get('/api/fx/bop').json()
    assert data['status'] == 'ready' and data['frequency'] == 'trailing4q'
    assert data['refresh_available'] is False
    quarterly = client.get('/api/fx/bop?country=usa&frequency=quarterly').json()
    assert quarterly['latest']['current_account'] == official()['series'][-1]['current_account']
    assert client.get('/api/fx/bop?country=EUR').status_code == 404
    assert client.get('/api/fx/bop?country=XXX').status_code == 404
    assert client.get('/api/fx/bop?frequency=monthly').status_code == 422


def test_preserves_published_capital_instead_of_rederiving_rounded_difference():
    csv = sample_csv().replace('40000,-1000', '40000,-999')
    snapshot = assemble_snapshot(csv, sample_html(), '2026-10-29T00:00:00+00:00')
    assert snapshot['series'][-1]['capital_account'] == -.999
    assert snapshot['series'][-1]['net_lending'] - snapshot['series'][-1]['current_account'] == -1
    assert build_view(snapshot)['latest']['capital_account'] == -3.996


def test_audited_bea_release_figures_and_rolling_window():
    reference = json.loads((SNAPSHOT.parent / 'usa_bop_reference_2026-09-24.json').read_text())
    snapshot = official()
    by_date = {row['date']: row for row in snapshot['series']}
    for field, values in reference['series_usd_millions'].items():
        for at, expected in zip(reference['dates'], values):
            tolerance = .001 if field.startswith('customs_') else .00000001
            assert by_date[at][field] == pytest.approx(expected / 1000, abs=tolerance)
    view = build_view(snapshot)
    audited = next(row for row in view['series'] if row['date'] == '2026-04-01')
    assert audited['current_account'] == -942.557
    assert audited['goods'] == -1080.357
    assert audited['services'] == 350.296
    assert audited['goods_gap'] == 27.8392
    assert audited['capital_account'] == -1.253
    assert by_date['2026-04-01']['current_account'] == -246.023


def test_page_refresh_always_checks_sources_and_frequency_changes_reuse_cache(tmp_path):
    calls = []
    def transport(url):
        calls.append(url)
        return sample_csv() if url == FRED_URL else sample_html()
    path = tmp_path / 'cache.json'
    path.write_text(json.dumps(assemble_snapshot(sample_csv(), sample_html(), '2026-01-01T00:00:00+00:00')))
    service = BOPService(path, transport=transport, api_key='')
    assert service.view(refresh=True)['delivery'] == 'source_checked'
    assert service.view(refresh=True)['delivery'] == 'source_checked'
    assert calls == [FRED_URL, CENSUS_URL, FRED_URL, CENSUS_URL]
    assert service.view(frequency='quarterly', refresh=False)['delivery'] == 'saved_snapshot'
    assert len(calls) == 4


def test_api_default_checks_sources_and_disables_http_caching(tmp_path):
    app = create_app(tmp_path / 'test.sqlite3')
    calls = []
    def transport(url):
        calls.append(url)
        return sample_csv() if url == FRED_URL else sample_html()
    app.state.bop.enabled = True
    app.state.bop.api_key = ''
    app.state.bop.transport = transport
    app.state.bop.snapshot = assemble_snapshot(sample_csv(), sample_html(), '2026-01-01T00:00:00+00:00')
    client = TestClient(app)
    response = client.get('/api/fx/bop')
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.json()['delivery'] == 'source_checked'
    assert calls == [FRED_URL, CENSUS_URL]
    client.get('/api/fx/bop?refresh=false&frequency=quarterly')
    assert len(calls) == 2


def test_configured_fred_api_returns_native_unannualized_observations():
    import csv
    import io
    from urllib.parse import parse_qs, urlparse
    records = list(csv.DictReader(io.StringIO(sample_csv())))
    def transport(url):
        query = parse_qs(urlparse(url).query)
        assert query['units'] == ['lin'] and query['api_key'] == ['unit-test-secret']
        assert 'frequency' not in query  # No aggregation or annualization at FRED.
        series_id = query['series_id'][0]
        return json.dumps({'count': 4, 'observations': [{'date': row['observation_date'], 'value': row[series_id]} for row in records]})
    body, method, warning = fetch_fred(transport, 'unit-test-secret')
    assert method == 'FRED API' and warning is None
    assert len(parse_fred(body)) == 4
    assert parse_fred(body)[-1]['current_account'] == -70
    assert parse_fred(body)[-1]['capital_account'] == -1
    assert len(SERIES) == 11


def test_api_failure_falls_back_to_public_fred_without_exposing_credentials():
    def transport(url):
        if url == FRED_URL:
            return sample_csv()
        raise OSError('Private authenticated URL: api_key=unit-test-secret')
    body, method, warning = fetch_fred(transport, 'unit-test-secret')
    assert method == 'FRED public CSV' and warning
    assert 'unit-test-secret' not in body + method + warning


def test_regressed_or_truncated_source_cannot_replace_valid_history(tmp_path):
    def truncated(url):
        if url == FRED_URL:
            return sample_csv()
        return sample_html()
    service = BOPService(tmp_path / 'cache.json', transport=truncated, api_key='')
    original = deepcopy(service.snapshot)
    result = service.view(refresh=True)
    assert result['refresh_error'] and service.snapshot == original
