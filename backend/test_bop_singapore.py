from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
import json
from threading import Event
import zipfile
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .bop import BOPError, BOPService
from .bop_singapore import (SingaporeBOPService, SNAPSHOT, API_ROOT, TABLES, CURRENCY_URL,
                            build_singapore_view, parse_singstat, parse_bis_sgd, period_date, _number)


def official():
    return json.loads(SNAPSHOT.read_text(encoding='utf-8'))


def fixture(url):
    key = sha256(url.encode()).hexdigest()
    with zipfile.ZipFile(SNAPSHOT.parent / 'singapore_parser_fixtures.zip') as archive:
        return archive.read(key + '.txt').decode('utf-8')


def bop_inputs():
    meta = json.loads(fixture(API_ROOT + 'metadata/M060171'))
    pages = [(offset, json.loads(fixture(API_ROOT + 'tabledata/M060171' + (f'?offset={offset}' if offset else '')))) for offset in (0, 5000, 10000)]
    return meta, pages


def test_complete_singstat_pages_merge_split_series_and_convert_sgd_millions():
    meta, pages = bop_inputs()
    assert sum(len(row['columns']) for _, page in pages for row in page['Data']['row']) == 11202
    rows, sources = parse_singstat(meta, pages, 'bop')
    assert len(rows) == 118 and rows[-1]['date'] == '2026-04-01'
    assert rows[-1]['current_account'] == pytest.approx(37.0774)
    assert rows[-1]['other_liabilities'] == pytest.approx(50.7768)  # Row crosses the 5,000-cell page boundary.
    assert rows[-1]['reserve_transactions'] == pytest.approx(9.0151)
    assert all(source['native_unit'] == 'Million Dollars' for source in sources)


@pytest.mark.parametrize('damage', ['missing_page', 'repeated_page', 'offset', 'vintage', 'unit', 'frequency', 'series', 'nan', 'identity', 'count'])
def test_incomplete_or_changed_singstat_release_is_rejected(damage):
    meta, pages = bop_inputs()
    if damage == 'missing_page': pages.pop()
    elif damage == 'repeated_page': pages.append(deepcopy(pages[-1]))
    elif damage == 'offset': pages[1][1]['Data']['offset'] = '0'
    elif damage == 'vintage': pages[1][1]['Data']['dataLastUpdated'] = '01/01/2000'
    elif damage == 'unit': pages[0][1]['Data']['row'][0]['uoM'] = 'US Dollars'
    elif damage == 'frequency': meta['Data']['records']['frequency'] = 'Annual'
    elif damage == 'series': pages[0][1]['Data']['row'][0]['seriesNo'] = '99'
    elif damage == 'nan': pages[0][1]['Data']['row'][0]['columns'][-1]['value'] = 'NaN'
    elif damage == 'identity': pages[0][1]['Data']['row'][0]['columns'][-1]['value'] = '100'
    else: meta['Data']['records']['total'] += 1
    with pytest.raises(BOPError): parse_singstat(meta, pages, 'bop')


def test_all_six_downloaders_accept_native_official_responses_without_network(tmp_path):
    service = SingaporeBOPService(tmp_path / 'sg.json', transport=fixture)
    result = service.view(refresh=True)
    assert result['delivery'] == 'source_checked' and result['macro']['warnings'] == []
    assert len(result['sources']) == 6
    assert service.cache_path.exists() and not service.cache_path.with_suffix('.tmp').exists()
    assert SingaporeBOPService(service.cache_path, enabled=False).view()['latest'] == result['latest']


def test_native_nsa_gdp_is_actual_quarter_and_inward_signs_reverse_dos_net_outflows():
    v = build_singapore_view(official(), 'quarterly')
    row = v['macro']['quarterly'][-1]
    assert row['current_account_gdp_pct'] == pytest.approx(37.0774 / 216.3393 * 100)
    assert row['net_direct'] == pytest.approx(58.6195 - 41.9605)
    assert row['net_portfolio'] == pytest.approx(12.071 - 53.5543)
    assert row['net_other'] == pytest.approx(50.7768 - 55.8888)
    assert row['net_equity'] is None and row['net_debt'] is None
    assert row['reserve_transactions'] == pytest.approx(9.0151)
    assert v['macro']['portfolio_split'] == 'combined'
    assert v['latest']['capital_account'] is None and v['latest']['financial_account'] is None
    assert v['seasonal_adjustment'] == 'Not seasonally adjusted'


def test_rolling_flows_and_ratios_use_matched_windows_and_never_sum_stocks():
    snapshot = official()
    q = build_singapore_view(snapshot, 'quarterly')
    r = build_singapore_view(snapshot, 'trailing4q')
    assert r['latest']['current_account'] == pytest.approx(146.6131)
    for field in ('goods', 'services', 'primary_income', 'secondary_income', 'customs_goods', 'goods_gap'):
        assert r['latest'][field] == pytest.approx(sum(row[field] for row in q['series'][-4:]))
    for field in ('net_direct', 'net_portfolio', 'net_other', 'reserve_transactions'):
        assert r['macro']['quarterly'][-1][field] == pytest.approx(sum(row[field] for row in q['macro']['quarterly'][-4:]))
    for field in ('reserve_stock', 'short_term_external_debt', 'reserves_debt_pct'):
        assert r['macro']['quarterly'][-1][field] == q['macro']['quarterly'][-1][field]
    assert q['macro']['quarterly'][-1]['short_term_external_debt'] == pytest.approx(1676.3071 + 464.0881)
    gdps = snapshot['groups']['gdp']['rows'][-4:]
    assert r['macro']['quarterly'][-1]['current_account_gdp_pct'] == pytest.approx(146.6131 / sum(row['gdp'] for row in gdps) * 100)
    currency = snapshot['groups']['currency']['rows']
    expected = sum(row['currency_index'] for row in currency if '2025-07-01' <= row['date'] <= '2026-06-01') / 12
    assert r['macro']['quarterly'][-1]['currency_index'] == pytest.approx(expected)


def test_monthly_goods_use_thousand_sgd_conversion_include_reexports_and_complete_quarters():
    v = build_singapore_view(official(), 'quarterly')
    latest = v['macro']['monthly'][-1]
    assert latest['date'] == '2026-09-01'
    assert latest['exports'] == 94.425316 and latest['imports'] == 84.348034
    assert v['macro']['trade_scope'] == 'merchandise'
    trade = v['macro']['trade']
    assert trade[-1]['date'] == '2026-07-01'  # Later trade does not create a BOP quarter.
    assert v['latest']['date'] == '2026-04-01'
    assert trade[-1]['exports_ma3'] == pytest.approx(sum(row['exports'] for row in trade[-3:]) / 3)
    r = build_singapore_view(official())['macro']['trade']
    assert r[-1]['exports'] == pytest.approx(sum(row['exports'] for row in trade[-4:]))
    assert r[-1]['imports_yoy'] == pytest.approx(100 * (r[-1]['imports'] / r[-5]['imports'] - 1))
    assert r[-1]['trade_balance'] == pytest.approx(r[-1]['exports'] - r[-1]['imports'])


def test_nil_is_zero_missing_is_unknown_and_periods_are_strict():
    assert _number('-') == 0 and _number('na') is None and _number(None) is None
    assert period_date('2026 2Q', 'Quarterly') == '2026-04-01'
    assert period_date('2026 Sep', 'Monthly') == '2026-09-01'
    with pytest.raises(BOPError): _number('unknown')
    with pytest.raises(BOPError): period_date('2026 5Q', 'Quarterly')


def test_direct_bis_series_is_monthly_nominal_broad_singapore_with_latest_release():
    body = fixture(CURRENCY_URL)
    assert parse_bis_sgd(body)[-1] == {'date': '2026-09-01', 'currency_index': 114.17}
    for changed in (body.replace(',B,SG,', ',B,JP,'), body.replace(',882,', ',999,'),
                    body.replace('\nM,N,', '\nQ,N,'), body + body.splitlines()[-1] + '\n'):
        with pytest.raises(BOPError): parse_bis_sgd(changed)
    with pytest.raises(BOPError): parse_bis_sgd(body.replace(',A,F,', ',F,F,'))


def test_missing_inputs_and_nonzero_government_debt_leave_coverage_unavailable():
    snapshot = official()
    snapshot['groups']['debt']['rows'][-1]['government_total'] = 1
    snapshot['groups']['gdp']['rows'][-1]['gdp'] = None
    snapshot['groups']['currency']['rows'] = [row for row in snapshot['groups']['currency']['rows'] if row['date'] != '2026-05-01']
    snapshot['groups']['reserves']['rows'] = [row for row in snapshot['groups']['reserves']['rows'] if row['date'] != '2026-06-01']
    row = build_singapore_view(snapshot)['macro']['quarterly'][-1]
    for field in ('current_account_gdp_pct', 'currency_index', 'reserve_stock', 'short_term_external_debt', 'reserves_debt_pct'):
        assert row[field] is None
    snapshot['groups']['customs']['rows'] = [row for row in snapshot['groups']['customs']['rows'] if row['date'] != '2026-08-01']
    assert build_singapore_view(snapshot)['macro']['trade'][-1]['date'] == '2026-04-01'


def test_outage_and_interior_regression_keep_values_times_and_failure_backoff(tmp_path):
    service = SingaporeBOPService(tmp_path / 'sg.json'); before = deepcopy(service.snapshot); calls = []
    def download(name):
        calls.append(name)
        if name != 'bop': raise OSError('Offline')
        group = deepcopy(before['groups'][name]); group['rows'][10]['portfolio_net'] = None
        return group
    service._download = download
    failed = service.view(refresh=True)
    assert len(failed['macro']['warnings']) == 6 and failed['delivery'] == 'saved_snapshot'
    assert service.snapshot['groups'] == before['groups']
    service.view(refresh=True)
    assert len(calls) == 6 and failed['refresh_cooldown_seconds'] > 0


def test_partial_refresh_accepts_revision_but_retains_failed_source_timestamp(tmp_path):
    service = SingaporeBOPService(tmp_path / 'sg.json'); before = deepcopy(service.snapshot)
    def download(name):
        if name == 'currency': raise OSError('BIS temporarily unavailable')
        group = deepcopy(before['groups'][name]); group['checked_at'] = '2026-10-29T17:00:00+00:00'
        if name == 'reserves': group['rows'][-1]['reserve_stock'] += 1
        return group
    service._download = download
    result = service.view(refresh=True)
    assert len(result['macro']['warnings']) == 1
    assert service.snapshot['groups']['currency'] == before['groups']['currency']
    assert service.snapshot['groups']['reserves']['rows'][-1]['reserve_stock'] == before['groups']['reserves']['rows'][-1]['reserve_stock'] + 1
    assert service.cache_path.exists()


def test_simultaneous_refreshes_share_one_source_check(tmp_path):
    service = SingaporeBOPService(tmp_path / 'sg.json'); before = deepcopy(service.snapshot)
    started, release, second_started = Event(), Event(), Event(); calls = []
    def download(name):
        calls.append(name); started.set(); assert release.wait(5)
        return deepcopy(before['groups'][name])
    service._download = download
    class ObservedLock:
        def __init__(self, lock): self.lock, self.attempts = lock, 0
        def __enter__(self):
            self.attempts += 1
            # view has recorded requested_at before trying this lock, so the
            # second request is provably pending when the first completes.
            if self.attempts == 2: second_started.set()
            self.lock.acquire()
            return self
        def __exit__(self, *args): self.lock.release()
    service.lock = ObservedLock(service.lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.view, refresh=True); assert started.wait(5)
        other = pool.submit(service.view, refresh=True); assert second_started.wait(5)
        release.set()
        assert first.result()['delivery'] == other.result()['delivery'] == 'source_checked'
    assert len(calls) == 6


def test_singapore_endpoint_isolated_sgd_with_no_store_and_actual_provenance(tmp_path):
    client = TestClient(create_app(tmp_path / 'state.json'))
    for frequency in ('quarterly', 'trailing4q'):
        response = client.get('/api/fx/bop', params={'country': 'sgp', 'frequency': frequency, 'refresh': 'false'})
        assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
        data = response.json()
        assert data['country']['connected'] and data['reporting_currency'] == 'SGD'
        assert data['macro']['country_code'] == 'SGP' and data['macro']['frequency'] == frequency
        assert data['macro']['portfolio_split'] == 'combined' and len(data['sources']) == 6
        assert all(source['latest_observation'] and source['checked_at'] for source in data['sources'])
        assert 'M.N.B.SG' in {s['id'] for source in data['sources'] for s in source['series']}
    with pytest.raises(BOPError): BOPService(tmp_path / 'usa.json', enabled=False).view('SGP')
    assert client.get('/api/fx/bop?country=EUR&refresh=false').status_code == 404
