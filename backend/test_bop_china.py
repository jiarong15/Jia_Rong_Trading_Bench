from copy import deepcopy
from decimal import Decimal
from functools import lru_cache
from io import BytesIO
import json
from zipfile import ZipFile,ZIP_DEFLATED
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .bop import BOPError,BOPService
from .bop_china import (ChinaBOPService,SNAPSHOT,NBS_IDS,SAFE_PAGES,build_china_view,china_trade_view,
                        columns,customs_quarters,parse_bop_workbook,parse_debt_workbook,
                        parse_nbs,parse_positions_workbook,parse_trade_workbook,workbook_link)
from .public_workbooks import read_xlsx_sheet


@lru_cache
def workbook(name):
    with ZipFile(SNAPSHOT.parent/'china_safe_parser_fixtures.zip') as archive:return archive.read(name+'.xlsx')


def official():return json.loads(SNAPSHOT.read_text(encoding='utf-8'))


def nbs_fixture(group):return json.loads((SNAPSHOT.parent/('china_nbs_'+group+'_fixture.json')).read_text(encoding='utf-8'))


def replace_xml(body,old,new):
    output=BytesIO()
    with ZipFile(BytesIO(body)) as source,ZipFile(output,'w',ZIP_DEFLATED) as destination:
        for name in source.namelist():
            content=source.read(name)
            destination.writestr(name,content.replace(old.encode(),new.encode()) if name.endswith('.xml') else content)
    return output.getvalue()


def test_safe_native_units_full_precision_and_signed_outflows():
    rows=parse_bop_workbook(workbook('bop'))
    assert len(rows)==114
    last=rows[-1]
    assert last['date']=='2026-04-01'
    assert last['current_account']==pytest.approx(193.72430266602)
    assert last['current_account_cny']==pytest.approx(1324.27349215768)
    assert last['financial_account']==pytest.approx(157.24668723447)
    assert last['net_financial_inflow']==-last['financial_account']
    assert last['reserve_transactions']==pytest.approx(74.59062)
    assert last['direct_assets']==pytest.approx(33.62205157187)
    assert last['income_receipts']-last['income_payments']==pytest.approx(last['primary_income'])


@pytest.mark.parametrize('old,new',[
    ('100 million of US dollars','100 million of Hong Kong dollars'),
    ('1.A.a Goods','Missing goods row'),
    ('2026Q2','2026Q1'),
])
def test_changed_safe_schema_units_or_duplicate_quarters_are_rejected(old,new):
    with pytest.raises(BOPError):parse_bop_workbook(replace_xml(workbook('bop'),old,new))


def test_bounded_xlsx_reader_rejects_non_workbooks_and_missing_sheets():
    with pytest.raises(BOPError):read_xlsx_sheet(b'<html>Download unavailable</html>','Quarterly')
    with pytest.raises(BOPError):read_xlsx_sheet(workbook('bop'),'Nonexistent')
    with pytest.raises(BOPError):read_xlsx_sheet(b'x'*8_000_001,'Quarterly')


def test_live_workbook_link_discovery_uses_safe_current_attachment_not_a_pinned_filename():
    page=SAFE_PAGES['bop']
    first='/en/file/file/20260929/abc123.xlsx'
    revised='/en/file/file/20261230/def456.xlsx'
    assert workbook_link(f'<a href="{first}">Download</a>',page).endswith(first)
    assert workbook_link(f'<a href="{revised}">Download</a>',page).endswith(revised)
    with pytest.raises(BOPError):workbook_link('<a href="https://example.com/data.xlsx">Bad host</a>',page)
    with pytest.raises(BOPError):workbook_link(f'<a href="{first}"></a><a href="{revised}"></a>',page)


def test_safe_october_excel_format_and_january_february_combined_observations():
    raw=read_xlsx_sheet(workbook('trade'),'按美元计值')
    formatted=read_xlsx_sheet(workbook('trade'),'按美元计值',4)
    assert raw[3][1]==raw[3][10]=='2015.1'
    assert formatted[3][10]=='2015.10'
    rows=parse_trade_workbook(workbook('trade'))
    assert len(rows)==138 and rows[-1]['date']=='2026-08-01'
    assert len({row['date'] for row in rows})==len(rows)
    assert [row['date'] for row in rows if row['span_months']==2]==['2020-02-01','2021-02-01']
    assert all(row['date'] not in ('2020-01-01','2021-01-01') for row in rows)
    quarters={row['date']:row for row in china_trade_view(rows,'quarterly')}
    for year in (2020,2021):
        observed=[row for row in rows if f'{year}-01-01'<=row['date']<=f'{year}-03-01']
        assert len(observed)==2
        assert quarters[f'{year}-01-01']['exports']==pytest.approx(sum(row['exports'] for row in observed))
    rows.append({'date':'2020-01-01','span_months':1,'exports':1,'imports':1})
    with pytest.raises(BOPError):china_trade_view(rows,'quarterly')


def test_partial_trade_quarters_are_never_projected_and_growth_uses_selected_totals():
    monthly=parse_trade_workbook(workbook('trade'))
    quarter=china_trade_view(monthly,'quarterly')
    rolling=china_trade_view(monthly,'trailing4q')
    assert quarter[-1]['period']=='2026 Q2'
    assert quarter[-1]['exports']==pytest.approx(1214.5513913643)
    assert rolling[-1]['exports']==pytest.approx(sum(row['exports'] for row in quarter[-4:]))
    assert rolling[-1]['exports_ma3']==pytest.approx(sum(row['exports'] for row in rolling[-3:])/3)
    assert rolling[-1]['exports_yoy']==pytest.approx(100*(rolling[-1]['exports']/rolling[-5]['exports']-1))
    missing=[row for row in monthly if row['date']!='2026-05-01']
    assert china_trade_view(missing,'quarterly')[-1]['period']=='2026 Q1'


@pytest.mark.parametrize('group',['gdp','customs'])
def test_nbs_new_api_preserves_mainland_native_units_and_drops_unpublished_periods(group):
    rows=parse_nbs(json.dumps(nbs_fixture(group)),group)
    assert rows and rows[-1]['date']==('2026-07-01' if group=='gdp' else '2026-09-01')
    if group=='gdp':assert rows[-1]['gdp_cny']==37536.72
    else:
        assert rows[-1]['exports_ytd']==3370.695879
        assert rows[-1]['imports_ytd']==2432.583551


@pytest.mark.parametrize('corruption',['unit','region','indicator','date','duplicate','definition','negative','failed'])
def test_nbs_changed_or_invalid_indicators_are_rejected(corruption):
    body=nbs_fixture('gdp');first=body['data'][0];value=first['values'][0]
    if corruption=='unit':value['du_name']='美元'
    elif corruption=='region':value['da']='810000000000'
    elif corruption=='indicator':value['_id']='other-series'
    elif corruption=='date':first['code']='202609MM'
    elif corruption=='duplicate':body['data'].append(deepcopy(first))
    elif corruption=='definition':value['i_showname']='GDP累计值 (亿元)'
    elif corruption=='negative':value['value']='-1'
    elif corruption=='failed':body['success']=False
    with pytest.raises(BOPError):parse_nbs(json.dumps(body),'gdp')


def test_customs_quarter_flows_are_differences_of_matching_year_to_date_totals():
    monthly=[{'date':'2025-03-01','exports_ytd':100,'imports_ytd':80},
             {'date':'2025-06-01','exports_ytd':230,'imports_ytd':170},
             {'date':'2026-03-01','exports_ytd':120,'imports_ytd':90}]
    result=customs_quarters(monthly)
    assert result['2025-01-01']['customs_goods']==20
    assert result['2025-04-01']=={'customs_exports':130,'customs_imports':90,'customs_goods':40}
    assert result['2026-01-01']['customs_goods']==30  # Calendar-year reset, not last December subtraction.
    result=customs_quarters([monthly[1]])
    assert result['2025-04-01']['customs_goods'] is None


def test_reserve_positions_and_sector_debt_are_stocks_in_the_same_units():
    reserves=parse_positions_workbook(workbook('positions'))
    debt=parse_debt_workbook(workbook('debt'))
    assert reserves[-1]['reserve_stock']==3786.318
    assert any(row['date']=='2013-10-01' for row in reserves)  # Source mixes 12-31-2013 and day/month/year dates.
    assert debt[-1]['short_term_external_debt']==pytest.approx(1410.828042116)
    view=build_china_view(official(),'quarterly')
    assert view['macro']['quarterly'][-1]['reserves_debt_pct']==pytest.approx(268.3755841938875)
    assert any('intercompany lending' in text.lower() for text in view['methodology'])


def test_rolling_flows_matched_rmb_gdp_monthly_currency_average_and_unchanged_stocks():
    snapshot=official();quarter=build_china_view(snapshot,'quarterly');rolling=build_china_view(snapshot)
    assert rolling['latest']['current_account']==pytest.approx(sum(row['current_account'] for row in quarter['series'][-4:]),abs=.000001)
    ca=sum(Decimal(str(row['current_account_cny'])) for row in snapshot['groups']['bop']['rows'][-4:])
    gdp=sum(Decimal(str(row['gdp_cny'])) for row in snapshot['groups']['gdp']['rows'] if '2025-07-01'<=row['date']<='2026-04-01')
    assert rolling['macro']['quarterly'][-1]['current_account_gdp_pct']==pytest.approx(float(100*ca/gdp))
    assert quarter['macro']['quarterly'][-1]['current_account_gdp_pct']==pytest.approx(100*1324.27349215768/36151.11)
    currency=[row['NBCNBIS'] for row in snapshot['groups']['currency']['rows'] if '2026-04-01'<=row['date']<='2026-06-01']
    assert quarter['macro']['quarterly'][-1]['currency_index']==pytest.approx(sum(currency)/3)
    for field in ('net_direct','net_equity','net_debt','net_other','reserve_transactions'):
        assert rolling['macro']['quarterly'][-1][field]==pytest.approx(sum(row[field] for row in quarter['macro']['quarterly'][-4:]))
    for field in ('reserve_stock','short_term_external_debt','reserves_debt_pct'):
        assert rolling['macro']['quarterly'][-1][field]==quarter['macro']['quarterly'][-1][field]


def test_source_outages_preserve_original_data_times_and_backoff(tmp_path):
    calls=[]
    def offline(url,payload=None):calls.append(url);raise OSError('Offline')
    service=ChinaBOPService(tmp_path/'china.json',transport=offline)
    before=service.view();failed=service.view(refresh=True)
    assert before['latest']==failed['latest'] and before['sources']==failed['sources']
    assert failed['delivery']=='saved_snapshot' and len(failed['macro']['warnings'])==7
    service.view(refresh=True)
    assert len(calls)==7
    assert ChinaBOPService(service.cache_path,enabled=False).view()['latest']==before['latest']


def test_partial_refresh_accepts_revisions_keeps_failed_family_and_saves_atomically(tmp_path):
    service=ChinaBOPService(tmp_path/'china.json');before=deepcopy(service.snapshot)
    def download(name):
        if name=='currency':raise OSError('Temporary failure')
        group=deepcopy(before['groups'][name]);group['checked_at']='2026-10-30T00:00:00+00:00'
        if name=='positions':group['rows'][-1]['reserve_stock']+=1
        return group
    service._download=download
    updated=service.view(refresh=True)
    assert len(updated['macro']['warnings'])==1
    assert service.snapshot['groups']['currency']==before['groups']['currency']
    assert updated['macro']['quarterly'][-1]['reserve_stock']==3787.318
    assert service.cache_path.exists() and not service.cache_path.with_suffix('.tmp').exists()


def test_regressed_coverage_is_retained_as_old_data_with_warning(tmp_path):
    service=ChinaBOPService(tmp_path/'china.json');before=deepcopy(service.snapshot)
    def download(name):
        group=deepcopy(before['groups'][name])
        if name=='gdp':group['rows'].pop()
        return group
    service._download=download
    result=service.view(refresh=True)
    assert service.snapshot['groups']['gdp']==before['groups']['gdp']
    assert 'GDP refresh failed' in result['macro']['warnings'][0]


def test_country_route_keeps_all_three_countries_isolated_and_enables_china(tmp_path):
    client=TestClient(create_app(tmp_path/'state.json'))
    for frequency in ('quarterly','trailing4q'):
        response=client.get('/api/fx/bop',params={'country':'CHN','frequency':frequency,'refresh':'false'})
        assert response.status_code==200 and response.headers['Cache-Control']=='no-store'
        data=response.json()
        assert data['country']['connected'] and data['country']['currency']=='CNY'
        assert data['reporting_currency']=='USD' and data['units']=='USD billions'
        assert data['macro']['country_code']=='CHN' and data['macro']['frequency']==frequency
        assert len(data['sources'])==7 and data['validation']['status']=='passed'
        assert all(source['latest_observation'] for source in data['sources'])
    assert client.get('/api/fx/bop?country=USA&refresh=false').json()['country']['currency']=='USD'
    assert client.get('/api/fx/bop?country=JPN&refresh=false').json()['country']['currency']=='JPY'
    assert client.get('/api/fx/bop?country=EUR&refresh=false').status_code == 404
    with pytest.raises(BOPError):BOPService(tmp_path/'usa.json',enabled=False).view('CHN')
