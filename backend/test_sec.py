from copy import deepcopy
import json
import urllib.error
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .sec import SECService, SECError, map_financials, filing_url
from .test_equities import transport as alpaca_transport, financials

CURRENT={'form':'10-K','accessionNumber':'0001751008-26-000010','filingDate':'2026-02-19','reportDate':'2025-12-31','primaryDocument':'app-20251231.htm'}
PRIOR={'form':'10-K','accessionNumber':'0001751008-25-000018','filingDate':'2025-02-27','reportDate':'2024-12-31','primaryDocument':'app-20241231.htm'}
CIK='0001751008'

def fixture_facts():
    gaap={}
    def add(tag,end,value,start=None,accn=CURRENT['accessionNumber'],filed=CURRENT['filingDate'],unit='USD'):
        item={'end':end,'val':value*1e6,'accn':accn,'filed':filed,'fy':2025,'fp':'FY','form':'10-K'}
        if start:item['start']=start
        gaap.setdefault(tag,{'units':{}})['units'].setdefault(unit,[]).append(item)
    for year,scale in [(2025,1),(2024,.8)]:
        end=f'{year}-12-31';start=f'{year}-01-01'
        for tag,value in [('Assets',200),('AssetsCurrent',80),('LiabilitiesCurrent',40),('LongTermDebt',30),('LongTermDebtNoncurrent',28),('DebtCurrent',5),('StockholdersEquity',110),('CashAndCashEquivalentsAtCarryingValue',20)]:add(tag,end,value*scale)
        add('CommonStockSharesOutstanding',end,10,unit='shares')
        for tag,value in [('NetIncomeLoss',25),('NetCashProvidedByUsedInOperatingActivities',30),('Revenues',150),('CostOfRevenue',50),('OperatingIncomeLoss',40)]:add(tag,end,value*scale,start)
    add('Assets','2023-12-31',140,accn=PRIOR['accessionNumber'],filed=PRIOR['filingDate'])
    return {'cik':1751008,'entityName':'AppLovin','facts':{'us-gaap':gaap}}

def submissions(forms=None):
    rows=forms or [CURRENT,PRIOR]
    return {'name':'AppLovin','tickers':['APP'],'filings':{'recent':{k:[f[k] for f in rows] for k in CURRENT},'files':[]}}

def transport(url,html=False):
    if url.endswith('company_tickers.json'):return {'0':{'ticker':'APP','cik_str':1751008,'title':'AppLovin'}}
    if '/submissions/' in url:return submissions()
    if '/companyfacts/' in url:return fixture_facts()
    if '/Archives/' in url and html:return b'<!doctype html><html><body>Form 10-K annual report fixture.</body></html>'
    raise AssertionError('Unexpected SEC URL')

@pytest.fixture
def client(tmp_path):
    app=create_app(tmp_path/'sec.sqlite3');app.state.sec.transport=transport;app.state.equities.transport=alpaca_transport
    return TestClient(app)

def test_annual_mapping_uses_report_period_not_fy_label_and_derives_transparently():
    document,sources,warnings=map_financials(fixture_facts(),[CURRENT,PRIOR],CURRENT,CIK,'APP')
    assert document['period_end']=='2025-12-31' and document['prior_period_end']=='2024-12-31'
    assert document['current']['revenue']==150 and document['prior']['revenue']==120
    assert document['current']['gross_profit']==100
    assert document['current']['total_debt']==33
    assert document['current']['invested_capital']==123
    assert document['prior']['beginning_assets']==140
    assert sources['prior.beginning_assets']['accession']==PRIOR['accessionNumber']
    assert sources['current.total_debt']['formula'] and warnings
    assert document['current']['equity_issued'] is None

def test_quarterly_future_and_ambiguous_facts_are_not_annual_inputs():
    facts=fixture_facts();gaap=facts['facts']['us-gaap']
    revenue=gaap['Revenues']['units']['USD'];quarter=deepcopy(revenue[0]);quarter['start']='2025-10-01';quarter['val']=999e6;revenue.append(quarter)
    future=deepcopy(revenue[0]);future['filed']='2027-01-01';future['val']=1000e6;revenue.append(future)
    document,_,_=map_financials(facts,[CURRENT,PRIOR],CURRENT,CIK,'APP')
    assert document['current']['revenue']==150
    conflicting=deepcopy(revenue[0]);conflicting['val']=151e6;revenue.append(conflicting)
    document,_,warnings=map_financials(facts,[CURRENT,PRIOR],CURRENT,CIK,'APP')
    assert document['current']['revenue'] is None and any('Ambiguous' in w for w in warnings)

def test_missing_debt_is_not_zero_and_zero_cash_proceeds_do_not_prove_no_issuance():
    facts=fixture_facts();gaap=facts['facts']['us-gaap'];del gaap['DebtCurrent']
    gaap['ProceedsFromIssuanceOfCommonStock']={'units':{'USD':[dict(start='2025-01-01',end='2025-12-31',val=0,accn=CURRENT['accessionNumber'],filed=CURRENT['filingDate'],form='10-K')]}}
    document,_,_=map_financials(facts,[CURRENT,PRIOR],CURRENT,CIK,'APP')
    assert document['current']['total_debt'] is None and document['current']['invested_capital'] is None
    assert document['current']['equity_issued'] is None

def test_sec_service_download_cache_and_exact_company_match(tmp_path):
    calls=[]
    def counted(url,html=False):calls.append(url);return transport(url,html)
    service=SECService(tmp_path,transport=counted);report=service.report('APP')
    assert report['filing']['accession']==CURRENT['accessionNumber'] and report['download_available']
    assert service.document_path('APP').read_bytes().startswith(b'<!doctype html>')
    count=len(calls);assert service.report('APP')==report and len(calls)==count
    assert service.report('AP')['status']=='unavailable'

def test_sec_sync_populates_analysis_preserves_portfolio_and_is_idempotent(client):
    before=client.get('/api/workspace').json()
    response=client.post('/api/equities/sec/APP/sync');assert response.status_code==200,response.text
    assert response.json()['origin']=='sec'
    row=client.get('/api/equities/market?symbols=APP').json()['rows'][0]
    assert row['financial_origin']=='sec' and row['financials']['coverage']==8
    assert client.post('/api/equities/APP/valuation',json={}).json()['available']
    saved=client.get('/api/equities/financials/APP').json();assert saved['document']['current']['revenue']==150
    after=client.get('/api/workspace').json()
    assert before['portfolio']==after['portfolio'] and before['cash']==after['cash']
    assert client.post('/api/equities/sec/APP/sync').status_code==200
    assert client.get('/api/workspace').json()==after
    download=client.get('/api/equities/sec/APP/document')
    assert download.status_code==200 and 'attachment' in download.headers['content-disposition']
    assert 'sandbox' in download.headers['content-security-policy']

def test_manual_override_survives_sec_sync_and_failed_refresh(client):
    manual=financials();client.put('/api/equities/financials/APP',json={'document':manual})
    response=client.post('/api/equities/sec/APP/sync').json()
    assert response['origin']=='manual' and response['document']==manual
    def fail(*_):raise SECError('SEC HTTP 403. Access denied.')
    client.app.state.sec.transport=fail
    assert client.post('/api/equities/sec/APP/sync?refresh=true').status_code==409
    assert client.get('/api/equities/financials/APP').json()['document']==manual

def test_foreign_annual_report_is_not_claimed_as_10k(tmp_path):
    def foreign(url,html=False):
        if '/submissions/' in url:
            row={**CURRENT,'form':'20-F'};return submissions([row])
        return transport(url,html)
    report=SECService(tmp_path,transport=foreign).report('APP')
    assert report['status']=='unavailable' and '20-F' in report['message'] and report['document'] is None

def test_full_filing_failure_is_reported_separately_from_usable_facts(tmp_path):
    def blocked(url,html=False):
        if html:raise SECError('SEC HTTP 403. Access denied.')
        return transport(url,html)
    service=SECService(tmp_path,transport=blocked);report=service.report('APP')
    assert report['document'] and not report['download_available'] and '403' in report['download_error']
    with pytest.raises(SECError,match='403'):service.document_path('APP')

def test_mismatched_identity_and_untrusted_document_paths_fail(tmp_path):
    def wrong(url,html=False):
        result=transport(url,html)
        if '/companyfacts/' in url:result['cik']=320193
        return result
    with pytest.raises(SECError,match='identity'):SECService(tmp_path,transport=wrong).report('APP')
    with pytest.raises(SECError):filing_url(CIK,{**CURRENT,'primaryDocument':'../../untrusted.htm'})

def test_sec_http_errors_do_not_expose_upstream_body_or_broker_credentials(tmp_path,monkeypatch):
    def denied(request,timeout):
        assert request.get_header('User-agent').startswith('Meridian')
        assert not any('apca' in name.lower() for name in request.headers)
        raise urllib.error.HTTPError(request.full_url,403,'blocked',{},None)
    monkeypatch.setattr('urllib.request.urlopen',denied)
    with pytest.raises(SECError,match='SEC HTTP 403'):SECService(tmp_path).report('APP')
