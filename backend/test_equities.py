from copy import deepcopy
from datetime import date, timedelta
import pytest
from fastapi.testclient import TestClient
from .alpaca import AlpacaService, momentum_metrics, symbols_from
from .app import create_app
from .equity_analysis import score_financials, validate_financials, valuation, implementation

def financials():
    return {'source':'Verified annual report test fixture','period_end':'2025-12-31','filed_at':'2026-03-01','prior_period_end':'2024-12-31','prior_filed_at':'2025-03-01',
        'current':{'net_income':8,'cfo':10,'assets':110,'current_assets':60,'current_liabilities':20,'long_term_debt':15,'revenue':140,'gross_profit':50,'equity_issued':False,'shares':10,'book_equity':80,'ebit':20,'invested_capital':100,'cash':10,'total_debt':20},
        'prior':{'net_income':4,'assets':100,'beginning_assets':80,'current_assets':40,'current_liabilities':20,'long_term_debt':20,'revenue':100,'gross_profit':30,'invested_capital':90}}

def bars(count=300):
    return [{'t':(date(2025,1,1)+timedelta(days=n)).isoformat()+'T04:00:00Z','o':100+n,'h':102+n,'l':99+n,'c':101+n,'v':10000} for n in range(count)]

def transport(host,path,params):
    if path=='/v2/assets':return [{'symbol':s,'name':name,'class':'us_equity','status':'active','exchange':'NASDAQ','tradable':True} for s,name in [('AAPL','Apple Inc.'),('MSFT','Microsoft Corporation'),('JPM','JPMorgan Chase'),('PLTR','Palantir Technologies Inc.')]]
    if path=='/v2/clock':return {'is_open':False,'timestamp':'2026-10-24T01:00:00-04:00','next_open':'2026-10-26T09:30:00-04:00','next_close':'2026-10-26T16:00:00-04:00'}
    symbols=params['symbols'].split(',')
    if path.endswith('/snapshots'):return {s:{'latestTrade':{'p':110,'t':'2026-10-23T20:00:00Z'},'latestQuote':{'bp':109.9,'ap':110.1,'t':'2026-10-23T20:00:00Z'},'dailyBar':{'v':10000},'prevDailyBar':{'c':100}} for s in symbols}
    if path.endswith('/bars'):return {'bars':{s:bars() for s in symbols},'next_page_token':None}
    raise AssertionError('Unexpected upstream route')

@pytest.fixture
def client(tmp_path):
    app=create_app(tmp_path/'equities.sqlite3');app.state.equities.transport=transport
    return TestClient(app)

def test_nine_score_and_missing_not_zero():
    complete=score_financials(financials(),110)
    assert complete['score']==complete['coverage']==9
    assert complete['book_to_market']==pytest.approx(80/1100)
    missing=score_financials(None)
    assert missing['score'] is None and missing['coverage']==0
    assert all(t['point'] is None for t in missing['tests'])
    partial=financials();del partial['current']['equity_issued']
    assert score_financials(partial)['score'] is None
    assert score_financials(partial)['coverage']==8

def test_ties_fail_improvement_tests_and_unknown_issuance_stays_unknown():
    d=financials();d['current']['cfo']=d['current']['net_income'];d['current']['equity_issued']=None
    tests={t['name']:t['point'] for t in score_financials(d)['tests']}
    assert tests['Cash earnings quality']==0
    assert tests['No equity issuance'] is None

def test_validation_rejects_future_bad_dates_and_numeric_boolean():
    d=financials();assert validate_financials(d,date(2026,10,24))==d
    for key,value in [('filed_at','2028-01-01'),('period_end','2024-01-01')]:
        bad=deepcopy(d);bad[key]=value
        with pytest.raises(ValueError):validate_financials(bad,date(2026,10,24))
    d['current']['assets']=True
    with pytest.raises(ValueError):validate_financials(d,date(2026,10,24))

def test_momentum_excludes_last_month():
    normal=[{'date':str(n),'close':100+n,'volume':1000} for n in range(300)]
    first=momentum_metrics(normal)
    changed=deepcopy(normal)
    for b in changed[-21:]:b['close']*=2
    second=momentum_metrics(changed)
    assert first['momentum_12_1']==second['momentum_12_1']
    assert first['return_1m']!=second['return_1m']
    assert momentum_metrics(normal[:20])['momentum_12_1'] is None

def test_alpaca_pagination_and_cache():
    calls=[]
    def paged(host,path,params):
        calls.append((path,dict(params)))
        if path.endswith('/bars'):
            return {'bars':{'AAPL':bars()} if 'page_token' not in params else {'MSFT':bars()},'next_page_token':'second' if 'page_token' not in params else None}
        return transport(host,path,params)
    service=AlpacaService(transport=paged)
    market=service.market(['AAPL','MSFT'])
    assert all(row['history_count']==300 for row in market['rows'])
    assert all(row['change_pct']==pytest.approx(10) for row in market['rows'])
    count=len(calls);assert service.market(['AAPL','MSFT'])==market and len(calls)==count
    assert len([path for path,_ in calls if path.endswith('/bars')])==2
    assert all(host.startswith('https:') for host in ['https://data.alpaca.markets'])

def test_empty_upstream_quotes_never_turn_into_demo_prices():
    def empty(host,path,params):
        if path.endswith('/snapshots'):return {}
        return transport(host,path,params)
    row=AlpacaService(transport=empty).market(['AAPL'])['rows'][0]
    assert row['price'] is None and row['stale']

@pytest.mark.parametrize('clock_time,is_open,expected',[('15:45:00',True,1),('16:05:00',False,1),('16:20:00',False,2)])
def test_history_requires_complete_close_after_request_lag(clock_time,is_open,expected):
    def recent(host,path,params):
        if path=='/v2/clock':return {'is_open':is_open,'timestamp':f'2026-10-23T{clock_time}-04:00'}
        if path.endswith('/bars'):
            return {'bars':{'AAPL':[{'t':f'2026-10-{day}T04:00:00Z','o':100,'h':102,'l':99,'c':101,'v':1000} for day in ['22','23']]}}
        raise AssertionError('Unexpected request')
    assert len(AlpacaService(transport=recent).bars(['AAPL'])['AAPL'])==expected

def test_market_and_saved_fundamentals_api(client):
    before=client.get('/api/workspace').json()
    result=client.get('/api/equities/market?symbols=AAPL,MSFT,JPM')
    assert result.status_code==200,result.text
    assert result.json()['feed']=='iex'
    assert all(row['financials']['score'] is None for row in result.json()['rows'])
    for s in ['AAPL','MSFT','JPM']:
        assert client.put('/api/equities/financials/'+s,json={'document':financials()}).status_code==200
    result=client.get('/api/equities/market?symbols=AAPL,MSFT,JPM').json()
    assert all(row['financials']['score']==9 for row in result['rows'])
    assert all(row['value_momentum_percentile'] is not None for row in result['rows'])
    after=client.get('/api/workspace').json()
    assert before['cash']==after['cash'] and before['positions']==after['positions']
    assert 'secret' not in str(result).lower() and 'APCA-API' not in str(result)

def test_valuation_duration_and_implied_cap():
    d=financials();v=valuation(d,20,.09,.21,.15,.03,10)
    assert v['available'] and v['historical_roic_pct']>0
    assert v['curve'][20]['fair_value']>v['curve'][5]['fair_value']
    fair=v['fair_value_per_share'];inverse=valuation(d,fair,.09,.21,.15,.03,10)
    assert inverse['implied_cap_years']==pytest.approx(10,abs=1e-7)
    assert valuation(d,20,.09,.21,.05,.03,10)['implied_cap_years'] is None
    assert not valuation(None,20)['available']

def test_implementation_buffer_cost_and_tax():
    row={'price':100,'spread_bps':10,'average_daily_dollar_volume':1000000}
    held=implementation(row,100000,50,.055,.01)
    assert held['action']=='hold' and held['quantity']==0
    sold=implementation(row,100000,100,.05,0,1,5,80,.2)
    assert sold['action']=='sell' and sold['quantity']==50
    assert sold['estimated_tax']==pytest.approx(200)
    assert sold['estimated_total']==pytest.approx(205.5)
    assert not sold['cost_is_partial']
    assert implementation(row,100000,100,.05,0)['cost_is_partial']

def test_calculator_endpoints_do_not_trade(client):
    client.put('/api/equities/financials/AAPL',json={'document':financials()})
    before=client.get('/api/workspace').json()
    assert client.post('/api/equities/AAPL/valuation',json={}).json()['available']
    assert client.post('/api/equities/AAPL/implementation',json={'target_weight':.1}).status_code==200
    after=client.get('/api/workspace').json()
    assert before==after
    assert client.post('/api/equities/AAPL/valuation',json={'wacc':0}).status_code==422
    assert client.get('/api/equities/market?symbols=INVALID/URL').status_code==409

def test_repeated_history_token_fails():
    def repeat(host,path,params):
        if path.endswith('/bars'):return {'bars':{'AAPL':bars()},'next_page_token':'repeat'}
        return transport(host,path,params)
    with pytest.raises(ValueError,match='repeated history page token'):AlpacaService(transport=repeat).market(['AAPL'])

def test_symbol_limits():
    assert symbols_from('aapl, MSFT,AAPL')==['AAPL','MSFT']
    with pytest.raises(ValueError):symbols_from('')
    with pytest.raises(ValueError):symbols_from('AAPL/../../account')

def test_directory_search_finds_unloaded_ticker_and_company(client):
    before=client.get('/api/workspace').json()
    ticker=client.get('/api/equities/search?q=pltr').json()
    company=client.get('/api/equities/search?q=palantir%20technologies').json()
    assert ticker['results']==company['results']
    assert ticker['results'][0]['symbol']=='PLTR'
    assert ticker['source']=='Alpaca asset directory'
    loaded=client.get('/api/equities/market?symbols=PLTR').json()['rows'][0]
    assert loaded['name']=='Palantir Technologies Inc.' and loaded['price']==110
    assert client.get('/api/workspace').json()==before

def test_directory_search_pagination_ranking_and_cache():
    calls=[]
    def directory(host,path,params):
        assert path=='/v2/assets' and host=='https://paper-api.alpaca.markets'
        assert params=={'status':'active','asset_class':'us_equity'}
        calls.append(path)
        return [{'symbol':s,'name':n,'class':'us_equity','status':status,'tradable':True,'exchange':'NYSE'} for s,n,status in [
            ('ZABC','ABC Holdings','active'),('ABC','Example Company','active'),('ABCD','Another Business','active'),
            ('OLD','ABC inactive','inactive')]]
    service=AlpacaService(transport=directory)
    first=service.search('ABC',limit=2);second=service.search('ABC',offset=2,limit=2)
    assert [r['symbol'] for r in first['results']]==['ABC','ABCD']
    assert [r['symbol'] for r in second['results']]==['ZABC']
    assert first['total']==first['universe_count']==3 and len(calls)==1
    assert service.search('  EXAMPLE   company ')['results'][0]['symbol']=='ABC'
    assert service.search('nonexistent')['results']==[]

def test_directory_errors_and_search_request_validation(client):
    def broken(*_):raise ValueError('Alpaca directory unavailable')
    client.app.state.equities.transport=broken
    assert client.get('/api/equities/search?q=AAPL').status_code==409
    assert client.get('/api/equities/search?offset=-1').status_code==422
    assert client.get('/api/equities/search?limit=101').status_code==422
    assert client.get('/api/equities/search?q='+'x'*101).status_code==422
