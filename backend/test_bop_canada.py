from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import csv
import io
import json
import time
import zipfile
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .bop import BOPError, BOPService
from .bop_canada import (CanadaBOPService, SNAPSHOT, SPEC, GROUP_TABLES, GROUP_NAMES, CURRENCY,
                         build_canada_view, parse_statcan, parse_bis_cad, validate_canada, fetch_canada)


def official(): return json.loads(SNAPSHOT.read_text(encoding='utf-8'))


def fixture(name):
    with zipfile.ZipFile(SNAPSHOT.parent / 'canada_parser_fixtures.zip') as archive: return archive.read(name)


def records(pid):
    with zipfile.ZipFile(io.BytesIO(fixture(pid + '.zip'))) as archive:
        return list(csv.DictReader(io.TextIOWrapper(archive.open(pid + '.csv'), encoding='utf-8-sig')))


def archive(pid, rows):
    output = io.StringIO(newline=''); writer = csv.DictWriter(output, fieldnames=rows[0]); writer.writeheader(); writer.writerows(rows)
    body = io.BytesIO()
    with zipfile.ZipFile(body, 'w', zipfile.ZIP_DEFLATED) as result: result.writestr(pid + '.csv', output.getvalue().encode('utf-8-sig'))
    return body.getvalue()


@pytest.mark.parametrize('pid', list(SPEC))
def test_native_canadian_vectors_labels_units_dates_and_saved_values(pid):
    group = next(name for name, tables in GROUP_TABLES.items() if pid in tables)
    saved = {row['date']: row for row in official()['groups'][group]['rows']}
    rows, metadata = parse_statcan(fixture(pid + '.zip'), pid)
    assert len(metadata) == len(SPEC[pid]['fields'])
    for row in rows:
        for field in SPEC[pid]['fields']: assert saved[row['date']][field] == row[field]


@pytest.mark.parametrize('damage', ['currency', 'unit', 'scale', 'adjustment', 'geography', 'coordinate', 'id', 'duplicate', 'missing_date', 'first_date', 'future', 'nonquarter', 'nan', 'unknown_status', 'suppressed_value', 'terminated'])
def test_canadian_source_corruption_is_rejected(damage):
    pid = '36100104'; rows = records(pid); row = rows[-1]
    if damage == 'currency': row['UOM'] = 'US dollars'
    elif damage == 'unit': row['UOM'] = 'Percent'
    elif damage == 'scale': row['SCALAR_FACTOR'] = 'units'
    elif damage == 'adjustment': row['Seasonal adjustment'] = 'Seasonally adjusted at annual rates'
    elif damage == 'geography': row['GEO'] = 'United States'
    elif damage == 'coordinate': row['COORDINATE'] = '1.2.1.30'
    elif damage == 'id': row['VECTOR'] = 'v_other'
    elif damage == 'duplicate': rows.append(deepcopy(row))
    elif damage == 'missing_date': rows.pop(-3)
    elif damage == 'first_date': rows.pop(0)
    elif damage == 'future': row['REF_DATE'] = '2099-01'
    elif damage == 'nonquarter': row['REF_DATE'] = '2026-05'
    elif damage == 'nan': row['VALUE'] = 'NaN'
    elif damage == 'unknown_status': row['STATUS'] = 'unknown'
    elif damage == 'suppressed_value': row['STATUS'] = 'x'
    else: row['TERMINATED'] = 't'
    # A changed vector drops the final native date; initial parsing alone cannot
    # know a newer release exists. The service must reject coverage regression.
    if damage == 'id':
        rows[0]['VECTOR'] = 'v_other'
    with pytest.raises(BOPError): parse_statcan(archive(pid, rows), pid)


def test_canadian_missing_and_suppressed_observations_remain_gaps():
    rows = records('36100104'); rows[-1]['VALUE'] = ''; rows[-1]['STATUS'] = 'x'
    assert parse_statcan(archive('36100104', rows), '36100104')[0][-1]['gdp'] is None
    assert any(row['reserve_other'] is None for row in official()['groups']['positions']['rows'])


def test_bis_cad_country_basket_missing_months_and_duplicates():
    body = fixture('bis-cad.csv'); assert parse_bis_cad(body) == official()['groups']['currency']['rows']
    for damaged in (body.replace(b',CA,', b',US,'), body.replace(b',N,B,', b',R,B,'), b'not a csv'):
        with pytest.raises(BOPError): parse_bis_cad(damaged)
    lines = body.splitlines()
    for damaged in (b'\n'.join(lines[:100] + lines[101:]), body + b'\n' + lines[-1]):
        with pytest.raises(BOPError): parse_bis_cad(damaged)


def test_fetch_is_limited_to_the_fixed_canadian_official_downloads():
    for url in ('http://www150.statcan.gc.ca/n1/tbl/csv/36100016-eng.zip', 'https://example.com/data', 'https://stats.bis.org/api/v1/data/WS_EER/M.N.B.US?format=csv'):
        with pytest.raises(BOPError): fetch_canada(url)


def test_published_rounding_is_checked_without_altering_source_numbers():
    snapshot = official(); result = build_canada_view(snapshot, 'quarterly')
    assert len(result['validation']['checks']) == 12
    assert result['latest']['current_account'] == 5.041
    assert result['latest']['current_account'] != sum(result['latest'][field] for field in ('goods', 'services', 'primary_income', 'secondary_income'))
    broken = deepcopy(snapshot['groups']['accounts']['rows']); broken[-1]['goods'] += .01
    with pytest.raises(BOPError): validate_canada(broken, 'accounts')


def test_gdp_uses_matching_actual_unadjusted_quarters_never_annual_rates():
    q = build_canada_view(official(), 'quarterly'); r = build_canada_view(official())
    assert q['macro']['quarterly'][-1]['current_account_gdp_pct'] == pytest.approx(5.041 / 847.438 * 100)
    ca = sum(row['current_account'] for row in q['series'][-4:]); gdp = sum(row['gdp'] for row in official()['groups']['gdp']['rows'][-4:])
    assert r['latest']['current_account'] == pytest.approx(ca)
    assert r['macro']['quarterly'][-1]['current_account_gdp_pct'] == pytest.approx(ca / gdp * 100)


def test_inward_investment_signs_and_reserve_flow_are_published_transactions():
    row = build_canada_view(official(), 'quarterly')['macro']['quarterly'][-1]
    for field, expected in [('net_direct', 25.888 - 17.130), ('net_equity', -9.551 - 35.543), ('net_debt', 110.195 - 10.014), ('net_other', 19.074 - 93.435), ('reserve_transactions', 1.187)]:
        assert row[field] == pytest.approx(expected)


def test_reserves_and_short_term_debt_are_matching_cad_stocks_in_both_modes():
    q = build_canada_view(official(), 'quarterly')['macro']['quarterly']; r = build_canada_view(official())['macro']['quarterly']
    assert q[-1]['reserve_stock'] == 179.662 and q[-1]['short_term_external_debt'] == 1861.342
    assert q[-1]['reserves_debt_pct'] == pytest.approx(179.662 / 1861.342 * 100)
    for field in ('reserve_stock', 'short_term_external_debt', 'reserves_debt_pct'): assert q[-1][field] == r[-1][field]
    for field in ('net_direct', 'net_equity', 'net_debt', 'net_other', 'reserve_transactions'):
        assert r[-1][field] == pytest.approx(sum(row[field] for row in q[-4:]))


def test_customs_complete_months_and_bop_goods_services_trade_calculations():
    q = build_canada_view(official(), 'quarterly'); r = build_canada_view(official()); monthly = q['macro']['monthly']
    assert monthly[-1]['exports'] == pytest.approx(74.8128 + 20.966)
    assert monthly[-1]['imports'] == pytest.approx(74.2481 + 20.918)
    assert q['macro']['trade'][-1]['date'] == '2026-04-01'  # July/August never project September.
    assert next(row for row in monthly if row['date'] == '2006-12-01')['exports'] is None
    quarter_months = [row for row in monthly if '2026-04-01' <= row['date'] <= '2026-06-01']
    assert q['latest']['customs_exports'] == pytest.approx(sum(row['customs_exports'] for row in quarter_months))
    for field in ('exports', 'imports'):
        rows = r['macro']['trade']
        assert rows[-1][field] == pytest.approx(sum(row[field] for row in q['macro']['trade'][-4:]))
        assert rows[-1][field + '_ma3'] == pytest.approx(sum(row[field] for row in rows[-3:]) / 3)
        assert rows[-1][field + '_yoy'] == pytest.approx((rows[-1][field] / rows[-5][field] - 1) * 100)


def test_missing_inputs_cannot_become_apparent_ratios_or_flows():
    snapshot = official(); snapshot['groups']['gdp']['rows'][-1]['gdp'] = None
    snapshot['groups']['debt']['rows'][-1]['short_term_external_debt'] = None
    snapshot['groups']['currency']['rows'] = [row for row in snapshot['groups']['currency']['rows'] if row['date'] != '2026-05-01']
    snapshot['groups']['investment']['rows'][-1]['equity_assets'] = None
    row = build_canada_view(snapshot)['macro']['quarterly'][-1]
    for field in ('current_account_gdp_pct', 'reserves_debt_pct', 'currency_index', 'net_equity'): assert row[field] is None


def test_partial_refresh_retains_failed_time_and_accepts_revisions_atomically(tmp_path):
    service = CanadaBOPService(tmp_path / 'ca.json'); before = deepcopy(service.snapshot)
    def download(name):
        if name == 'currency': raise OSError('BIS unavailable')
        group = deepcopy(before['groups'][name]); group['checked_at'] = '2026-10-30T01:00:00+00:00'
        if name == 'gdp': group['rows'][-1]['gdp'] += 1
        return group
    service._download = download; view = service.view(refresh=True)
    assert view['delivery'] == 'saved_snapshot' and len(view['macro']['warnings']) == 1
    assert service.snapshot['groups']['currency'] == before['groups']['currency']
    assert service.snapshot['groups']['gdp']['rows'][-1]['gdp'] == 848.438
    assert CanadaBOPService(service.cache_path, enabled=False).view()['latest'] == view['latest']


def test_regressed_coverage_outage_and_backoff_preserve_observations(tmp_path):
    service = CanadaBOPService(tmp_path / 'ca.json'); before = deepcopy(service.snapshot); calls = []
    def download(name):
        calls.append(name)
        if name != 'debt': raise OSError('Offline')
        group = deepcopy(before['groups'][name]); group['rows'][-1]['short_term_external_debt'] = None; return group
    service._download = download; view = service.view(refresh=True); service.view(refresh=True)
    assert len(calls) == 7 and len(view['macro']['warnings']) == 7 and view['refresh_cooldown_seconds'] > 0
    assert service.snapshot['groups'] == before['groups']


def test_conflicting_source_vintages_do_not_replace_the_saved_dataset(tmp_path):
    service = CanadaBOPService(tmp_path / 'ca.json'); before = deepcopy(service.snapshot)
    def download(name):
        group = deepcopy(before['groups'][name])
        if name == 'investment':
            row = group['rows'][-1]
            for field in ('assets_total', 'direct_assets', 'financial_account'): row[field] += 1
        return group
    service._download = download; view = service.view(refresh=True)
    assert service.snapshot['groups'] == before['groups'] and 'reconciled' in view['macro']['warnings'][0]


def test_simultaneous_clients_share_one_source_refresh(tmp_path):
    service = CanadaBOPService(tmp_path / 'ca.json'); calls = []
    def download(name):
        calls.append(name); time.sleep(.05); return deepcopy(service.snapshot['groups'][name])
    service._download = download
    with ThreadPoolExecutor(max_workers=4) as pool: results = list(pool.map(lambda _: service.view(refresh=True), range(4)))
    assert len(calls) == len(GROUP_NAMES) and all(row['delivery'] == 'source_checked' for row in results)


def test_canadian_endpoint_is_country_specific_no_store_and_live_by_default(tmp_path):
    app = create_app(tmp_path / 'state.json'); client = TestClient(app)
    for frequency in ('quarterly', 'trailing4q'):
        response = client.get('/api/fx/bop', params={'country': 'can', 'frequency': frequency, 'refresh': 'false'})
        assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
        data = response.json()
        assert data['country']['connected'] and data['country']['currency'] == data['reporting_currency'] == 'CAD'
        assert len(data['sources']) == 7 and data['macro']['country_code'] == 'CAN' and data['macro']['frequency'] == frequency
        assert not any(item['code'] == 'EUR' for item in data['countries'])
    calls = []; app.state.bop_canada.enabled = True
    def download(name): calls.append(name); return deepcopy(app.state.bop_canada.snapshot['groups'][name])
    app.state.bop_canada._download = download
    assert client.get('/api/fx/bop?country=CAN').json()['delivery'] == 'source_checked' and len(calls) == 7
    with pytest.raises(BOPError): BOPService(tmp_path / 'usa.json', enabled=False).view('CAN')
