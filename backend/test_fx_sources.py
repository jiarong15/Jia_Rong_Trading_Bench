from datetime import datetime, timedelta, timezone
import pytest
from .fx_sources import FXSources
from .yahoo_fx import YahooFXService, YahooFXError, yahoo_symbol
from .twelvedata import TwelveDataService
from .test_twelvedata import transport as twelve_transport

NOW=datetime(2026,10,26,12,0,tzinfo=timezone.utc)

def yahoo_transport(symbol,interval,period):
    quote={'JPY=X':'JPY','SGD=X':'SGD','CHF=X':'CHF'}.get(symbol,symbol[:-2][-3:])
    bars=[]
    for n in range(100,-1,-1):
        at=NOW.replace(hour=0) - timedelta(days=n) if interval=='1d' else NOW-timedelta(minutes=n*(60 if interval=='1h' else 5))
        bars.append({'time':at.isoformat(),'open':1.1,'high':1.2,'low':1,'close':1.12+n*.0001})
    return {'meta':{'symbol':symbol,'currency':quote,'instrumentType':'CURRENCY','exchangeTimezoneName':'UTC','regularMarketPrice':1.12345,
        'regularMarketTime':NOW,'previousClose':1.12,'chartPreviousClose':.9,'marketState':'REGULAR','regularMarketDayHigh':1.13,'regularMarketDayLow':1.11},'bars':bars}

def sources(tmp_path,primary='yahoo',yahoo_override=None,twelve_override=None,clock=None):
    clock=clock or (lambda:NOW)
    yahoo=YahooFXService(tmp_path,transport=yahoo_override or yahoo_transport,clock=clock)
    twelve=TwelveDataService(transport=twelve_override or twelve_transport,clock=clock)
    return FXSources(yahoo,twelve,primary,clock)

def test_symbol_aliases_and_timestamped_yahoo_quote(tmp_path):
    assert yahoo_symbol('usdsgd')=='SGD=X' and yahoo_symbol('EUR/USD')=='EURUSD=X'
    assert yahoo_symbol('USD/JPY')=='JPY=X' and yahoo_symbol('EUR/JPY')=='EURJPY=X'
    quote=sources(tmp_path).yahoo.quote('EUR/USD')
    assert quote['quote_time']==NOW.isoformat() and not quote['stale']
    assert quote['change_pct']==pytest.approx((1.12345/1.12-1)*100)
    assert quote['provider']=='yahoo'

def test_epoch_timestamps_work_but_range_previous_close_is_not_daily_change(tmp_path):
    def epoch(symbol,interval,period):
        data=yahoo_transport(symbol,interval,period);data['meta']['regularMarketTime']=int(NOW.timestamp());del data['meta']['previousClose'];return data
    quote=sources(tmp_path,yahoo_override=epoch).yahoo.quote('EUR/USD')
    assert quote['quote_time']==NOW.isoformat() and quote['change_pct'] is None

def test_healthy_yahoo_never_spends_twelve_data_credits(tmp_path):
    calls=[]
    def counted(path,params):calls.append(path);return twelve_transport(path,params)
    fx=sources(tmp_path,twelve_override=counted)
    market=fx.market(['EUR/USD','USD/SGD'])
    assert all(r['provider']=='yahoo' and not r['fallback'] for r in market['rows'])
    assert fx.history('EUR/USD')['provider']=='yahoo' and calls==[]
    assert market['quota']['initialized'] is False

def test_yahoo_network_cooldown_prevents_six_repeated_failures(tmp_path):
    calls=[]
    def offline(*args):calls.append(args);raise YahooFXError('Yahoo network unavailable.','provider')
    fx=sources(tmp_path,yahoo_override=offline);market=fx.market(['EUR/USD','USD/SGD','GBP/USD'])
    assert len(calls)==1
    assert all(row['provider']=='twelvedata' and row['fallback'] for row in market['rows'])
    assert market['providers'][0]['state']=='cooldown'

def test_unsupported_pair_does_not_disable_yahoo_for_other_pairs(tmp_path):
    def some(symbol,interval,period):
        if symbol=='EURSGD=X':raise YahooFXError('No data for this pair.')
        return yahoo_transport(symbol,interval,period)
    fx=sources(tmp_path,yahoo_override=some);market=fx.market(['EUR/SGD','EUR/USD'])
    assert market['rows'][0]['provider']=='twelvedata' and market['rows'][1]['provider']=='yahoo'
    assert market['source']=='Mixed FX sources'

def test_twelve_data_primary_can_fall_back_to_yahoo_when_quota_is_exhausted(tmp_path):
    calls=[]
    def exhausted(path,params):calls.append(path);return {'status':'error','code':429,'message':'API credits exhausted'}
    fx=sources(tmp_path,'twelvedata',twelve_override=exhausted);market=fx.market(['EUR/USD','USD/SGD'])
    assert all(row['provider']=='yahoo' and row['fallback'] for row in market['rows'])
    assert calls==['/api_usage']

def test_history_falls_back_as_a_whole_without_splicing_candles(tmp_path):
    calls=[]
    def invalid_daily(symbol,interval,period):
        calls.append(interval)
        data=yahoo_transport(symbol,interval,period)
        if interval=='1d':data['bars'][0]['close']=float('nan')
        return data
    fx=sources(tmp_path,yahoo_override=invalid_daily)
    history=fx.history('EUR/USD')
    assert history['provider']=='twelvedata' and history['fallback']
    assert 'invalid' in history['fallback_reason']
    assert fx.history('EUR/USD')['provider']=='twelvedata' and calls.count('1d')==1
    assert fx.history('EUR/USD','1h')['provider']=='yahoo'

def test_bad_quote_identity_time_or_price_is_rejected(tmp_path):
    for key,value in [('currency','JPY'),('instrumentType','EQUITY'),('symbol','WRONG=X'),('regularMarketTime',NOW+timedelta(hours=1)),('regularMarketPrice',float('nan'))]:
        def invalid(symbol,interval,period,key=key,value=value):
            data=yahoo_transport(symbol,interval,period);data['meta'][key]=value;return data
        fx=sources(tmp_path,yahoo_override=invalid)
        assert fx.market(['EUR/USD'])['rows'][0]['provider']=='twelvedata'

def test_cooldown_expires_and_primary_is_retried(tmp_path):
    clock=[NOW];calls=[]
    def temporary(*args):
        calls.append(args)
        if len(calls)==1:raise YahooFXError('Temporary Yahoo failure.','provider')
        data=yahoo_transport(*args);data['meta']['regularMarketTime']=clock[0];return data
    fx=sources(tmp_path,yahoo_override=temporary,clock=lambda:clock[0])
    assert fx.quote('EUR/USD')['provider']=='twelvedata'
    clock[0]+=timedelta(minutes=6)
    assert fx.quote('EUR/USD')['provider']=='yahoo' and len(calls)==2

def test_both_sources_unavailable_never_become_demo_quotes(tmp_path):
    def yahoo_down(*args):raise YahooFXError('Yahoo unavailable.','provider')
    def twelve_down(*args):return {'status':'error','code':403,'message':'Restricted'}
    fx=sources(tmp_path,yahoo_override=yahoo_down,twelve_override=twelve_down)
    row=fx.quote('EUR/USD');assert row['price'] is None and row['source'] is None
    with pytest.raises(ValueError,match='both sources'):fx.history('EUR/USD')

def test_daily_and_intraday_completion_follow_actual_session_timezone(tmp_path):
    fx=sources(tmp_path)
    daily=fx.yahoo.history('EUR/USD','1day');intraday=fx.yahoo.history('EUR/USD','5min')
    assert daily['bars'][-1]['complete'] is False and daily['bars'][-2]['complete'] is True
    assert daily['analytics']['completed_observations']==100
    assert intraday['bars'][-1]['complete'] is False
    assert daily['session_timezone']=='UTC' and intraday['bars'][-1]['time'].endswith('+00:00')


@pytest.mark.parametrize('field,value',[('low',99),('high',0),('open',None),('low',float('nan'))])
def test_source_closes_survive_unusable_ohlc_without_repairs_or_backup_credits(tmp_path,field,value):
    calls=[]
    def bad_range(symbol,interval,period):
        data=yahoo_transport(symbol,interval,period);data['bars'][0][field]=value;return data
    def counted(*args):calls.append(args);return twelve_transport(*args)
    fx=sources(tmp_path,yahoo_override=bad_range,twelve_override=counted)
    history=fx.history('EUR/USD');original=yahoo_transport('EURUSD=X','1d','2y')
    assert history['provider']=='yahoo' and not history['fallback'] and calls==[]
    assert [bar['close'] for bar in history['bars']]==[bar['close'] for bar in original['bars']]
    bar=history['bars'][0]
    assert not bar['ohlc_available'] and all(bar[field] is None for field in ('open','high','low'))
    assert history['data_quality']['unavailable_ohlc_count']==1
    assert history['data_quality']['unavailable_ohlc_dates']==[bar['date']]
    assert history['analytics']==sources(tmp_path).yahoo.history('EUR/USD')['analytics']


@pytest.mark.parametrize('value',[None,float('nan'),float('inf'),0,-1,True])
def test_invalid_closes_still_reject_yahoo_history(tmp_path,value):
    def invalid(symbol,interval,period):
        data=yahoo_transport(symbol,interval,period);data['bars'][0]['close']=value;return data
    history=sources(tmp_path,yahoo_override=invalid).history('EUR/USD')
    assert history['provider']=='twelvedata' and history['fallback']
    assert 'invalid FX closing prices' in history['fallback_reason']


def test_good_closes_do_not_relax_symbol_or_timestamp_validation(tmp_path):
    def future(symbol,interval,period):
        data=yahoo_transport(symbol,interval,period);data['bars'][0]['time']=(NOW+timedelta(days=1)).isoformat();return data
    history=sources(tmp_path,yahoo_override=future).history('EUR/USD')
    assert history['provider']=='twelvedata' and 'timestamp' in history['fallback_reason']
