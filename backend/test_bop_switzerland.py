from copy import deepcopy
import json
import zipfile
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .bop import BOPError, BOPService
from .bop_switzerland import (SwitzerlandBOPService, SNAPSHOT, SPEC, build_switzerland_view,
                             parse_snb, parse_seco_gdp, parse_bis_chf, fetch_switzerland, validate_swiss)


def official(): return json.loads(SNAPSHOT.read_text(encoding='utf-8'))


def fixture(name):
    with zipfile.ZipFile(SNAPSHOT.parent / 'switzerland_parser_fixtures.zip') as archive: return archive.read(name)


def test_every_selected_snb_series_matches_native_units_dates_and_saved_values():
    snapshot = official()
    for group, definitions in SPEC.items():
        saved = {row['date']: row for row in snapshot['groups'][group]['rows']}
        for definition in definitions:
            rows, meta = parse_snb(fixture(definition['cube'] + '.json'), definition)
            assert len(meta) == len(definition['fields'])
            for row in rows:
                for field in definition['fields']: assert saved[row['date']][field] == row[field]
    assert snapshot['groups']['trade']['rows'][-1]['date'] == '2026-08-01'
    assert snapshot['groups']['debt']['rows'][-1]['debt_banks'] > 600  # already billions, never divided by 1000


@pytest.mark.parametrize('damage', ['unit', 'scale', 'header', 'id', 'frequency', 'missing', 'duplicate_series', 'duplicate_date', 'missing_date', 'first_date', 'nan', 'boolean'])
def test_snb_schema_definition_and_data_corruption_are_rejected(damage):
    definition = SPEC['accounts'][0]; body = json.loads(fixture('bopoverq.json')); row = body['timeseries'][0]
    if damage == 'unit': row['metadata']['unit'] = 'In USD millions'
    elif damage == 'scale': row['metadata']['scale'] = '1000'
    elif damage == 'header': row['header'][0]['dimItem'] = 'Other account'
    elif damage == 'id': row['metadata']['key'] = 'OTHER_COUNTRY'
    elif damage == 'frequency': row['metadata']['frequency'] = 'P1Y'
    elif damage == 'missing': body['timeseries'].pop(0)
    elif damage == 'duplicate_series': body['timeseries'].append(deepcopy(row))
    elif damage == 'duplicate_date': row['values'].append(deepcopy(row['values'][-1]))
    elif damage == 'missing_date': row['values'].pop(-3)
    elif damage == 'first_date': row['values'].pop(0)
    elif damage == 'nan': row['values'][-1]['value'] = 'NaN'
    else: row['values'][-1]['value'] = True
    with pytest.raises(BOPError): parse_snb(json.dumps(body).encode(), definition)


def test_native_null_values_stay_missing_and_never_become_zero():
    definition = SPEC['accounts'][0]; body = json.loads(fixture('bopoverq.json'))
    body['timeseries'][0]['values'][-1]['value'] = None
    assert parse_snb(json.dumps(body).encode(), definition)[0][-1]['current_account'] is None


def test_seco_gdp_selects_unadjusted_current_price_actual_quarters():
    rows, meta = parse_seco_gdp(fixture('seco-gdp.csv'), fixture('seco-gdp.json'))
    assert rows == official()['groups']['gdp']['rows'] and rows[-1]['gdp'] == pytest.approx(226.43812945443)
    assert meta[0]['id'] == 'SECO.gdp.nom.na' and meta[0]['adjustment'] == 'NSA'


@pytest.mark.parametrize('damage', ['currency', 'adjustment', 'aggregate', 'missing_series', 'changed_scale', 'duplicate'])
def test_seco_rejects_changed_basis_units_or_history(damage):
    body = fixture('seco-gdp.csv'); meta = json.loads(fixture('seco-gdp.json'))
    if damage == 'currency': meta['units']['type']['nom']['en'] = 'Euros, at current prices'
    elif damage == 'adjustment': meta['labels']['seas_adj']['na']['en'] = 'Sports event adjusted'
    elif damage == 'aggregate': meta['labels']['structure']['gdp']['en'] = 'Real GDP'
    elif damage == 'missing_series': body = body.replace(b',nom,na,', b',nom,nasa,')
    elif damage == 'changed_scale': body = body.replace(b'226438.12945443', b'226438129454.43')
    else:
        line = next(line for line in body.splitlines() if line.startswith(b'gdp,nom,na,')); body += line + b'\n'
    with pytest.raises(BOPError): parse_seco_gdp(body, json.dumps(meta).encode())


def test_bis_is_swiss_franc_monthly_nominal_broad_only():
    body = fixture('bis-chf.csv'); rows = parse_bis_chf(body)
    assert rows == official()['groups']['currency']['rows'] and rows[-1]['date'] == '2026-09-01'
    for damaged in (body.replace(b',B,CH,', b',B,JP,'), body.replace(b',882,', b',999,'), body + body.splitlines()[-1] + b'\n'):
        with pytest.raises(BOPError): parse_bis_chf(damaged)


def test_transport_rejects_untrusted_hosts_before_network_access():
    for url in ('http://data.snb.ch/api/cube/test', 'https://other.example/data', 'https://data.snb.ch.other.example/data'):
        with pytest.raises(BOPError): fetch_switzerland(url)


def test_full_financial_account_adds_derivatives_and_native_reserve_sign_is_preserved():
    view = build_switzerland_view(official(), 'quarterly'); row = view['macro']['quarterly'][-1]
    assert view['latest']['financial_account'] == pytest.approx(5.800480098336394 + 1.173977676046304)
    assert view['latest']['net_financial_inflow'] == -view['latest']['financial_account']
    assert row['net_direct'] == pytest.approx(-10.104299013143644 - (-.850191031861727))
    assert row['net_equity'] == pytest.approx(2.522 - (-14.160706970083073))
    assert row['net_debt'] == pytest.approx(-6.243472904797291 - 10.029625816972637)
    assert row['net_other'] == pytest.approx(1.910933893584621 - (-3.8840862263133413))
    assert row['reserve_transactions'] == pytest.approx(2.7510004852655848)


def test_broad_customs_including_gold_reconciles_to_bop_with_merchanting_and_supplements():
    view = build_switzerland_view(official(), 'quarterly'); row = view['latest']
    assert row['customs_exports'] == pytest.approx(115.701459607) and row['customs_imports'] == pytest.approx(105.596539866)
    assert row['goods'] == pytest.approx(row['customs_goods_native'] + row['goods_merchanting'] + row['goods_supplements'])
    assert row['goods_gap'] == pytest.approx(row['customs_goods'] - row['goods'])
    assert row['goods_gap'] < -19
    assert any(check['name'] == 'goods_bridge' for check in view['validation']['checks'])


def test_legacy_income_residual_is_recorded_without_changing_published_figures():
    rows = official()['groups']['accounts']['rows']; result = validate_swiss(rows, 'accounts')
    income = next(check for check in result['checks'] if check['name'] == 'primary_income')
    assert income['max_difference_chf_billions'] == pytest.approx(.0002) and income['tolerance_chf_billions'] == .0002001
    broken = deepcopy(rows); broken[-1]['income_payments'] += .01
    with pytest.raises(BOPError): validate_swiss(broken, 'accounts')


def test_current_account_gdp_uses_actual_matching_quarters_and_four_quarter_flows():
    q = build_switzerland_view(official(), 'quarterly'); r = build_switzerland_view(official())
    assert q['macro']['quarterly'][-1]['current_account_gdp_pct'] == pytest.approx(23.65755439121218 / 226.43812945443 * 100)
    ca = sum(row['current_account'] for row in q['series'][-4:]); gdp = sum(row['gdp'] for row in official()['groups']['gdp']['rows'][-4:])
    assert r['latest']['current_account'] == pytest.approx(ca, abs=1e-6)
    assert r['macro']['quarterly'][-1]['current_account_gdp_pct'] == pytest.approx(ca / gdp * 100)


def test_reserve_debt_is_same_quarter_stock_ratio_and_excludes_unallocated_intercompany():
    q = build_switzerland_view(official(), 'quarterly')['macro']['quarterly']; r = build_switzerland_view(official())['macro']['quarterly']
    assert q[-1]['reserve_stock'] == pytest.approx(879.9322135275765)
    debt = .12981298916 + 86.97675723878 + 622.7777810273615 + 307.10033734773737
    assert q[-1]['short_term_external_debt'] == pytest.approx(debt)
    assert q[-1]['reserves_debt_pct'] == pytest.approx(879.9322135275765 / debt * 100)
    for field in ('reserve_stock', 'short_term_external_debt', 'reserves_debt_pct'): assert q[-1][field] == r[-1][field]
    for field in ('net_direct', 'net_equity', 'net_debt', 'net_other', 'reserve_transactions'):
        assert r[-1][field] == pytest.approx(sum(row[field] for row in q[-4:]))


def test_sa_monthly_business_cycle_trade_uses_complete_quarters_ma_and_yoy():
    q = build_switzerland_view(official(), 'quarterly'); r = build_switzerland_view(official())
    assert q['macro']['monthly'][-1]['exports'] == pytest.approx(23.05002834119547)
    assert q['macro']['trade'][-1]['date'] == '2026-04-01'  # July/August do not project September
    for field in ('exports', 'imports'):
        rows = r['macro']['trade']
        assert rows[-1][field] == pytest.approx(sum(row[field] for row in q['macro']['trade'][-4:]))
        assert rows[-1][field + '_ma3'] == pytest.approx(sum(row[field] for row in rows[-3:]) / 3)
        assert rows[-1][field + '_yoy'] == pytest.approx((rows[-1][field] / rows[-5][field] - 1) * 100)


def test_missing_gdp_debt_currency_or_investment_inputs_stay_gaps():
    snapshot = official(); snapshot['groups']['gdp']['rows'][-1]['gdp'] = None
    snapshot['groups']['debt']['rows'][-1]['debt_banks'] = None
    snapshot['groups']['currency']['rows'] = [row for row in snapshot['groups']['currency']['rows'] if row['date'] != '2026-05-01']
    snapshot['groups']['investment']['rows'][-1]['equity_assets'] = None
    row = build_switzerland_view(snapshot)['macro']['quarterly'][-1]
    for field in ('current_account_gdp_pct', 'reserves_debt_pct', 'currency_index', 'net_equity'): assert row[field] is None


def test_partial_refresh_keeps_failed_source_original_time_and_accepts_revisions(tmp_path):
    service = SwitzerlandBOPService(tmp_path / 'ch.json'); before = deepcopy(service.snapshot)
    def download(name):
        if name == 'currency': raise OSError('BIS unavailable')
        group = deepcopy(before['groups'][name]); group['checked_at'] = '2026-10-30T00:00:00+00:00'
        if name == 'gdp': group['rows'][-1]['gdp'] += 1
        return group
    service._download = download; view = service.view(refresh=True)
    assert view['delivery'] == 'saved_snapshot' and len(view['macro']['warnings']) == 1
    assert service.snapshot['groups']['currency'] == before['groups']['currency']
    assert service.snapshot['groups']['gdp']['rows'][-1]['gdp'] == pytest.approx(227.43812945443)
    assert SwitzerlandBOPService(service.cache_path, enabled=False).view()['latest'] == view['latest']


def test_regressed_coverage_outage_and_failure_backoff_keep_verified_observations(tmp_path):
    service = SwitzerlandBOPService(tmp_path / 'ch.json'); before = deepcopy(service.snapshot); calls = []
    def download(name):
        calls.append(name)
        if name != 'debt': raise OSError('Offline')
        group = deepcopy(before['groups'][name]); group['rows'][-1]['debt_banks'] = None; return group
    service._download = download; view = service.view(refresh=True); service.view(refresh=True)
    assert len(calls) == 7 and len(view['macro']['warnings']) == 7 and view['refresh_cooldown_seconds'] > 0
    assert service.snapshot['groups'] == before['groups']


def test_swiss_endpoint_is_chf_country_specific_and_no_store(tmp_path):
    client = TestClient(create_app(tmp_path / 'state.json'))
    for frequency in ('quarterly', 'trailing4q'):
        response = client.get('/api/fx/bop', params={'country': 'che', 'frequency': frequency, 'refresh': 'false'})
        assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
        data = response.json()
        assert data['country']['connected'] and data['country']['currency'] == data['reporting_currency'] == 'CHF'
        assert len(data['sources']) == 7 and data['macro']['country_code'] == 'CHE' and data['macro']['frequency'] == frequency
        assert not any(item['code'] == 'EUR' for item in data['countries'])
    with pytest.raises(BOPError): BOPService(tmp_path / 'usa.json', enabled=False).view('CHE')
