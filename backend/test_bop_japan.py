from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .bop import BOPError, BOPService
from .bop_japan import (BOJ_GROUPS, BOP_CODES, CUSTOMS_URL, JapanBOPService, SNAPSHOT,
                        build_japan_view, parse_boj, parse_customs)


def official():
    return json.loads(SNAPSHOT.read_text(encoding='utf-8'))


def api_fixture():
    return json.loads((SNAPSHOT.parent / 'japan_bop_api_fixture.json').read_text(encoding='utf-8'))


def test_boj_parser_preserves_native_dates_full_precision_and_converts_yen_units():
    payload = api_fixture()
    rows, metadata = parse_boj(json.dumps(payload), 'bop')
    assert rows[-1]['date'] == '2026-08-01'
    assert rows[-1]['current_account'] == pytest.approx(40619.67522224 / 10)
    assert len(metadata) == len(BOP_CODES)
    assert all(item['native_unit'] == '100 million Yen' for item in metadata)


@pytest.mark.parametrize('corruption', ['status', 'pagination', 'database', 'missing', 'duplicate_series', 'unit', 'frequency', 'duplicate_dates', 'length', 'nan', 'identity'])
def test_invalid_or_incomplete_boj_data_is_rejected(corruption):
    body = api_fixture()
    first = body['RESULTSET'][0]
    if corruption == 'status': body['STATUS'] = 503
    elif corruption == 'pagination': body['NEXTPOSITION'] = 10
    elif corruption == 'database': body['PARAMETER']['DB'] = 'FM08'
    elif corruption == 'missing': body['RESULTSET'].pop()
    elif corruption == 'duplicate_series': body['RESULTSET'][-1] = deepcopy(first)
    elif corruption == 'unit': first['UNIT'] = 'US Dollars'
    elif corruption == 'frequency': first['FREQUENCY'] = 'QUARTERLY'
    elif corruption == 'duplicate_dates': first['VALUES']['SURVEY_DATES'][0] = first['VALUES']['SURVEY_DATES'][1]
    elif corruption == 'length': first['VALUES']['VALUES'].pop()
    elif corruption == 'nan': first['VALUES']['VALUES'][0] = 'NaN'
    elif corruption == 'identity': first['VALUES']['VALUES'][0] += 100
    with pytest.raises((BOPError, ValueError)):
        parse_boj(json.dumps(body), 'bop')


def test_every_historical_quarter_matches_independent_mof_release_within_published_rounding():
    reference = json.loads((SNAPSHOT.parent / 'japan_bop_reference.json').read_text(encoding='utf-8'))
    view = build_japan_view(official(), 'quarterly')
    actual = {row['date']: row for row in view['series']}
    assert len(reference['series']) == 122
    for row in reference['series']:
        for field, expected in row.items():
            if field != 'date':
                assert actual[row['date']][field] == pytest.approx(expected, abs=.050001), (row['date'], field)
    assert view['validation']['status'] == 'passed'
    assert all(check['observations_checked'] == 122 for check in view['validation']['checks'])


def test_japan_rolling_flows_and_stock_ratios_keep_different_meanings():
    quarterly = build_japan_view(official(), 'quarterly')
    rolling = build_japan_view(official(), 'trailing4q')
    assert quarterly['latest']['current_account'] == pytest.approx(8069.657061373)
    assert rolling['latest']['current_account'] == pytest.approx(35719.383884975, abs=.000001)
    for field in ('net_direct', 'net_equity', 'net_debt', 'net_other', 'reserve_transactions'):
        assert rolling['macro']['quarterly'][-1][field] == pytest.approx(sum(row[field] for row in quarterly['macro']['quarterly'][-4:]))
    for field in ('reserve_stock', 'short_term_external_debt', 'reserves_debt_pct'):
        assert rolling['macro']['quarterly'][-1][field] == quarterly['macro']['quarterly'][-1][field]
    assert quarterly['macro']['quarterly'][-1]['reserve_stock'] == 208818.127
    assert quarterly['macro']['quarterly'][-1]['reserves_debt_pct'] == pytest.approx(40.83615663869042)
    assert quarterly['latest']['date'] == '2026-04-01'  # July/August are not a complete quarter.


def test_matched_seasonally_adjusted_gdp_and_monthly_yen_averages():
    snapshot = official()
    quarter = build_japan_view(snapshot, 'quarterly')['macro']['quarterly'][-1]
    rolling = build_japan_view(snapshot, 'trailing4q')['macro']['quarterly'][-1]
    months = snapshot['groups']['bop']['rows']
    numerator = sum(Decimal(str(row['current_account_sa'])) for row in months if '2026-04-01' <= row['date'] <= '2026-06-01')
    assert quarter['current_account_gdp_pct'] == pytest.approx(float(100 * numerator / (Decimal('689219.1') / 4)))
    assert quarter['current_account_gdp_pct'] == pytest.approx(5.202756247684372)
    currency = snapshot['groups']['currency']['rows']
    assert quarter['currency_index'] == pytest.approx(sum(row['currency_index'] for row in currency if '2026-04-01' <= row['date'] <= '2026-06-01') / 3)
    assert rolling['currency_index'] == pytest.approx(sum(row['currency_index'] for row in currency if '2025-07-01' <= row['date'] <= '2026-06-01') / 12)
    snapshot['groups']['gdp']['rows'].pop()
    assert build_japan_view(snapshot)['macro']['quarterly'][-1]['current_account_gdp_pct'] is None


def test_missing_month_is_not_projected_and_currency_gaps_are_not_averaged_away():
    snapshot = official()
    snapshot['groups']['bop']['rows'] = [row for row in snapshot['groups']['bop']['rows'] if row['date'] != '2026-05-01']
    view = build_japan_view(snapshot, 'quarterly')
    assert view['latest']['period'] == '2026 Q1'
    assert not any(row['date'] == '2026-04-01' for row in view['macro']['trade'])
    snapshot = official()
    snapshot['groups']['currency']['rows'] = [row for row in snapshot['groups']['currency']['rows'] if row['date'] != '2026-05-01']
    assert build_japan_view(snapshot)['macro']['quarterly'][-1]['currency_index'] is None


def test_customs_thousand_yen_conversion_future_placeholders_and_schema():
    body = 'WORLD Monthly Data (a thousand yen)\nYears/Months,Exp-Total,Imp-Total\n2026/09,10953854342,11510333013\n2026/10,0,0\n'
    rows = parse_customs(body)
    assert rows == [{'date': '2026-09-01', 'customs_exports': 10953.854342, 'customs_imports': 11510.333013, 'customs_goods': -556.478671}]
    with pytest.raises(BOPError): parse_customs(body.replace('a thousand yen', 'US dollars'))
    with pytest.raises(BOPError): parse_customs(body + '2026/09,2,3\n')


def test_japan_trade_frequency_moving_average_and_yoy_from_selected_totals():
    quarterly = build_japan_view(official(), 'quarterly')['macro']['trade']
    rolling = build_japan_view(official(), 'trailing4q')['macro']['trade']
    assert quarterly[-1]['exports'] == pytest.approx(41788.972760683)
    assert quarterly[-1]['exports_ma3'] == pytest.approx(sum(row['exports'] for row in quarterly[-3:]) / 3)
    assert rolling[-1]['exports'] == pytest.approx(sum(row['exports'] for row in quarterly[-4:]))
    assert rolling[-1]['exports_yoy'] == pytest.approx(100 * (rolling[-1]['exports'] / rolling[-5]['exports'] - 1))
    assert quarterly[-1]['trade_balance'] == pytest.approx(quarterly[-1]['exports'] - quarterly[-1]['imports'])


def test_source_outage_keeps_original_values_and_check_times_with_failure_backoff(tmp_path):
    calls = []
    def offline(url):
        calls.append(url)
        raise OSError('Offline')
    service = JapanBOPService(tmp_path / 'japan.json', transport=offline)
    original = service.view()
    failed = service.view(refresh=True)
    assert failed['latest'] == original['latest']
    assert failed['sources'] == original['sources']
    assert failed['delivery'] == 'saved_snapshot' and len(failed['macro']['warnings']) == 5
    service.view(refresh=True)
    assert len(calls) == 5
    assert JapanBOPService(service.cache_path, enabled=False).view()['latest'] == original['latest']


def test_partial_refresh_keeps_failed_family_but_accepts_revisions_and_saves_atomically(tmp_path):
    service = JapanBOPService(tmp_path / 'japan.json')
    previous = deepcopy(service.snapshot)
    def download(name):
        if name == 'currency': raise OSError('BOJ currency temporarily unavailable')
        group = deepcopy(previous['groups'][name])
        group['checked_at'] = '2026-10-30T00:00:00+00:00'
        if name == 'positions': group['rows'][-1]['reserve_stock'] += 1
        return group
    service._download = download
    updated = service.view(refresh=True)
    assert len(updated['macro']['warnings']) == 1
    assert service.snapshot['groups']['currency'] == previous['groups']['currency']
    assert updated['macro']['quarterly'][-1]['reserve_stock'] == 208819.127
    assert service.cache_path.exists() and not service.cache_path.with_suffix('.tmp').exists()


def test_regressed_series_coverage_is_rejected_without_relabelling_old_data(tmp_path):
    service = JapanBOPService(tmp_path / 'japan.json')
    before = deepcopy(service.snapshot)
    def download(name):
        group = deepcopy(before['groups'][name])
        if name == 'bop': group['rows'][-1]['equity_assets'] = None
        return group
    service._download = download
    result = service.view(refresh=True)
    assert service.snapshot['groups']['bop'] == before['groups']['bop']
    assert 'international transactions refresh failed' in result['macro']['warnings'][0]


def test_japan_endpoint_is_isolated_from_us_and_has_correct_provenance_and_units(tmp_path):
    app = create_app(tmp_path / 'state.json')
    client = TestClient(app)
    for frequency in ('quarterly', 'trailing4q'):
        response = client.get('/api/fx/bop', params={'country': 'JPN', 'frequency': frequency, 'refresh': 'false'})
        assert response.status_code == 200
        data = response.json()
        assert response.headers['Cache-Control'] == 'no-store'
        assert data['country']['connected'] and data['country']['currency'] == 'JPY'
        assert data['units'] == 'JPY billions' and data['macro']['country_code'] == 'JPN'
        assert data['macro']['frequency'] == frequency
        assert len(data['sources']) == 5 and all(source['latest_observation'] for source in data['sources'])
        assert not any('IEA' in series['id'] or 'DTWEX' in series['id'] for source in data['sources'] for series in source['series'])
    assert client.get('/api/fx/bop?country=USA&refresh=false').json()['country']['currency'] == 'USD'
    assert client.get('/api/fx/bop?country=EUR&refresh=false').status_code == 404
    with pytest.raises(BOPError): BOPService(tmp_path / 'us.json', enabled=False).view('JPN')
