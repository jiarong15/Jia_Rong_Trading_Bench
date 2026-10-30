from copy import deepcopy
import json
import zipfile
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .bop import BOPError, BOPService, COUNTRIES
from .bop_australia import (AustraliaBOPService, SNAPSHOT, SPEC, PAGES, build_australia_view,
                            parse_abs, parse_abs_sheets, parse_bis_aud, discover_files)
from .public_workbooks import read_xlsx_sheet


def official(): return json.loads(SNAPSHOT.read_text(encoding='utf-8'))


def fixture(name):
    with zipfile.ZipFile(SNAPSHOT.parent / 'australia_parser_fixtures.zip') as archive:
        return archive.read(name)


def sheets(definition): return json.loads(fixture(definition['filename'] + '.json'))


def test_native_abs_workbook_parser_preserves_original_signs_and_aud_units():
    definition = SPEC['accounts'][0]
    rows, metadata = parse_abs(fixture('530201.xlsx'), definition)
    assert len(rows) == 118 and rows[-1]['date'] == '2026-04-01'
    assert rows[-1]['current_account_nsa'] == -24.803
    assert rows[-1]['direct_assets_cash'] == -3.128
    assert rows[-1]['reserve_cash'] == 2.427
    assert all(item['native_unit'] == '$ Millions' for item in metadata)


@pytest.mark.parametrize('damage', ['units', 'id', 'duplicate_id', 'adjustment', 'stock_flow', 'frequency', 'count', 'truncated', 'duplicate_date', 'nan'])
def test_changed_or_truncated_abs_series_is_rejected(damage):
    definition = SPEC['accounts'][0]; data = sheets(definition); rows = data['Data1']
    if damage == 'units': rows[1][1] = 'USD Millions'
    elif damage == 'id': rows[9][1] = 'OTHER_COUNTRY'
    elif damage == 'duplicate_id': rows[9][2] = rows[9][1]
    elif damage == 'adjustment': rows[2][1] = 'Seasonally Adjusted'
    elif damage == 'stock_flow': rows[3][1] = 'STOCK_CLOSE'
    elif damage == 'frequency': rows[4][1] = 'Year'
    elif damage == 'count': rows[8][1] = str(int(rows[8][1]) + 1)
    elif damage == 'truncated': rows.pop()
    elif damage == 'duplicate_date': rows.append(deepcopy(rows[-1]))
    else: rows[-1][1] = 'NaN'
    with pytest.raises(BOPError): parse_abs_sheets(data, definition)


def test_every_selected_series_matches_native_cells_with_metadata_validation():
    snapshot = official()
    for group, definitions in SPEC.items():
        stored = {row['date']: row for row in snapshot['groups'][group]['rows']}
        for definition in definitions:
            rows, metadata = parse_abs_sheets(sheets(definition), definition)
            assert len(metadata) == len(definition['fields'])
            for row in rows:
                for field in definition['fields']: assert stored[row['date']][field] == row[field]


def test_latest_release_links_are_rediscovered_and_cannot_leave_official_path():
    page = PAGES['bop']; base = page.rsplit('/', 1)[0]
    for edition in ('jun-2026', 'sep-2026'):
        body = f'<a href="{base}/{edition}/530201.xlsx">current release</a>'.encode()
        assert discover_files(body, page, ['530201.xlsx'])['530201.xlsx'] == base + '/' + edition + '/530201.xlsx'
    for html in ('<a href="https://other.example/530201.xlsx">bad</a>', '<p>not available</p>',
                 f'<a href="{base}/jun-2026/530201.xlsx">one</a><a href="{base}/sep-2026/530201.xlsx">two</a>'):
        with pytest.raises(BOPError): discover_files(html.encode(), page, ['530201.xlsx'])


def test_bis_only_accepts_monthly_nominal_broad_australian_index():
    body = fixture('bis-aud.csv')
    rows = parse_bis_aud(body)
    assert rows[-1]['date'] == '2026-09-01'
    for damaged in (body.replace(b',B,AU,', b',B,SG,'), body.replace(b',882,', b',999,'),
                    body.replace(b'\nM,N,', b'\nQ,N,'), body + body.splitlines()[-1] + b'\n'):
        with pytest.raises(BOPError): parse_bis_aud(damaged)


def test_bookkeeping_and_cash_flow_signs_match_abs_headline_and_financial_release():
    view = build_australia_view(official(), 'quarterly'); latest = view['latest']; row = view['macro']['quarterly'][-1]
    assert latest['current_account'] == -27.22  # ABS headline SA deficit.
    assert latest['bop_goods_nsa'] == 3.351 and latest['goods'] == 2.214
    assert latest['financial_account'] == -7.591 and latest['net_financial_inflow'] == 7.591
    assert row['net_direct'] == 15.14
    assert row['net_equity'] == pytest.approx(-60.463 + 7.765)
    assert row['net_debt'] == pytest.approx(-57.716 + 22.852)
    assert row['net_other'] == 74.855
    assert row['reserve_transactions'] == -2.427  # ABS positive asset cash flow is disposal.
    assert view['validation']['status'] == 'passed' and len(view['validation']['checks']) == 10


def test_matched_actual_quarter_gdp_and_original_customs_comparison():
    q = build_australia_view(official(), 'quarterly'); r = build_australia_view(official())
    assert q['macro']['quarterly'][-1]['current_account_gdp_pct'] == pytest.approx(-27.22 / 742.502 * 100)
    gdps = official()['groups']['gdp']['rows'][-4:]
    assert r['macro']['quarterly'][-1]['current_account_gdp_pct'] == pytest.approx(-94.455 / sum(row['gdp'] for row in gdps) * 100)
    assert r['latest']['current_account'] == -94.455
    assert q['latest']['goods_gap'] == pytest.approx(q['latest']['customs_goods'] - q['latest']['bop_goods_nsa'])
    assert q['latest']['goods_gap'] != pytest.approx(q['latest']['customs_goods'] - q['latest']['goods'])
    for field in ('goods', 'services', 'customs_goods', 'goods_gap', 'bop_goods_nsa'):
        assert r['latest'][field] == pytest.approx(sum(row[field] for row in q['series'][-4:]))


def test_sector_debt_and_reserves_are_matched_positive_quarter_end_stocks():
    q = build_australia_view(official(), 'quarterly')['macro']['quarterly']
    r = build_australia_view(official())['macro']['quarterly']
    assert q[-1]['reserve_stock'] == 102.52
    assert q[-1]['short_term_external_debt'] == pytest.approx(9.437 + 1.55 + 700.31 + 12.555 + 19.51)
    assert q[-1]['reserves_debt_pct'] == pytest.approx(102.52 / 743.362 * 100)
    for field in ('reserve_stock', 'short_term_external_debt', 'reserves_debt_pct'): assert q[-1][field] == r[-1][field]
    for field in ('net_direct', 'net_equity', 'net_debt', 'net_other', 'reserve_transactions'):
        assert r[-1][field] == pytest.approx(sum(row[field] for row in q[-4:]))


def test_monthly_sa_bop_goods_only_levels_growth_and_strict_complete_quarters():
    q = build_australia_view(official(), 'quarterly'); r = build_australia_view(official())
    assert q['macro']['trade_scope'] == 'bop_goods'
    assert q['macro']['monthly'][-1]['exports'] == 47.433 and q['macro']['monthly'][-1]['imports'] == 46.938
    assert q['macro']['trade'][-1]['date'] == '2026-04-01'  # July/August don't project September.
    for field in ('exports', 'imports'):
        trade = r['macro']['trade']
        assert trade[-1][field] == pytest.approx(sum(row[field] for row in q['macro']['trade'][-4:]))
        assert trade[-1][field + '_ma3'] == pytest.approx(sum(row[field] for row in trade[-3:]) / 3)
        assert trade[-1][field + '_yoy'] == pytest.approx(100 * (trade[-1][field] / trade[-5][field] - 1))


def test_missing_native_inputs_stay_missing_in_matched_windows():
    snapshot = official()
    snapshot['groups']['gdp']['rows'][-1]['gdp'] = None
    snapshot['groups']['debt']['rows'][-1]['debt_banks'] = None
    snapshot['groups']['currency']['rows'] = [row for row in snapshot['groups']['currency']['rows'] if row['date'] != '2026-05-01']
    snapshot['groups']['investment']['rows'][-1]['equity_assets_cash'] = None
    row = build_australia_view(snapshot)['macro']['quarterly'][-1]
    for field in ('current_account_gdp_pct', 'reserves_debt_pct', 'currency_index', 'net_equity'): assert row[field] is None


def test_country_refresh_retains_failed_family_timestamp_accepts_revisions_and_saves(tmp_path):
    service = AustraliaBOPService(tmp_path / 'au.json'); before = deepcopy(service.snapshot)
    def download(name):
        if name == 'currency': raise OSError('BIS temporarily unavailable')
        group = deepcopy(before['groups'][name]); group['checked_at'] = '2026-10-29T18:00:00+00:00'
        if name == 'gdp': group['rows'][-1]['gdp'] += 1
        return group
    service._download = download
    result = service.view(refresh=True)
    assert result['delivery'] == 'saved_snapshot' and len(result['macro']['warnings']) == 1
    assert service.snapshot['groups']['currency'] == before['groups']['currency']
    assert service.snapshot['groups']['gdp']['rows'][-1]['gdp'] == 743.502
    assert service.cache_path.exists() and not service.cache_path.with_suffix('.tmp').exists()
    assert AustraliaBOPService(service.cache_path, enabled=False).view()['latest'] == result['latest']


def test_coverage_regression_and_outage_keep_original_data_with_backoff(tmp_path):
    service = AustraliaBOPService(tmp_path / 'au.json'); before = deepcopy(service.snapshot); calls = []
    def download(name):
        calls.append(name)
        if name != 'accounts': raise OSError('Offline')
        group = deepcopy(before['groups'][name]); group['rows'][10]['bop_goods_nsa'] = None
        return group
    service._download = download
    result = service.view(refresh=True)
    assert len(result['macro']['warnings']) == 7 and service.snapshot['groups'] == before['groups']
    service.view(refresh=True)
    assert len(calls) == 7 and result['refresh_cooldown_seconds'] > 0


def test_workbook_allowance_is_bounded_without_changing_other_sources_default():
    body = fixture('530201.xlsx')
    with pytest.raises(BOPError): read_xlsx_sheet(body, 'Data1', max_expanded_bytes=60_000_001)
    with pytest.raises(BOPError): read_xlsx_sheet(body, 'Data1', max_expanded_bytes=1)
    assert read_xlsx_sheet(body, 'Data1')[9][0] == 'Series ID'


def test_australia_endpoint_has_aud_sources_and_removed_euro_area_is_unknown(tmp_path):
    client = TestClient(create_app(tmp_path / 'state.json'))
    assert [item['code'] for item in COUNTRIES] == ['USA', 'CHN', 'JPN', 'GBR', 'CAN', 'AUS', 'NZL', 'CHE', 'SGP']
    for frequency in ('quarterly', 'trailing4q'):
        response = client.get('/api/fx/bop', params={'country': 'aus', 'frequency': frequency, 'refresh': 'false'})
        assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
        data = response.json()
        assert data['country']['connected'] and data['reporting_currency'] == 'AUD'
        assert data['macro']['country_code'] == 'AUS' and data['macro']['frequency'] == frequency
        assert len(data['sources']) == 7 and not any(item['code'] == 'EUR' for item in data['countries'])
        assert all(source['latest_observation'] and source['checked_at'] for source in data['sources'])
    assert client.get('/api/fx/bop?country=EUR&refresh=false').status_code == 404
    with pytest.raises(BOPError): BOPService(tmp_path / 'usa.json', enabled=False).view('AUS')
