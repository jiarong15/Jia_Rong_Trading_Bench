from copy import deepcopy
import csv
import io
import json
import zipfile
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .bop import BOPError, BOPService
from .bop_uk import (UKBOPService, SNAPSHOT, UKEA_CODES, TRADE_CODES, GROUP_NAMES,
                     build_uk_view, customs_url, parse_ons, parse_customs, parse_sterling)


def official():
    return json.loads(SNAPSHOT.read_text(encoding='utf-8'))


def fixture(name):
    with zipfile.ZipFile(SNAPSHOT.parent / 'uk_parser_fixtures.zip') as archive:
        return archive.read(name).decode('utf-8-sig')


def test_ons_native_millions_convert_to_billions_and_keep_sa_and_nsa_separate():
    rows, metadata = parse_ons(fixture('ukea.csv'), UKEA_CODES, 'quarterly')
    latest = rows[-1]
    assert latest['current_account'] == -19.932
    assert latest['current_account_nsa'] == -22.456
    assert latest['goods'] == -64.044 and latest['bop_goods_nsa'] == -62.758
    assert latest['gdp'] == 787.197  # Actual quarter, not an annual rate.
    assert len(metadata) == len(UKEA_CODES)
    assert all(row['date'][5:7] in ('01', '04', '07', '10') for row in rows)
    assert all(item['native_unit'] == 'GBP millions' for item in metadata)


@pytest.mark.parametrize('corruption', ['unit', 'missing_code', 'duplicate_code', 'duplicate_date', 'truncated_row', 'nan', 'ca_identity', 'financial_identity'])
def test_corrupt_ons_data_is_rejected(corruption):
    rows = list(csv.reader(io.StringIO(fixture('ukea.csv'))))
    if corruption == 'unit': rows[2][1] = '$'
    elif corruption == 'missing_code': rows[1][1] = 'NOT_UK'
    elif corruption == 'duplicate_code': rows[1][2] = rows[1][1]
    elif corruption == 'duplicate_date': rows.append(deepcopy(rows[-1]))
    elif corruption == 'truncated_row': rows[-1].pop()
    elif corruption == 'nan': rows[-1][1] = 'NaN'
    elif corruption == 'ca_identity': rows[-1][1] = '100'
    else: rows[-1][rows[1].index('N2SV')] = '100'
    output = io.StringIO(); csv.writer(output).writerows(rows)
    with pytest.raises(BOPError): parse_ons(output.getvalue(), UKEA_CODES, 'quarterly')


def test_hmrc_query_avoids_suppressed_product_double_count_and_keeps_eu_non_eu():
    from urllib.parse import unquote
    query = unquote(customs_url(2026))
    assert 'SuppressionIndex eq 0' in query and 'CommodityId' not in query
    body = fixture('customs-2026.json')
    rows = parse_customs(body, 2026)
    assert len(rows) == 8 and rows[-1]['date'] == '2026-08-01'
    # Independent sum of the four native GBP API flows, including HS2 totals.
    raw = json.loads(body)['value']
    august = {item['FlowTypeId']: item['TotalValue'] for item in raw if item['MonthId'] == 202608}
    assert rows[-1]['customs_exports'] == pytest.approx((august[2] + august[4]) / 1e9)
    assert rows[-1]['customs_imports'] == pytest.approx((august[1] + august[3]) / 1e9)
    for damage in ('missing_flow', 'duplicate', 'pagination', 'wrong_year'):
        payload = json.loads(body)
        if damage == 'missing_flow': payload['value'].pop()
        elif damage == 'duplicate': payload['value'].append(deepcopy(payload['value'][0]))
        elif damage == 'pagination': payload['@odata.nextLink'] = 'next-page'
        else: payload['value'][0]['MonthId'] = 202508
        with pytest.raises(BOPError): parse_customs(json.dumps(payload), 2026)


def test_boe_broad_sterling_index_uses_completed_quarters_and_rejects_schema_drift():
    body = fixture('sterling.csv')
    rows = parse_sterling(body)
    assert rows[-1] == {'date': '2026-07-01', 'currency_index': 85.2595}
    with pytest.raises(BOPError): parse_sterling(body.replace('XUQABK82', 'XUQABK67'))
    with pytest.raises(BOPError): parse_sterling(body + '30 Sep 2026,85.2595\n')
    with pytest.raises(BOPError): parse_sterling('DATE,XUQABK82\n30 Aug 2026,85\n')


def test_uk_flow_signs_match_ons_financial_bulletin_and_reserve_acquisition():
    view = build_uk_view(official(), 'quarterly')
    row = next(row for row in view['macro']['quarterly'] if row['date'] == '2026-04-01')
    assert row['net_direct'] == 4.319   # -13.362 outward + 17.681 inward.
    assert row['net_equity'] == -58.966  # -61.356 outward + 2.390 inward.
    assert row['net_debt'] == -1.954
    assert row['net_other'] == 63.557
    assert row['reserve_transactions'] == 2.263
    assert view['latest']['financial_account'] == -29.747
    assert view['latest']['net_financial_inflow'] == 29.747
    assert view['latest']['statistical_discrepancy'] == -5.105
    assert view['validation']['status'] == 'passed'
    assert all(check['observations_checked'] == 118 for check in view['validation']['checks'])


def test_uk_rolling_flows_matched_gdp_and_unsummed_sector_stock_coverage():
    snapshot = official()
    q = build_uk_view(snapshot, 'quarterly')
    r = build_uk_view(snapshot, 'trailing4q')
    assert r['latest']['current_account'] == -86.904
    for field in ('current_account', 'goods', 'services', 'bop_goods_nsa', 'customs_goods', 'goods_gap'):
        assert r['latest'][field] == pytest.approx(sum(row[field] for row in q['series'][-4:]))
    at = '2026-04-01'
    qr = next(row for row in q['macro']['quarterly'] if row['date'] == at)
    rr = next(row for row in r['macro']['quarterly'] if row['date'] == at)
    assert qr['current_account_gdp_pct'] == pytest.approx(-19.932 / 787.197 * 100)
    gdp = sum(row['gdp'] for row in snapshot['groups']['accounts']['rows'][-4:])
    assert rr['current_account_gdp_pct'] == pytest.approx(-86.904 / gdp * 100)
    assert qr['short_term_external_debt'] == pytest.approx(54.167 + 4594.482 + 1497.747 + 40.571)
    assert qr['reserves_debt_pct'] == pytest.approx(162.626 / qr['short_term_external_debt'] * 100)
    for field in ('reserve_stock', 'short_term_external_debt', 'reserves_debt_pct'):
        assert qr[field] == rr[field]
    for field in ('net_direct', 'net_equity', 'net_debt', 'net_other', 'reserve_transactions'):
        assert rr[field] == pytest.approx(sum(row[field] for row in q['macro']['quarterly'] if '2025-07-01' <= row['date'] <= at))
    assert r['latest']['goods_gap'] == pytest.approx(r['latest']['customs_goods'] - r['latest']['bop_goods_nsa'])
    assert r['latest']['goods_gap'] != pytest.approx(r['latest']['customs_goods'] - r['latest']['goods'])


def test_missing_inputs_leave_gaps_and_newer_sterling_does_not_project_accounts():
    snapshot = official()
    snapshot['groups']['accounts']['rows'][-1]['gdp'] = None
    snapshot['groups']['accounts']['rows'][-1]['debt_banks'] = None
    snapshot['groups']['currency']['rows'] = [row for row in snapshot['groups']['currency']['rows'] if row['date'] != '2026-01-01']
    snapshot['groups']['trade']['rows'] = [row for row in snapshot['groups']['trade']['rows'] if row['date'] != '2026-05-01']
    view = build_uk_view(snapshot)
    quarter = next(row for row in view['macro']['quarterly'] if row['date'] == '2026-04-01')
    for field in ('current_account_gdp_pct', 'reserves_debt_pct', 'currency_index'):
        assert quarter[field] is None
    assert not any(row['date'] == '2026-04-01' for row in view['macro']['trade'])
    latest = view['macro']['quarterly'][-1]
    assert latest['date'] == '2026-07-01' and latest['currency_index'] is None  # Missing quarter in 4Q window.
    assert latest['current_account_gdp_pct'] is None and latest['reserve_transactions'] is None
    assert view['latest']['date'] == '2026-04-01'


def test_ons_monthly_trade_balance_growth_and_moving_average_are_recomputed():
    parsed, _ = parse_ons(fixture('mret.csv'), TRADE_CODES, 'monthly')
    assert parsed[-1]['exports'] == 82.332 and parsed[-1]['trade_balance'] == -.92
    q = build_uk_view(official(), 'quarterly')['macro']['trade']
    r = build_uk_view(official(), 'trailing4q')['macro']['trade']
    assert q[-1]['date'] == '2026-04-01'  # August isn't a complete third quarter.
    for field in ('exports', 'imports'):
        assert r[-1][field] == pytest.approx(sum(row[field] for row in q[-4:]))
        assert r[-1][field + '_ma3'] == pytest.approx(sum(row[field] for row in r[-3:]) / 3)
        assert r[-1][field + '_yoy'] == pytest.approx(100 * (r[-1][field] / r[-5][field] - 1))
    assert r[-1]['trade_balance'] == pytest.approx(r[-1]['exports'] - r[-1]['imports'])


def test_partial_refresh_preserves_failed_source_time_and_accepts_revisions(tmp_path):
    service = UKBOPService(tmp_path / 'uk.json')
    before = deepcopy(service.snapshot)
    def download(name):
        if name == 'customs': raise OSError('HMRC temporarily unavailable')
        group = deepcopy(before['groups'][name]); group['checked_at'] = '2026-10-29T16:00:00+00:00'
        if name == 'currency': group['rows'][-1]['currency_index'] += 1
        return group
    service._download = download
    result = service.view(refresh=True)
    assert result['delivery'] == 'saved_snapshot' and len(result['macro']['warnings']) == 1
    assert service.snapshot['groups']['customs'] == before['groups']['customs']
    assert service.snapshot['groups']['currency']['rows'][-1]['currency_index'] == 86.2595
    assert service.cache_path.exists() and not service.cache_path.with_suffix('.tmp').exists()
    assert UKBOPService(service.cache_path, enabled=False).view()['latest'] == result['latest']


def test_outage_backoff_and_interior_coverage_regression_keep_saved_data(tmp_path):
    service = UKBOPService(tmp_path / 'uk.json')
    before = deepcopy(service.snapshot)
    calls = []
    def download(name):
        calls.append(name)
        group = deepcopy(before['groups'][name])
        if name == 'accounts': group['rows'][10]['gdp'] = None
        else: raise OSError('Offline')
        return group
    service._download = download
    result = service.view(refresh=True)
    assert len(result['macro']['warnings']) == 4 and service.snapshot['groups'] == before['groups']
    service.view(refresh=True)
    assert len(calls) == len(GROUP_NAMES)
    assert 0 < result['refresh_cooldown_seconds'] <= 30


def test_uk_endpoint_is_isolated_gbp_and_no_store_with_real_source_metadata(tmp_path):
    client = TestClient(create_app(tmp_path / 'state.json'))
    for frequency in ('quarterly', 'trailing4q'):
        response = client.get('/api/fx/bop', params={'country': 'gbr', 'frequency': frequency, 'refresh': 'false'})
        assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
        data = response.json()
        assert data['country']['connected'] and data['reporting_currency'] == 'GBP'
        assert data['units'] == 'GBP billions' and data['macro']['country_code'] == 'GBR'
        assert data['macro']['frequency'] == frequency and len(data['sources']) == 4
        assert all(source['latest_observation'] and source['checked_at'] for source in data['sources'])
        assert 'XUQABK82' in {series['id'] for source in data['sources'] for series in source['series']}
    with pytest.raises(BOPError): BOPService(tmp_path / 'usa.json', enabled=False).view('GBR')
    assert client.get('/api/fx/bop?country=EUR&refresh=false').status_code == 404
