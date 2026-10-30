from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import urllib.error
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .twelvedata import TwelveDataService, TwelveDataError, pair_from, pairs_from, fx_metrics

NOW=datetime(2026,10,26,12,0,tzinfo=timezone.utc)

def transport(path,params):
    if path=='/api_usage':return {'plan_limit':8,'current_usage':1,'daily_usage':1,'plan_daily_limit':800,'plan_category':'basic'}
    if path=='/forex_pairs':return {'data':[{'symbol':'EUR/USD','currency_base':'Euro','currency_quote':'US Dollar','currency_group':'Major'},
        {'symbol':'USD/SGD','currency_base':'US Dollar','currency_quote':'Singapore Dollar','currency_group':'Exotic'},
        {'symbol':'EUR/SGD','currency_base':'Euro','currency_quote':'Singapore Dollar','currency_group':'Exotic-Cross'}]}
    if path=='/quote':return {'symbol':params['symbol'],'name':params['symbol'],'close':'1.12345','previous_close':'1.12','open':'1.12','high':'1.13','low':'1.11','timestamp':int((NOW-timedelta(hours=12)).timestamp()),'last_quote_at':int(NOW.timestamp()),'is_market_open':True}
    if path=='/time_series':
        if params['interval']=='1day':
            values=[{'datetime':(NOW.date()-timedelta(days=n)).isoformat(),'open':str(1+n*.001),'high':str(1.01+n*.001),'low':str(.99+n*.001),'close':str(1+n*.001)} for n in range(100,-1,-1)]
        else:
            values=[{'datetime':(NOW-timedelta(minutes=n)).strftime('%Y-%m-%d %H:%M:%S'),'open':'1.1','high':'1.2','low':'1.0','close':'1.11'} for n in [120,60,0]]
        return {'meta':{'symbol':params['symbol'],'interval':params['interval']},'values':values,'status':'ok'}
    raise AssertionError('Unexpected FX endpoint')

@pytest.fixture
def client(tmp_path):
    app=create_app(tmp_path/'fx.sqlite3');app.state.fx.transport=transport;app.state.fx.clock=lambda:NOW
    return TestClient(app)

def service(override=None,clock=None):return TwelveDataService(transport=override or transport,clock=clock or (lambda:NOW))

def test_pair_validation_and_no_unbounded_comparison():
    assert pair_from('eurusd')==pair_from('EUR-USD')=='EUR/USD'
    assert pairs_from('eurusd,USD/SGD,EUR/USD')==['EUR/USD','USD/SGD']
    for bad in ['EUR/EUR','EUR/USD?apikey=x','../secrets','BTC/USDT']:
        with pytest.raises(ValueError):pair_from(bad)
    with pytest.raises(ValueError):pairs_from('')
    with pytest.raises(ValueError):pairs_from(','.join(f'AA{chr(65+n)}/USD' for n in range(13)))

def test_directory_company_currency_search_paging_and_cache():
    calls=[]
    def counted(path,params):calls.append(path);return transport(path,params)
    fx=service(counted)
    assert fx.search('Singapore')['total']==2
    assert fx.search('EURSGD')['results'][0]['symbol']=='EUR/SGD'
    assert fx.search('Euro',limit=1,offset=1)['results'][0]['symbol']=='EUR/USD'
    assert fx.search('nonexistent')['total']==0
    assert calls==['/forex_pairs'] and fx.quota()['minute_used']==0

def test_quote_prefers_last_quote_time_and_cache_does_not_spend_credits():
    calls=[]
    def counted(path,params):calls.append(path);return transport(path,params)
    fx=service(counted);first=fx.quote('EUR/USD');second=fx.quote('EUR/USD')
    assert first==second and first['quote_time']==NOW.isoformat() and not first['stale']
    assert first['price']==1.12345 and first['change_pct']==pytest.approx((1.12345/1.12-1)*100)
    assert calls==['/api_usage','/quote'] and fx.quota()['minute_used']==2

def test_budget_partial_market_and_minute_rollover():
    clock=[NOW];fx=service(clock=lambda:clock[0])
    pairs=[f'AA{chr(65+n)}/USD' for n in range(9)]
    result=fx.market(pairs)
    assert sum(row['price'] is not None for row in result['rows'])==7
    assert result['rows'][-1]['price'] is None and result['rows'][-1]['retry_after_seconds']>0
    assert fx.quota()['minute_used']==8
    clock[0]+=timedelta(minutes=1,seconds=1)
    assert fx.quote(pairs[-1])['price']==1.12345
    assert fx.quota()['minute_used']==1

def test_daily_budget_blocks_until_utc_day_changes():
    clock=[NOW]
    def limited(path,params):
        if path=='/api_usage':return {'plan_limit':8,'current_usage':1,'daily_usage':800,'plan_daily_limit':800,'plan_category':'basic'}
        return transport(path,params)
    fx=service(limited,lambda:clock[0]);result=fx.market(['EUR/USD'])
    assert result['rows'][0]['price'] is None and result['quota']['daily_remaining_observed']==0
    clock[0]=NOW+timedelta(days=1)
    assert fx.quote('EUR/USD')['price']==1.12345

def test_completed_daily_history_and_units_have_explicit_conservative_cutoff():
    history=service().history('EUR/USD')
    assert len(history['bars'])==101
    assert not history['bars'][-1]['complete'] and not history['bars'][-2]['complete']
    assert history['bars'][-3]['complete']
    assert history['analytics_as_of']=='2026-10-24'
    assert history['analytics']['completed_observations']==99
    assert history['analytics']['volatility_20d_pct'] is not None
    changed=deepcopy(history['bars']);changed[-1]['close']=100
    assert fx_metrics(changed)==history['analytics']

def test_intraday_utc_bar_completion_and_history_cache():
    fx=service();history=fx.history('USD/SGD','1h');used=fx.quota()['minute_used']
    assert history['bars'][0]['complete'] and not history['bars'][-1]['complete']
    assert history['bars'][0]['time'].endswith('+00:00') and history['analytics'] is None
    assert fx.history('USD/SGD','1h')==history and fx.quota()['minute_used']==used

@pytest.mark.parametrize('bad_value',['NaN','-1','0','Infinity'])
def test_invalid_exchange_rates_never_turn_into_demo_values(bad_value):
    def invalid(path,params):
        result=transport(path,params)
        if path=='/quote':result['close']=bad_value
        return result
    row=service(invalid).market(['EUR/USD'])['rows'][0]
    assert row['price'] is None and 'valid exchange rate' in row['error']

def test_identity_invalid_ohlc_and_future_quote_fail():
    def invalid(path,params):
        result=transport(path,params)
        if path=='/quote':result['last_quote_at']=int((NOW+timedelta(hours=1)).timestamp())
        if path=='/time_series':result['values'][0]['low']='99'
        return result
    fx=service(invalid)
    assert 'future' in fx.market(['EUR/USD'])['rows'][0]['error']
    with pytest.raises(TwelveDataError,match='OHLC'):fx.history('EUR/USD')
    def wrong(path,params):
        result=transport(path,params)
        if path=='/quote':result['symbol']='USD/JPY'
        return result
    assert 'identity' in service(wrong).market(['EUR/USD'])['rows'][0]['error']

def test_provider_error_message_is_sanitized_and_429_pauses_requests():
    calls=[]
    def limited(path,params):
        calls.append(path)
        if path=='/quote':return {'status':'error','code':429,'message':'Secret token dummy-sensitive-value in vendor diagnostic'}
        return transport(path,params)
    fx=service(limited);result=fx.market(['EUR/USD','USD/SGD'])
    assert all(row['price'] is None for row in result['rows'])
    assert 'dummy-sensitive' not in json.dumps(result) and calls==['/api_usage','/quote']
    assert result['quota']['retry_after_seconds']>0

def test_account_headers_include_shared_key_consumption():
    def shared(path,params):return transport(path,params),{'Api-Credits-Used':'7'}
    fx=service(shared);result=fx.market(['EUR/USD','USD/SGD'])
    assert result['rows'][0]['price']==1.12345 and result['rows'][1]['price'] is None

def test_fx_endpoints_leave_book_and_other_provider_data_unchanged(client):
    before=client.get('/api/workspace').json()
    market=client.get('/api/fx/market?pairs=EURUSD,USD/SGD');assert market.status_code==200
    assert all(r['price'] for r in market.json()['rows'])
    history=client.get('/api/fx/history?pair=EURUSD');assert history.status_code==200
    assert history.json()['analytics']['completed_observations']==99
    assert client.get('/api/fx/search?q=Singapore').json()['total']==2
    assert client.get('/api/workspace').json()==before
    assert client.get('/api/fx/history?pair=../secrets').status_code==409
    assert client.get('/api/fx/history?interval=invalid').status_code==422
    assert client.get('/api/fx/search?limit=101').status_code==422

def test_real_request_uses_header_not_url_and_http_errors_do_not_echo_credentials(monkeypatch):
    monkeypatch.setattr('backend.twelvedata.twelvedata_key',lambda:'test-only-key')
    def denied(request,timeout):
        assert request.get_header('Authorization')=='apikey test-only-key'
        assert 'test-only-key' not in request.full_url and 'apikey' not in request.full_url
        raise urllib.error.HTTPError(request.full_url,401,'test-only-key should not leak',{},None)
    monkeypatch.setattr('urllib.request.urlopen',denied)
    fx=TwelveDataService(clock=lambda:NOW)
    with pytest.raises(TwelveDataError,match='rejected') as exc:fx.initialize()
    assert 'test-only-key' not in str(exc.value)
