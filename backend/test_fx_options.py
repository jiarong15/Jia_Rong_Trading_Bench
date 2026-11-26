from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import csv
import io
import json
import math
from threading import Event, RLock
from types import SimpleNamespace
import zipfile

from fastapi.testclient import TestClient
import pytest

from .app import create_app
from .fx_options import FXOptionsService, FXOptionTape, identity, index_files
from .fx_option_models import (REQUIRED, build_cells, estimated_greeks, expiry_bucket,
    interpolate_vol, normalize_trade, parse_fx_archive, resolve_trades, saxo_surface)
from .test_fx_volatility import report

NOW=datetime(2026,10,31,12,tzinfo=timezone.utc)
DAILY='CFTC_CUMULATIVE_FOREX_2026_10_30.zip'
SLICE='CFTC_SLICE_FOREX_2026_10_31_1.zip'


def option(identifier='100',day='2026-10-30',**changes):
    row=dict.fromkeys(REQUIRED,'')
    row.update({'Dissemination Identifier':identifier,'Action type':'NEWT','Event type':'TRAD',
        'Event timestamp':day+'T10:01:00Z','Execution Timestamp':day+'T10:00:00Z',
        'Expiration Date':'2027-01-15','First exercise date':'2027-01-15',
        'UPI FISN':'NA/O Van Call EUR USD','UPI Underlier Name':'EUR USD',
        'Notional amount-Leg 1':'1,000,000','Notional currency-Leg 1':'EUR',
        'Notional amount-Leg 2':'1,100,000','Notional currency-Leg 2':'USD',
        'Call amount':'1000000','Call currency':'EUR','Put amount':'1100000','Put currency':'USD',
        'Strike Price':'1.1','Strike price currency/currency pair':'EUR/USD','Strike price notation':'1',
        'Package indicator':'FALSE','Non-standardized term indicator':'FALSE'})
    row.update(changes);return row


def zipped(rows,fields=None):
    text=io.StringIO();writer=csv.DictWriter(text,fieldnames=sorted(fields or REQUIRED));writer.writeheader();writer.writerows(rows)
    body=io.BytesIO()
    with zipfile.ZipFile(body,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('FX.csv',text.getvalue())
    return body.getvalue()


def meta(name,count,at='2026-10-31T11:00:00Z'):
    return {'fileName':name,'rowCount':count,'dissemDTM':at,'fullFilePath':'https://untrusted.test/not-followed'}


class Tape:
    def __init__(self):
        self.calls=[];self.fail=False
        self.rows={DAILY:[option()],SLICE:[option('200','2026-10-31')]}
        self.daily=[meta(DAILY,1,'2026-10-31T00:15:00Z')];self.slices=[meta(SLICE,1)]
    def __call__(self,url):
        self.calls.append(url)
        if self.fail:raise ValueError('PRIVATE_UPSTREAM_EXCEPTION')
        if url.endswith('bucketname'):return b'kgc0418-tdw-data-0'
        if url.endswith('Cumulative.json'):return json.dumps({'CFTC_FX':self.daily}).encode()
        if url.endswith('Slice.json'):return json.dumps({'CFTC_FX':self.slices}).encode()
        return zipped(self.rows[url.rsplit('/',1)[-1]])


class Carry:
    def __init__(self):
        self.lock=RLock();self.calls=0
        self.quotes={'EUR/USD':{'spot':1.1,'forward_12m':1.12,'spot_time':None,'spot_observation_date':'2026-10-30','quote_time':'2026-10-30T18:00:00Z'}}
        self.forward_source=SimpleNamespace(snapshot=lambda now:{'quotes':deepcopy(self.quotes),'errors':{}})
        saved=SimpleNamespace(lock=RLock(),raw=report(value='10.0'),error=None)
        self.volatility_source=SimpleNamespace(report=saved,snapshot=lambda now:{})
    def view(self,**kwargs):self.calls+=1;return {}


def test_normalizes_base_currency_calls_puts_and_reciprocal_strikes():
    assert normalize_trade(option(),NOW)['side']=='call'
    put=option(**{'UPI FISN':'NA/O Van Put EUR USD','Call currency':'USD','Put currency':'EUR','Call amount':'1100000','Put amount':'1000000'})
    assert normalize_trade(put,NOW)['side']=='put'
    reverse=option(**{'Strike Price':str(1/1.1),'Strike price currency/currency pair':'USD/EUR'})
    assert normalize_trade(reverse,NOW)['strike']==pytest.approx(1.1)
    sek=option(**{'UPI FISN':'NA/O Van Put SEK USD','UPI Underlier Name':'SEK USD',
        'Notional amount-Leg 1':'860000','Notional currency-Leg 1':'USD','Notional amount-Leg 2':'8490433','Notional currency-Leg 2':'SEK',
        'Call currency':'USD','Call amount':'860000','Put currency':'SEK','Put amount':'9000000',
        'Strike Price':'9.8726','Strike price currency/currency pair':'SEK/USD'})
    normalized=normalize_trade(sek,NOW)
    assert normalized['pair']=='USD/SEK' and normalized['side']=='call' and normalized['strike']==9.8726
    assert normalized['notional']==860000


@pytest.mark.parametrize('changes',[
    {'UPI FISN':'NA/O Targ O EUR USD'}, {'Package indicator':'TRUE'}, {'Non-standardized term indicator':'TRUE'},
    {'First exercise date':'2026-11-01'}, {'Strike price notation':'3'}, {'Call currency':'JPY'},
    {'Strike price currency/currency pair':'GBP/USD'}, {'Notional amount-Leg 2':'9,000,000'},
    {'Event type':'COMP'}, {'UPI Underlier Name':'GBP USD'},
    {'Expiration Date':'2026-10-01','First exercise date':'2026-10-01'},
])
def test_excludes_unsupported_payoffs_ambiguous_units_and_inconsistent_currency_legs(changes):
    assert normalize_trade(option(**changes),NOW) is None


def test_capped_reported_notional_is_retained_as_reported_not_multiplied_by_plus():
    row=option(**{'Notional amount-Leg 1':'1,000,000+','Notional amount-Leg 2':'1,100,000+'})
    assert normalize_trade(row,NOW)['notional']==1e6


def test_lifecycle_corrections_errors_deduplication_and_no_fresh_modification_prices():
    a=option();b=option('101');correction=option('102',**{'Action type':'CORR','Original Dissemination Identifier':'100',
        'Notional amount-Leg 1':'2,000,000','Notional amount-Leg 2':'2,200,000','Event timestamp':'2026-10-31T10:00:00Z'})
    error=option('103',**{'Action type':'EROR','Original Dissemination Identifier':'101','Event timestamp':'2026-10-31T11:00:00Z'})
    modification=option('104',**{'Action type':'MODI','Original Dissemination Identifier':'102'})
    termination=option('105',**{'Action type':'TERM','Original Dissemination Identifier':'102'})
    events=parse_fx_archive(zipped([a,b,correction,error,modification,termination]),NOW,6)
    trades=resolve_trades({'a':{'events':events},'duplicate_slice':{'events':events}})
    assert len(trades)==1 and trades[0]['notional']==2e6 and trades[0]['executed_at']=='2026-10-30T10:00:00+00:00'


def test_inconsistent_contract_dates_skip_only_bad_print_and_still_apply_corrections():
    bad=option('101',**{'Expiration Date':'2026-10-01','First exercise date':'2026-10-01'})
    events=parse_fx_archive(zipped([option(),bad]),NOW,2)
    assert len(resolve_trades({'a':{'events':events}}))==1
    correction={**bad,'Action type':'CORR','Original Dissemination Identifier':'100'}
    events=parse_fx_archive(zipped([option(),correction]),NOW,2)
    assert resolve_trades({'a':{'events':events}})==[]


def test_rejects_incomplete_changed_future_nonfinite_and_duplicate_archives():
    for rows,expected in [([option()],2),([option(),option()],2),([option(**{'Strike Price':'NaN'})],1),
        ([option(**{'Execution Timestamp':'2027-01-01T00:00:00Z'})],1)]:
        with pytest.raises(ValueError):parse_fx_archive(zipped(rows),NOW,expected)
    with pytest.raises(ValueError):parse_fx_archive(zipped([],REQUIRED-{'Call currency'}),NOW,0)
    with pytest.raises(ValueError):identity('../secret.zip')
    with pytest.raises(ValueError):index_files(json.dumps({'CFTC_FX':[meta(DAILY,1,'2027-01-01T00:00:00Z')]}),'CUMULATIVE',NOW)


def test_estimated_forward_greeks_match_finite_difference_prices_and_usd_units():
    spot=1.1;forward=1.12;strike=1.11;years=.4;sigma=.12;n=1e6
    f=spot*math.exp(math.log(forward/spot)*years)
    normal=lambda x:.5*(1+math.erf(x/math.sqrt(2)))
    def price(value,vol):
        d1=(math.log(value/strike)+.5*vol*vol*years)/(vol*math.sqrt(years));d2=d1-vol*math.sqrt(years)
        return value*normal(d1)-strike*normal(d2)
    h=1e-4;v=1e-5
    delta=(price(f+h,sigma)-price(f-h,sigma))/(2*h)
    gamma=(price(f+h,sigma)-2*price(f,sigma)+price(f-h,sigma))/(h*h)
    vega=(price(f,sigma+v)-price(f,sigma-v))/(2*v)*.01
    result=estimated_greeks(spot,forward,strike,years,sigma,'call',n,1)
    assert result['delta']==pytest.approx(delta*f*n,rel=1e-5)
    assert result['gamma']==pytest.approx(gamma*f*f*n*.01,rel=1e-5)
    assert result['vega']==pytest.approx(vega*n,rel=1e-5)
    put=estimated_greeks(spot,forward,strike,years,sigma,'put',n,1)
    assert put['gamma']==result['gamma'] and put['vega']==result['vega']
    assert put['delta']+result['delta']==pytest.approx(n*f)
    converted=estimated_greeks(spot,forward,strike,years,sigma,'call',n,1/150)
    assert converted['vega']==pytest.approx(result['vega']/150)


def test_atm_total_variance_interpolation_no_long_extrapolation_and_expiry_boundaries():
    assert interpolate_vol([(7/365,.1),(.5,.2),(1,.3)],.01)==.1
    assert interpolate_vol([(.5,.2),(1,.3)],.75)==pytest.approx(math.sqrt((.5*.2**2+1*.3**2)/2/.75))
    assert interpolate_vol([(.5,.2),(1,.3)],1.01) is None
    assert expiry_bucket(date(2026,11,30),date(2026,10,31))==0
    assert expiry_bucket(date(2026,12,1),date(2026,10,31))==1
    surface,published=saxo_surface(report(value='10.0'),NOW)
    assert len(surface['EUR/USD'])==5 and surface['EUR/USD'][-1]==(1,.1) and published=='2026-10-30T12:30:00+00:00'
    assert 'NZD/USD' not in surface


def test_bucketing_filter_windows_same_day_expiry_and_missing_greeks_never_zero_fill():
    a=normalize_trade(option(),NOW);long={**a,'expiry':'2028-01-01'}
    cells,selected,modelled,last=build_cells([a,long], 'EUR/USD',NOW,30,Carry().quotes['EUR/USD'],[(7/365,.1),(1,.1)])
    assert selected==2 and modelled==1 and sum(c['notional'] for c in cells)==2.2e6
    assert any(c['gamma'] is None for c in cells) and any(c['gamma'] is not None for c in cells)
    none,selected,_,_=build_cells([{**a,'expiry':'2026-10-31'}],'EUR/USD',NOW,30,Carry().quotes['EUR/USD'],[(1,.1)])
    assert none==[] and selected==0
    assert build_cells([a],'EUR/USD',NOW,1,None,[])[0]==[]


@pytest.mark.parametrize('reverse',[False,True])
def test_mixed_bucket_keeps_covered_greeks_and_counts_without_zero_filling_missing_trades(reverse):
    # A leap year puts 365-day and 366-day expiries in the same 9-12M bucket.
    now=NOW.replace(year=2027)
    trade={**normalize_trade(option(),NOW),'executed_at':(now-timedelta(days=1)).isoformat(),'expiry':'2028-10-30'}
    unsupported={**trade,'expiry':'2028-10-31'}
    quote=Carry().quotes['EUR/USD'];nodes=[(7/365,.1),(1,.1)]
    expected=build_cells([trade],'EUR/USD',now,30,quote,nodes)[0][0]
    trades=[trade,unsupported]
    if reverse:trades.reverse()
    cells,selected,modelled,_=build_cells(trades,'EUR/USD',now,30,quote,nodes)
    assert selected==2 and modelled==1 and len(cells)==1
    cell=cells[0]
    assert cell['count']==2 and cell['modelled_count']==1 and cell['notional']==2.2e6
    for metric in ('delta','gamma','vega'):
        assert cell[metric]==expected[metric]
    missing=build_cells([unsupported],'EUR/USD',now,30,quote,nodes)[0][0]
    assert missing['modelled_count']==0 and all(missing[metric] is None for metric in ('delta','gamma','vega'))


def test_refresh_throttle_new_trades_inputs_outage_recovery_and_saved_only(tmp_path):
    transport=Tape();carry=Carry();now=[NOW]
    service=FXOptionsService(tmp_path/'fx.json',carry,transport=transport,clock=lambda:now[0])
    assert not service.view(saved_only=True)['metrics']['gamma']['available'] and not transport.calls
    before=service.view();assert before['included_trades']==2 and before['modelled_trades']==2
    assert before['basis']=='reported_trades' and before['status']=='ready' and before['spot_at'] is None
    assert 'ESTIMATED GREEKS' in ' '.join(before['notices'])
    calls=len(transport.calls);now[0]+=timedelta(seconds=59);service.view();assert len(transport.calls)==calls
    name='CFTC_SLICE_FOREX_2026_10_31_2.zip'
    transport.rows[name]=[option('201','2026-10-31',**{'Event timestamp':'2026-10-31T11:01:00Z','Execution Timestamp':'2026-10-31T11:00:00Z'})]
    transport.slices.insert(0,meta(name,1,'2026-10-31T11:02:00Z'));now[0]+=timedelta(seconds=1)
    after=service.view();assert after['included_trades']==3 and after['last_trade_at']=='2026-10-31T11:00:00+00:00'
    assert after['cells'][0]['notional']>before['cells'][0]['notional']
    previous=after['cells'][0]['gamma'];carry.quotes['EUR/USD']['forward_12m']=1.15
    assert service.view(saved_only=True)['cells'][0]['gamma']!=previous
    carry.quotes['EUR/USD']['forward_12m']=1.12
    transport.fail=True;now[0]+=timedelta(seconds=60);failed=service.view()
    assert failed['status']=='stale' and failed['included_trades']==3 and failed['checked_at']==after['checked_at']
    assert 'PRIVATE_UPSTREAM' not in json.dumps(failed) and 'Dissemination Identifier' not in json.dumps(failed)
    recovered=FXOptionTape(tmp_path/'fx.json',transport=lambda url:pytest.fail('Saved load must not fetch'),clock=lambda:now[0])
    assert len(recovered.snapshot()['trades'])==3
    transport.fail=False;now[0]+=timedelta(seconds=60);assert service.view()['status']=='ready'


def test_background_updates_trade_data_without_browser_requests_and_stops(tmp_path):
    transport=Tape();now=[NOW];waiting=Event();release=Event();updated=Event();waits=[]
    service=FXOptionsService(tmp_path/'fx.json',Carry(),transport=transport,clock=lambda:now[0])
    def wait(seconds):
        assert 0<seconds<=60
        waits.append(1)
        if len(waits)==1:waiting.set();assert release.wait(3);return False
        updated.set();return True
    service.stop.wait=wait
    try:
        service.start();service.start();assert waiting.wait(3)
        name='CFTC_SLICE_FOREX_2026_10_31_2.zip';transport.rows[name]=[option('300','2026-10-31')];transport.slices.append(meta(name,1))
        now[0]+=timedelta(seconds=60);release.set();assert updated.wait(3)
        assert service.view(saved_only=True)['included_trades']==3
    finally:
        release.set();service.close()
    assert not service.thread.is_alive()


def test_missing_and_old_volatility_leave_greek_cells_unavailable_but_keep_reported_notional(tmp_path):
    carry=Carry();carry.volatility_source.report.raw=report(day='20 Oct 2026')
    service=FXOptionsService(tmp_path/'fx.json',carry,transport=Tape(),clock=lambda:NOW)
    data=service.view()
    assert data['metrics']['notional']['available'] and not data['metrics']['gamma']['available']
    assert all(c['gamma'] is None for c in data['cells']) and data['status']=='stale'


def test_api_location_no_store_disabled_mode_and_pair_validation(tmp_path):
    app=create_app(tmp_path/'app.sqlite3')
    with TestClient(app) as client:
        response=client.get('/api/fx/options/heatmap?pair=EUR%2FUSD&saved_only=true')
        assert response.status_code==200 and response.headers['cache-control']=='no-store'
        assert response.json()['status']=='disabled' and response.json()['cells']==[]
        cny=client.get('/api/fx/options/heatmap?pair=USD%2FCNY&saved_only=true')
        assert cny.status_code==200 and cny.json()['market_source']=='CFETS / ChinaMoney'
        assert not cny.json()['metrics']['gamma']['available']
        assert client.get('/api/fx/options/heatmap?pair=FAKE').status_code==409
