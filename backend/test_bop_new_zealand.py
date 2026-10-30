from copy import deepcopy
import csv
import io
import json
import zipfile
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .bop import BOPError, BOPService
from .bop_new_zealand import (NewZealandBOPService, SNAPSHOT, SPEC, CATALOGUE, GROUP_NAMES,
                             ExportForm, discover_csv, parse_stats_csv, parse_infoshare_trade,
                             parse_bis_nzd, validate_new_zealand, build_new_zealand_view)


def official(): return json.loads(SNAPSHOT.read_text(encoding='utf-8'))


def fixture(name):
    with zipfile.ZipFile(SNAPSHOT.parent / 'new_zealand_parser_fixtures.zip') as archive: return archive.read(name)


def write_csv(rows, columns):
    stream = io.StringIO(newline=''); writer = csv.DictWriter(stream, fieldnames=columns); writer.writeheader(); writer.writerows(rows)
    return stream.getvalue().encode('utf-8')


@pytest.mark.parametrize('group', ['accounts', 'gdp'])
def test_all_selected_native_csv_values_and_metadata_match_saved_observations(group):
    rows, metadata = parse_stats_csv(fixture(group + '.csv'), group)
    assert rows == official()['groups'][group]['rows']
    assert {item['id'] for item in metadata} == {spec['id'] for spec in SPEC[group].values()}
    assert rows[-1]['date'] == '2026-04-01'


@pytest.mark.parametrize('damage', ['unit', 'magnitude', 'adjustment', 'subject', 'id', 'missing_period', 'duplicate', 'frequency', 'status', 'nan', 'start_truncated'])
def test_corrupt_or_incomplete_stats_nz_series_are_rejected(damage):
    reader = csv.DictReader(io.StringIO(fixture('accounts.csv').decode())); columns = reader.fieldnames; rows = list(reader)
    target = next(row for row in rows if row['Series_reference'] == 'BOPQ.S06AC100000000D' and row['Period'] == '2026.06')
    if damage == 'unit': target['UNITS'] = 'US Dollars'
    elif damage == 'magnitude': target['MAGNTUDE'] = '9'
    elif damage == 'adjustment': target['Series_title_1'] = 'Seasonally adjusted'
    elif damage == 'subject': target['Subject'] = 'Other country'
    elif damage == 'id': rows = [row for row in rows if row['Series_reference'] != target['Series_reference']]
    elif damage == 'missing_period': rows.remove(next(row for row in rows if row['Series_reference'] == target['Series_reference'] and row['Period'] == '2025.03'))
    elif damage == 'duplicate': rows.append(deepcopy(target))
    elif damage == 'frequency': target['Period'] = '2026.05'
    elif damage == 'status': target['STATUS'] = 'FORECAST'
    elif damage == 'nan': target['Data_value'] = 'NaN'
    else: rows.remove(next(row for row in rows if row['Series_reference'] == target['Series_reference']))
    with pytest.raises(BOPError): parse_stats_csv(write_csv(rows, columns), 'accounts')


def test_missing_and_confidential_values_are_not_zero_or_leaked():
    reader = csv.DictReader(io.StringIO(fixture('accounts.csv').decode())); columns = reader.fieldnames; rows = list(reader)
    target = next(row for row in rows if row['Series_reference'] == 'BOPQ.S06AC100000000D' and row['Period'] == '2026.06')
    for status, value, suppression in [('C', '1234', ''), ('F', '', ''), ('F', '1234', 'Y')]:
        target.update(STATUS=status, Data_value=value, Suppressed=suppression)
        assert parse_stats_csv(write_csv(rows, columns), 'accounts')[0][-1]['current_account'] is None


def test_current_download_discovery_accepts_next_release_and_rejects_foreign_or_ambiguous_urls():
    for quarter in ('june', 'september'):
        name = f'balance-of-payments-and-international-investment-position-{quarter}-2026-quarter.csv'
        data = json.dumps({'DocumentLink': '/assets/Uploads/BOP/' + name, 'FileName': name})
        # Real Stats NZ pages embed HTML-escaped JSON in attributes.
        from html import escape
        body = ('<main data-page="' + escape(data, quote=True) + '"></main>').encode()
        assert discover_csv(body, 'accounts') == 'https://www.stats.govt.nz/assets/Uploads/BOP/' + name
        with pytest.raises(BOPError): discover_csv(body.replace(b'/assets/Uploads/BOP/', b'https://other.example/'), 'accounts')
        with pytest.raises(BOPError): discover_csv(b'<p>no download</p>', 'accounts')


def test_export_form_selects_only_monthly_history_and_preserves_hidden_form_state():
    parser = ExportForm()
    parser.feed('<input type="hidden" name="__VIEWSTATE" value="fresh&amp;state"><select name="ctl00$MainContent$TimeVariableSelector$lbVariableOptions"><option value="1">2026M09</option><option value="2">1997M01</option><option value="3">1996M12</option><option value="4">2026Q2</option></select><select><option value="999">2026M09</option></select>')
    assert parser.fields == [('__VIEWSTATE', 'fresh&state')] and parser.periods == ['1', '2']


def test_native_monthly_merchandise_export_is_nzd_units_not_millions_and_retains_status():
    rows = parse_infoshare_trade(fixture('trade.csv'))
    assert rows == official()['groups']['trade']['rows']
    assert rows[-1] == {'date': '2026-09-01', 'exports': 6.466753919, 'imports': 8.437709382}
    assert rows[0]['date'] == '1997-01-01'


@pytest.mark.parametrize('damage', ['unit', 'scale', 'series', 'valuation', 'months', 'negative', 'truncated'])
def test_wrong_or_incomplete_monthly_trade_export_is_rejected(damage):
    body = fixture('trade.csv')
    if damage == 'unit': body = body.replace(b'Units = $;', b'Units = USD;')
    elif damage == 'scale': body = body.replace(b'Magnitude = Units;', b'Magnitude = Millions;')
    elif damage == 'series': body = body.replace(b'Series is IMPM.SCT99C', b'Series is EXPM.SCT99F')
    elif damage == 'valuation': body = body.replace(b'CIF', b'FOB')
    elif damage == 'months': body = body.replace(b',Jan,,Feb,', b',Feb,,Jan,')
    elif damage == 'negative': body = body.replace(b'1468359697', b'-1468359697')
    else: body = b'\n'.join(line for line in body.splitlines() if not line.startswith(b'1997,'))
    with pytest.raises(BOPError): parse_infoshare_trade(body)


def test_bis_only_accepts_new_zealand_monthly_nominal_broad_index():
    body = fixture('bis-nzd.csv'); rows = parse_bis_nzd(body)
    assert rows == official()['groups']['currency']['rows'] and rows[-1]['date'] == '2026-09-01'
    for damaged in (body.replace(b',B,NZ,', b',B,AU,'), body.replace(b',882,', b',999,'), body + body.splitlines()[-1] + b'\n'):
        with pytest.raises(BOPError): parse_bis_nzd(damaged)


def test_cash_flow_signs_and_exact_four_inward_categories():
    view = build_new_zealand_view(official(), 'quarterly'); row = view['macro']['quarterly'][-1]
    assert row['net_direct'] == pytest.approx(4.435 - (-.111))
    assert row['net_equity'] == pytest.approx(1.639 - 4.063)
    assert row['net_debt'] == pytest.approx(1.288 - 4.221)
    assert row['net_other'] == pytest.approx(-.558 - (-4.010))
    assert row['reserve_transactions'] == .713
    assert view['latest']['financial_account'] == -4.631 and view['latest']['net_financial_inflow'] == 4.631
    assert view['latest']['net_lending'] == pytest.approx(-1.670 + .046)
    assert all(check['observations_checked'] for check in view['validation']['checks'])
    corrupted = deepcopy(official()['groups']['accounts']['rows']); corrupted[-1]['goods'] += 1
    with pytest.raises(BOPError): validate_new_zealand(corrupted)


def test_matching_actual_quarter_gdp_and_four_quarter_flows_with_published_rounding():
    q = build_new_zealand_view(official(), 'quarterly'); r = build_new_zealand_view(official())
    assert q['latest']['current_account'] == -1.67 and r['latest']['current_account'] == -14.584
    assert q['macro']['quarterly'][-1]['current_account_gdp_pct'] == pytest.approx(-1.67 / 114.916 * 100)
    gdp = sum(row['gdp'] for row in official()['groups']['gdp']['rows'][-4:])
    assert r['macro']['quarterly'][-1]['current_account_gdp_pct'] == pytest.approx(-14.584 / gdp * 100)
    assert round(r['macro']['quarterly'][-1]['current_account_gdp_pct'], 1) == -3.2
    for field in ('current_account', 'goods', 'services', 'primary_income', 'secondary_income', 'goods_gap'):
        assert r['latest'][field] == pytest.approx(sum(row[field] for row in q['series'][-4:]), abs=1e-6)


def test_reserve_stocks_and_published_sector_debt_are_never_summed():
    q = build_new_zealand_view(official(), 'quarterly')['macro']['quarterly']; r = build_new_zealand_view(official())['macro']['quarterly']
    assert q[-1]['reserve_stock'] == 52.036
    assert q[-1]['short_term_external_debt'] == pytest.approx(9.966 + .727 + 70.203 + 13.467)
    assert q[-1]['reserves_debt_pct'] == pytest.approx(52.036 / 94.363 * 100)
    for field in ('reserve_stock', 'short_term_external_debt', 'reserves_debt_pct'): assert q[-1][field] == r[-1][field]
    for field in ('net_direct', 'net_equity', 'net_debt', 'net_other', 'reserve_transactions'):
        assert r[-1][field] == pytest.approx(sum(row[field] for row in q[-4:]))


def test_complete_monthly_trade_totals_average_and_yoy_use_full_history():
    q = build_new_zealand_view(official(), 'quarterly'); r = build_new_zealand_view(official())
    assert q['macro']['trade_scope'] == 'merchandise' and q['macro']['trade'][-1]['date'] == '2026-07-01'
    for field in ('exports', 'imports'):
        assert q['macro']['trade'][-1][field] == pytest.approx(sum(row[field] for row in q['macro']['monthly'][-3:]))
        values = r['macro']['trade']
        assert values[-1][field] == pytest.approx(sum(row[field] for row in q['macro']['trade'][-4:]))
        assert values[-1][field + '_ma3'] == pytest.approx(sum(row[field] for row in values[-3:]) / 3)
        assert values[-1][field + '_yoy'] == pytest.approx((values[-1][field] / values[-5][field] - 1) * 100)
    broken = official(); broken['groups']['trade']['rows'][-1]['imports'] = None
    incomplete = build_new_zealand_view(broken)['macro']['trade']
    assert incomplete[-1]['date'] == '2026-04-01' and not any(row['date'] == '2026-07-01' for row in incomplete)


def test_missing_debt_gdp_or_currency_inputs_remain_gaps():
    broken = official(); broken['groups']['gdp']['rows'][-1]['gdp'] = None
    broken['groups']['accounts']['rows'][-1]['debt_banks'] = None
    broken['groups']['currency']['rows'] = [row for row in broken['groups']['currency']['rows'] if row['date'] != '2026-05-01']
    row = build_new_zealand_view(broken)['macro']['quarterly'][-1]
    assert row['current_account_gdp_pct'] is None and row['reserves_debt_pct'] is None and row['currency_index'] is None


def test_partial_refresh_keeps_failed_family_original_check_time_and_persists_revision(tmp_path):
    service = NewZealandBOPService(tmp_path / 'nz.json'); before = deepcopy(service.snapshot)
    def download(name):
        if name == 'currency': raise OSError('BIS unavailable')
        group = deepcopy(before['groups'][name]); group['checked_at'] = '2026-10-30T00:00:00+00:00'
        if name == 'gdp': group['rows'][-1]['gdp'] += 1
        return group
    service._download = download; view = service.view(refresh=True)
    assert view['delivery'] == 'saved_snapshot' and len(view['macro']['warnings']) == 1
    assert service.snapshot['groups']['currency'] == before['groups']['currency']
    assert service.snapshot['groups']['gdp']['rows'][-1]['gdp'] == 115.916
    assert NewZealandBOPService(service.cache_path, enabled=False).view()['latest'] == view['latest']


def test_regressed_coverage_outage_and_backoff_do_not_replace_verified_observations(tmp_path):
    service = NewZealandBOPService(tmp_path / 'nz.json'); before = deepcopy(service.snapshot); calls = []
    def download(name):
        calls.append(name)
        if name != 'accounts': raise OSError('Offline')
        group = deepcopy(before['groups'][name]); group['rows'][-1]['current_account'] = None; return group
    service._download = download; view = service.view(refresh=True); service.view(refresh=True)
    assert len(calls) == 4 and len(view['macro']['warnings']) == 4 and view['refresh_cooldown_seconds'] > 0
    assert service.snapshot['groups'] == before['groups']


def test_new_zealand_api_uses_its_own_adapter_and_no_store_with_no_country_leakage(tmp_path):
    client = TestClient(create_app(tmp_path / 'state.json'))
    for frequency in ('quarterly', 'trailing4q'):
        response = client.get('/api/fx/bop', params={'country': 'nzl', 'frequency': frequency, 'refresh': 'false'})
        assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
        data = response.json()
        assert data['country']['connected'] and data['country']['currency'] == data['reporting_currency'] == 'NZD'
        assert len(data['sources']) == 4 and data['macro']['country_code'] == 'NZL' and data['macro']['frequency'] == frequency
        assert any(item['code'] == 'NZL' for item in data['countries']) and not any(item['code'] == 'EUR' for item in data['countries'])
    with pytest.raises(BOPError): BOPService(tmp_path / 'usa.json', enabled=False).view('NZL')
    assert client.get('/api/fx/bop?country=EUR&refresh=false').status_code == 404
