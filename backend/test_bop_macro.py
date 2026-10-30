from copy import deepcopy
import json
import pytest
from .bop import BOPError, SNAPSHOT as CORE_SNAPSHOT
from .bop_macro import (BOPMacroService, DebtDateMismatch, DebtLinks, GROUPS,
                        SNAPSHOT, build_macro_view, build_trade_view, parse_csv, parse_debt)


def official():
    return json.loads(SNAPSHOT.read_text(encoding='utf-8'))


def base():
    return json.loads(CORE_SNAPSHOT.read_text(encoding='utf-8'))['series']


def test_official_baseline_units_and_matched_gdp_denominator():
    snapshot = official()
    rows = base()
    result = build_macro_view(snapshot, rows)
    latest = result['quarterly'][-1]
    gdp = {row['date']: row['GDP'] for row in snapshot['groups']['quarterly']['rows']}
    denominator = sum(gdp[row['date']] / 4 for row in rows[-4:])
    assert latest['current_account_gdp_pct'] == pytest.approx(100 * -246.023 / (gdp[rows[-1]['date']] / 4))
    rolling = build_macro_view(snapshot, rows, 'trailing4q')
    assert rolling['quarterly'][-1]['current_account_gdp_pct'] == pytest.approx(100 * -942.557 / denominator)
    assert latest['reserve_transactions'] == 3.229
    assert latest['reserve_stock'] == 1306.544
    assert latest['reserves_debt_pct'] == pytest.approx(100 * 1306.544 / 11820.164)
    assert result['monthly'][-1]['period'] == '2026-08'
    assert result['monthly'][-1]['exports'] == 315.182
    assert result['monthly'][-1]['imports'] == 420.754
    assert result['monthly'][-1]['trade_balance'] == pytest.approx(-105.572)


def test_gdp_ratio_requires_four_consecutive_matching_quarters():
    rows = base()[-4:]
    snapshot = official()
    assert build_macro_view(snapshot, rows[:-1], 'trailing4q')['quarterly'][-1]['current_account_gdp_pct'] is None
    snapshot['groups']['quarterly']['rows'] = [r for r in snapshot['groups']['quarterly']['rows'] if r['date'] != rows[1]['date']]
    assert build_macro_view(snapshot, rows, 'trailing4q')['quarterly'][-1]['current_account_gdp_pct'] is None
    assert build_macro_view(official(), [rows[0], rows[2], rows[3]], 'trailing4q')['quarterly'][-1]['current_account_gdp_pct'] is None


def test_every_financial_flow_obeys_frequency_while_stock_ratios_remain_quarter_end():
    snapshot = official()
    quarterly = build_macro_view(snapshot, base(), 'quarterly')['quarterly']
    rolling = build_macro_view(snapshot, base(), 'trailing4q')['quarterly']
    for field in ('net_direct', 'net_equity', 'net_debt', 'net_other', 'reserve_transactions'):
        assert rolling[-1][field] == pytest.approx(sum(row[field] for row in quarterly[-4:]))
        assert rolling[0][field] is None
    for field in ('reserve_stock', 'reserves_debt_pct', 'import_cover_months'):
        assert rolling[-1][field] == quarterly[-1][field]
    assert quarterly[-1]['current_account_gdp_pct'] != rolling[-1]['current_account_gdp_pct']


def test_rolling_currency_uses_daily_observation_weights_and_never_sums_indices():
    dates = ['2025-07-01', '2025-10-01', '2026-01-01', '2026-04-01']
    quotes = [('2025-07-01', 100), ('2025-07-02', 120), ('2025-10-01', 130), ('2026-01-01', 160), ('2026-04-01', 190), ('2026-04-02', 210), ('2026-07-01', 999)]
    snapshot = {'groups': {'daily': {'checked_at': 'fixture', 'url': 'fixture', 'rows': [{'date': at, 'DTWEXBGS': value, 'DEXUSEU': value / 100} for at, value in quotes]}}}
    rows = [{'date': at} for at in dates]
    quarterly = build_macro_view(snapshot, rows, 'quarterly')['quarterly'][-1]
    rolling = build_macro_view(snapshot, rows, 'trailing4q')['quarterly'][-1]
    assert quarterly['broad_dollar'] == 200
    assert rolling['broad_dollar'] == pytest.approx(910 / 6)
    assert rolling['eurusd'] == pytest.approx(9.1 / 6)


def trade_months(quarters=8):
    return [{'date': f'{2024 + index // 12}-{index % 12 + 1:02d}-01', 'period': str(index), 'exports': index // 3 + 1, 'imports': 2 * (index // 3 + 1)} for index in range(quarters * 3)]


@pytest.mark.parametrize('frequency,exports,average,growth', [('quarterly', 24, 21, 100), ('trailing4q', 78, 66, 160)])
def test_trade_levels_averages_and_growth_follow_selected_frequency(frequency, exports, average, growth):
    monthly = trade_months() + [{'date': '2026-01-01', 'exports': 999, 'imports': 1998}, {'date': '2026-02-01', 'exports': 999, 'imports': 1998}]
    latest = build_trade_view(monthly, frequency)[-1]
    assert latest['period'] == '2025 Q4'  # January/February cannot make Q1.
    assert latest['exports'] == exports
    assert latest['imports'] == exports * 2
    assert latest['trade_balance'] == -exports
    assert latest['exports_ma3'] == average
    assert latest['exports_yoy'] == pytest.approx(growth)


def test_incomplete_historical_trade_quarters_break_rolling_sums_and_calendar_lags():
    monthly = [row for row in trade_months(12) if row['date'] != '2025-05-01']
    quarterly = {row['period']: row for row in build_trade_view(monthly, 'quarterly')}
    rolling = {row['period']: row for row in build_trade_view(monthly, 'trailing4q')}
    assert quarterly['2025 Q2']['exports'] is None
    assert quarterly['2025 Q4']['exports'] == 24
    assert quarterly['2025 Q4']['exports_ma3'] is None
    assert quarterly['2026 Q2']['exports_yoy'] is None
    assert rolling['2025 Q4']['exports'] is None
    assert rolling['2026 Q1']['exports'] is None
    assert rolling['2026 Q2']['exports'] is not None


def test_missing_financial_quarter_is_not_compressed_into_a_four_quarter_sum():
    rows = base()[-5:]
    result = build_macro_view(official(), rows[:2] + rows[3:], 'trailing4q')['quarterly'][-1]
    assert all(result[field] is None for field in ('net_direct', 'net_equity', 'net_debt', 'net_other', 'reserve_transactions', 'broad_dollar'))


def test_api_passes_the_selected_frequency_to_all_companion_datasets(tmp_path):
    from fastapi.testclient import TestClient
    from .app import create_app
    app = create_app(tmp_path / 'frequency.sqlite3')
    client = TestClient(app)
    for frequency in ('quarterly', 'trailing4q'):
        response = client.get('/api/fx/bop', params={'frequency': frequency, 'refresh': 'false'})
        assert response.status_code == 200
        data = response.json()
        assert data['macro']['frequency'] == data['frequency'] == frequency
        assert data['macro']['trade'][-1]['period'] == '2026 Q2'
        expected = build_macro_view(app.state.bop_macro.snapshot, app.state.bop.snapshot['series'], frequency)
        assert data['macro']['quarterly'][-1] == expected['quarterly'][-1]
        assert data['macro']['trade'][-1] == expected['trade'][-1]


def test_net_inward_is_liabilities_minus_assets_not_gross_liabilities():
    snapshot = official()
    source = snapshot['groups']['quarterly']['rows'][-1]
    source.update(IEAADI=2000, IEAIDI=5000, IEAAPIE=7000, IEAIPIE=1000,
                  IEAAPID=-1000, IEAIPID=2000, IEAAOI=3000, IEAIOI=-1000)
    row = build_macro_view(snapshot, base())['quarterly'][-1]
    assert [row[k] for k in ('net_direct', 'net_equity', 'net_debt', 'net_other')] == [3, -6, 3, -4]


def test_import_coverage_uses_twelve_months_ending_at_quarter_end():
    snapshot = official()
    imports = [r['BOPTIMP'] for r in snapshot['groups']['monthly']['rows'] if '2025-07-01' <= r['date'] <= '2026-06-01']
    assert len(imports) == 12
    expected = 1306544 / (sum(imports) / 12)
    for row in snapshot['groups']['monthly']['rows']:
        if row['date'] > '2026-06-01':
            row['BOPTIMP'] = 999999999
    assert build_macro_view(snapshot, base())['quarterly'][-1]['import_cover_months'] == pytest.approx(expected)
    snapshot['groups']['monthly']['rows'] = [r for r in snapshot['groups']['monthly']['rows'] if r['date'] != '2026-01-01']
    assert build_macro_view(snapshot, base())['quarterly'][-1]['import_cover_months'] is None


def test_monthly_indicators_use_calendar_lags_and_preserve_gaps():
    rows = [{'date': f'{year}-{month:02d}-01', 'BOPTEXP': month * 1000, 'BOPTIMP': month * 2000}
            for year in (2025, 2026) for month in range(1, 13)]
    snapshot = {'groups': {'monthly': {'rows': rows, 'url': 'fixture', 'checked_at': 'fixture'}}}
    latest = build_macro_view(snapshot, [])['monthly'][-1]
    assert latest['exports_ma3'] == 11
    assert latest['imports_ma3'] == 22
    assert latest['exports_yoy'] == 0
    rows[:] = [r for r in rows if r['date'] not in ('2026-11-01', '2025-12-01')]
    latest = build_macro_view(snapshot, [])['monthly'][-1]
    assert latest['exports_ma3'] is None
    assert latest['exports_yoy'] is None


def test_currency_panels_average_native_daily_quotes_and_exclude_partial_quarter():
    snapshot = {'groups': {'daily': {'rows': [
        {'date': '2026-04-01', 'DTWEXBGS': 100, 'DEXUSEU': 1.1},
        {'date': '2026-06-30', 'DTWEXBGS': 120, 'DEXUSEU': 1.3},
        {'date': '2026-07-01', 'DTWEXBGS': 999, 'DEXUSEU': 9},
    ], 'url': 'fixture', 'checked_at': 'fixture'}}}
    rows = [{'date': '2026-04-01'}, {'date': '2026-07-01'}]
    view = build_macro_view(snapshot, rows)['quarterly']
    assert view[0]['broad_dollar'] == 110
    assert view[0]['eurusd'] == pytest.approx(1.2)
    assert view[1]['broad_dollar'] is None


@pytest.mark.parametrize('body,frequency', [
    ('observation_date,WRONG\n2026-01-01,1\n', 'quarterly'),
    ('observation_date,GDP\n2026-02-01,1\n', 'quarterly'),
    ('observation_date,GDP\n2026-01-01,1\n2026-01-01,2\n', 'quarterly'),
    ('observation_date,GDP\n2026-01-01,NaN\n', 'quarterly'),
    ('observation_date,GDP\n2026-01-01,.\n', 'quarterly'),
])
def test_invalid_or_silently_omitted_fred_data_is_rejected(body, frequency):
    with pytest.raises((BOPError, ValueError)):
        parse_csv(body, ('GDP',), frequency)


def debt_table(heading='June 30, 2026'):
    return f'<table><tr><td>Outstanding External Debt as of {heading}</td></tr><tr><td>Total 2/</td><td>15,000</td></tr><tr><td>Principal</td><td>12,000</td><td></td></tr><tr><td>Interest</td><td>3,000</td></tr></table>'


def test_debt_parser_uses_total_principal_excludes_interest_and_checks_reported_date():
    assert parse_debt(debt_table(), '2026-04-01') == 12
    assert parse_debt(debt_table('Sept 30, 2026'), '2026-07-01') == 12
    with pytest.raises(DebtDateMismatch):
        parse_debt(debt_table('September 30, 2017'), '2017-04-01')
    with pytest.raises(BOPError):
        parse_debt(debt_table().replace('Principal', 'Unknown'), '2026-04-01')


def test_unquoted_debt_links_cannot_supply_arbitrary_download_hosts():
    parser = DebtLinks()
    parser.feed('<a href=https://untrusted.invalid/debtd2026q2.html>Q2</a><a href="https://invalid/other.html">Other</a><a href=/debtd2022q2.htm>Old format</a>')
    assert list(parser.links) == ['2026-04-01', '2022-04-01']
    assert list(parser.links.values())[0].startswith('https://ticdata.treasury.gov/')
    assert parser.links['2022-04-01'].endswith('debtd2022q2.htm')


def test_partial_refresh_keeps_failed_source_values_and_original_timestamp(tmp_path):
    service = BOPMacroService(tmp_path / 'cache.json', transport=lambda url: '')
    original = deepcopy(service.snapshot)
    def group(name):
        if name == 'quarterly':
            raise BOPError('unavailable')
        result = deepcopy(original['groups'][name])
        result['checked_at'] = 'new check'
        return result
    service._group = group
    service._debt = lambda: deepcopy(original['groups']['debt'])
    result = service.view(base(), refresh=True)
    assert service.snapshot['groups']['quarterly'] == original['groups']['quarterly']
    assert service.snapshot['groups']['monthly']['checked_at'] == 'new check'
    assert any('Quarterly source refresh failed' in warning for warning in result['warnings'])
    saved = json.loads((tmp_path / 'cache.json').read_text())
    assert saved['groups']['quarterly']['checked_at'] == original['groups']['quarterly']['checked_at']


def test_disabled_refresh_never_calls_network_and_missing_sources_stay_missing(tmp_path):
    def forbidden(url):
        raise AssertionError('Network must not be called')
    service = BOPMacroService(tmp_path / 'cache.json', enabled=False, transport=forbidden)
    assert service.view(base(), refresh=True)['monthly']
    service.snapshot = {'groups': {}}
    result = service.view(base(), refresh=True)
    assert result['status'] == 'unavailable'
    assert all(row['net_direct'] is None for row in result['quarterly'])


def test_treasury_wrong_quarter_remains_a_gap_instead_of_being_relabelled(tmp_path):
    def transport(url):
        if 'system-home-page' in url:
            return '<a href=/debtd2017q2.html>Q2</a><a href=/debtd2026q2.html>Q2</a>'
        return debt_table('September 30, 2017') if '2017q2' in url else debt_table()
    service = BOPMacroService(tmp_path / 'cache.json', transport=transport)
    service.snapshot = {'groups': {}}
    group = service._debt()
    assert [row['date'] for row in group['rows']] == ['2026-04-01']
    assert len(group['warnings']) == 1
