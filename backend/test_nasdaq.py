import io
import json
import urllib.error
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .nasdaq import NasdaqService, NasdaqError, filing_reference
from .sec import SECError
from .test_sec import transport as sec_transport, CIK
from .test_equities import financials

def table(rows):
    columns=list(rows[0]) if rows else ['symbol']
    return {'datatable':{'columns':[{'name':k} for k in columns],'data':[[r.get(k) for k in columns] for r in rows]},'meta':{'next_cursor_id':None}}

def nasdaq_transport(code,params):
    assert params['symbol']=='APP'
    if code=='NDAQ/RD':return table([{'symbol':'APP','figi':'BBG_APP','name':'AppLovin','secfilings':f'https://www.sec.gov/cgi-bin/browse-edgar?CIK={CIK}&action=getcompany'}])
    assert params['dimension']=='ARY' and 'reportperiod.gte' in params
    return table([{'symbol':'APP','figi':'BBG_APP','dimension':'ARY','reportperiod':'2025-12-31','calendardate':'2025-12-31','netinc':3333751000,'currency':'USD'}])

def denied(*_):raise NasdaqError('Nasdaq NDAQ/RD: subscription required (HTTP 403, QEPx04).','subscription_required','QEPx04')

@pytest.fixture
def client(tmp_path):
    app=create_app(tmp_path/'fallback.sqlite3');app.state.sec.transport=sec_transport;app.state.nasdaq.transport=nasdaq_transport
    return TestClient(app)

def test_sec_primary_does_not_consume_nasdaq_calls(client):
    def unexpected(*_):pytest.fail('Nasdaq must not be called for a complete SEC report')
    client.app.state.nasdaq.transport=unexpected
    response=client.post('/api/equities/reports/APP/sync')
    assert response.status_code==200
    assert response.json()['origin']=='sec' and response.json()['nasdaq_report'] is None
    after=client.get('/api/workspace').json()
    client.post('/api/equities/reports/APP/sync')
    assert client.get('/api/workspace').json()==after

def test_missing_ticker_recovers_full_sec_report_using_nasdaq_cik(client):
    def missing_directory(url,html=False):return {} if url.endswith('company_tickers.json') else sec_transport(url,html)
    client.app.state.sec.transport=missing_directory
    result=client.post('/api/equities/reports/APP/sync').json()
    assert result['origin']=='sec' and 'Nasdaq' in result['sec_report']['discovery_source']
    assert result['sec_report']['download_available']
    assert result['nasdaq_report']['status']=='annual_data'
    assert client.get('/api/equities/sec/APP/document').status_code==200

def test_subscription_error_visible_and_no_portfolio_or_manual_change(client):
    manual=financials();client.put('/api/equities/financials/APP',json={'document':manual})
    before=client.get('/api/workspace').json()
    client.app.state.sec.transport=lambda *_: {}
    client.app.state.nasdaq.transport=denied
    result=client.post('/api/equities/reports/APP/sync').json()
    assert result['nasdaq_report']['status']=='subscription_required'
    assert result['nasdaq_report']['code']=='QEPx04'
    assert result['origin']=='manual' and result['document']==manual
    after=client.get('/api/workspace').json()
    assert before['cash']==after['cash'] and before['portfolio']==after['portfolio']
    assert client.get('/api/equities/financials/APP').json()['nasdaq_report']==result['nasdaq_report']

def test_source_failure_retains_saved_sec_values_and_exposes_attempt_error(client):
    initial=client.post('/api/equities/reports/APP/sync').json()
    def fail(*_):raise SECError('SEC HTTP 403. Access denied.')
    client.app.state.sec.transport=fail;client.app.state.nasdaq.transport=denied
    result=client.post('/api/equities/reports/APP/sync?refresh=true').json()
    assert result['document']==initial['document'] and result['retained_sec_report']
    assert '403' in result['sec_error'] and result['nasdaq_report']['status']=='subscription_required'
    assert client.get('/api/equities/financials/APP').json()['sec_error']==result['sec_error']

def test_cik_recovery_requires_sec_ticker_identity(client):
    def wrong_company(url,html=False):
        if url.endswith('company_tickers.json'):return {}
        result=sec_transport(url,html)
        if '/submissions/' in url:result['tickers']=['WRONG']
        return result
    client.app.state.sec.transport=wrong_company
    result=client.post('/api/equities/reports/APP/sync').json()
    assert result['document'] is None and result['origin'] is None
    assert any('did not match' in w for w in result['nasdaq_report']['warnings'])

def test_missing_xbrl_does_not_hide_existing_full_10k(client):
    def no_facts(url,html=False):
        if '/companyfacts/' in url:raise SECError('SEC HTTP 404. No company facts.')
        return sec_transport(url,html)
    client.app.state.sec.transport=no_facts
    result=client.post('/api/equities/reports/APP/sync').json()
    assert result['sec_report']['download_available'] and result['document'] is None
    assert any('404' in w for w in result['sec_report']['warnings'])
    assert result['nasdaq_report']['annual_tables']
    assert client.get('/api/equities/sec/APP/document').status_code==200

def test_raw_data_is_not_claimed_as_filing_or_automatic_scoring_input():
    service=NasdaqService(transport=nasdaq_transport);report=service.report('APP')
    assert report['status']=='annual_data' and report['document'] is None
    assert not report['full_document_available']
    assert report['annual_tables'][0]['rows'][0]['netinc']==3333751000
    assert all('api_key' not in t['url'] for t in report['annual_tables'])
    assert 'filing dates' in report['warnings'][-1]

@pytest.mark.parametrize('reference',[
    'https://evil.example/?CIK=123','http://www.sec.gov/cgi-bin/browse-edgar?CIK=123',
    'https://www.sec.gov@evil.example/cgi-bin/browse-edgar?CIK=123',
    'https://www.sec.gov/Archives/../../../x?CIK=123','https://www.sec.gov/edgar/browse/?CIK=not-a-cik',
    'https://www.sec.gov/edgar/browse/?CIK=0',None,
])
def test_untrusted_filing_references_are_not_followed(reference):assert filing_reference(reference)==(None,None)

def test_permission_failure_is_cached_across_symbols_and_explicit_refresh_retries():
    calls=[]
    def counted(code,params):calls.append(code);return denied()
    service=NasdaqService(transport=counted)
    service.report('APP');service.report('APP');service.report('AAPL')
    assert calls==['NDAQ/RD']
    service.report('APP',refresh=True);assert len(calls)==2

def test_paginated_rows_complete_and_wrong_company_rejected():
    calls=[]
    def paginated(code,params):
        calls.append(params)
        result=table([{'symbol':'APP'}])
        if 'qopts.cursor_id' not in params:result['meta']['next_cursor_id']='next-page'
        return result
    service=NasdaqService(transport=paginated)
    assert len(service.rows('NDAQ/RD',{'symbol':'APP'}))==2
    assert calls[1]['qopts.cursor_id']=='next-page'
    def wrong_figi(code,params):
        result=nasdaq_transport(code,params)
        if code!='NDAQ/RD':result['datatable']['data'][0][1]='OTHER_FIGI'
        return result
    report=NasdaqService(transport=wrong_figi).report('APP')
    assert not report['annual_tables'] and any('FIGI' in w for w in report['warnings'])

def test_http_errors_never_leak_api_key_or_untrusted_body(monkeypatch):
    monkeypatch.setattr('backend.nasdaq.nasdaq_key',lambda:'test-private-secret')
    def blocked(req,timeout):
        assert req.full_url.startswith('https://data.nasdaq.com/api/v3/datatables/')
        body=json.dumps({'quandl_error':{'code':'QEPx04','message':req.full_url}}).encode()
        raise urllib.error.HTTPError(req.full_url,403,'Denied',{},io.BytesIO(body))
    monkeypatch.setattr('urllib.request.urlopen',blocked)
    report=NasdaqService().report('APP')
    assert report['status']=='subscription_required' and report['code']=='QEPx04'
    assert 'test-private-secret' not in json.dumps(report) and 'api_key=' not in json.dumps(report)

def test_nonannual_or_future_or_conflicting_identity_rows_not_imported():
    for field,value in [('dimension','ARQ'),('reportperiod','2099-12-31'),('symbol','OTHER')]:
        def bad(code,params):
            result=nasdaq_transport(code,params)
            if code!='NDAQ/RD':
                cols=[c['name'] for c in result['datatable']['columns']]
                result['datatable']['data'][0][cols.index(field)]=value
            return result
        report=NasdaqService(transport=bad).report('APP')
        assert not report['annual_tables'] and report['warnings']
