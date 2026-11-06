from datetime import date, datetime, timedelta, timezone
import json
import math
from statistics import stdev
from urllib.parse import parse_qs, urlparse
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .fx_carry_risk import IDS, PAIRS, FXCarryRiskService, market_series, points, weekly_correlation
from .fx_exact_feed import validate_feed, configured_feed

TODAY = date(2026, 10, 30)
NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_public_network(monkeypatch):
    monkeypatch.setenv('FX_INVESTING_FORWARDS_ENABLED', 'false')
    monkeypatch.setenv('FX_SAXO_REPORT_ENABLED', 'false')


def prices():
    return {(TODAY-timedelta(days=850-i)).isoformat(): round(100*math.exp(i*.0003+.015*math.sin(i/9)), 7)
            for i in range(850) if (TODAY-timedelta(days=850-i)).weekday() < 5}


def feed(now=NOW, pairs=None):
    return {'provider':'Test licensed feed','as_of':now.isoformat(),'volatility_unit':'percent','forward_format':'outright',
            'quotes':[{'pair':item[2],'spot':100.0,'forward_12m':98.0,'spot_date':now.date().isoformat(),
                       'forward_date':(now.date()+timedelta(days=365)).isoformat(),'quote_time':now.isoformat(),
                       'iv_time':now.isoformat(),'iv_tenor':'12M','iv_delta':'ATM','implied_volatility_pct':7.5}
                      for item in PAIRS if pairs is None or item[2] in pairs]}


class Source:
    def __init__(self): self.fail=False;self.calls=[];self.values=prices()
    def __call__(self,url):
        self.calls.append(url)
        if self.fail:raise ValueError('transport failed with PRIVATE_KEY_123 '+url)
        query=parse_qs(urlparse(url).query)
        if 'series_id' in query:
            return json.dumps({'observations':[{'date':stamp,'value':str(value)} for stamp,value in self.values.items()],'count':len(self.values),'offset':0,'units':'lin'})
        identifier=query['id'][0]
        return 'observation_date,'+identifier+'\n'+''.join(f'{stamp},{value}\n' for stamp,value in self.values.items())


def test_exact_forward_carry_directions_and_implied_volatility():
    quotes=validate_feed(feed(),NOW)
    market={identifier:prices() for identifier in IDS}
    rows={row['currency']:row for row in points(market,quotes,TODAY,52,NOW)}
    assert rows['JPY']['pair_carry_pct']==pytest.approx((100/98-1)*100)
    assert rows['JPY']['usd_carry_pct']==rows['JPY']['pair_carry_pct']
    assert rows['EUR']['usd_carry_pct']==pytest.approx(-2)
    assert rows['EUR']['pair_carry_pct']!=-rows['EUR']['usd_carry_pct']
    assert rows['EUR']['risk_correlation']==pytest.approx(-1)
    assert rows['JPY']['risk_correlation']==pytest.approx(1)
    assert rows['EUR']['implied_volatility_pct']==7.5
    assert rows['EUR']['weekly_observations']==52
    assert rows['EUR']['correlation_end']=='2026-10-23'
    assert not any('realized_volatility_pct' in row for row in rows.values())


@pytest.mark.parametrize('change',[
    lambda body:body.update(volatility_unit='decimal'),
    lambda body:body.update(forward_format='points'),
    lambda body:body['quotes'][0].update(iv_tenor='1M'),
    lambda body:body['quotes'][0].update(iv_delta='25D'),
    lambda body:body['quotes'][0].update(spot=float('nan')),
    lambda body:body['quotes'][0].update(forward_12m=0),
    lambda body:body['quotes'][0].update(forward_date='2026-12-30'),
    lambda body:body['quotes'][0].update(quote_time='2027-01-01T00:00:00Z'),
    lambda body:body['quotes'].append(body['quotes'][0]),
])
def test_invalid_forward_or_options_quotes_are_rejected(change):
    body=feed();change(body)
    with pytest.raises(ValueError):validate_feed(body,NOW)


def test_completed_calendar_weeks_and_missing_weeks_never_bridge():
    source=prices()
    initial=weekly_correlation(source,source,False,TODAY,26)
    assert initial['observations']==26
    omitted={stamp:value for stamp,value in source.items() if not '2026-09-07'<=stamp<='2026-09-11'}
    assert weekly_correlation(omitted,source,False,TODAY,26)['observations']==24
    updated={**source,TODAY.isoformat():999999}
    assert weekly_correlation(updated,updated,False,TODAY,26)==initial


def test_endpoints_use_last_same_day_and_do_not_fill_missing_prices():
    source=prices();source['2026-10-23']=None
    risk=weekly_correlation(source,prices(),False,TODAY,26)
    assert risk['end']=='2026-10-22' and risk['observations']==26
    assert weekly_correlation({},prices(),False,TODAY,52)['value'] is None
    rows=points({}, {},TODAY,52,NOW)
    assert all(row['implied_volatility_pct'] is None and row['usd_carry_pct'] is None for row in rows)


@pytest.mark.parametrize('mutation',[
    lambda document: document.update(count=9999),
    lambda document: document.update(units='pch'),
    lambda document: document['observations'].append(document['observations'][0]),
    lambda document: document['observations'][0].update(value='NaN'),
    lambda document: document['observations'][0].update(value='0'),
    lambda document: document['observations'][0].update(date='2027-01-01'),
])
def test_rejects_invalid_fred_source(mutation):
    body=json.loads(Source()('https://api.stlouisfed.org/?series_id=SP500'))
    mutation(body)
    with pytest.raises(ValueError):market_series(json.dumps(body),'SP500',TODAY-timedelta(days=900),TODAY,True)


def test_refresh_saved_load_outages_and_no_secret_leak(tmp_path):
    source=Source();now=[NOW];path=tmp_path/'risk.json';quote_calls=[];fail=[False]
    def load_quotes():
        quote_calls.append(now[0])
        if fail[0]:raise ValueError('PRIVATE_KEY_123')
        return feed(now[0])
    service=FXCarryRiskService(path,transport=source,clock=lambda:now[0],api_key='PRIVATE_KEY_123',quote_loader=load_quotes)
    assert service.view(saved_only=True)['status']=='partial'
    assert not source.calls and not quote_calls
    first=service.view()
    assert first['status']=='ready' and len(first['rows'])==9
    assert len(source.calls)==10 and len(quote_calls)==1
    assert 'PRIVATE_KEY' not in json.dumps(first)+path.read_text()
    now[0]+=timedelta(seconds=59)
    assert service.view()['source_checks']==first['source_checks']
    assert len(quote_calls)==1
    now[0]+=timedelta(seconds=1)
    second=service.view()
    assert len(quote_calls)==2 and len(source.calls)==10
    assert second['rows'][0]['quote_time']==now[0].isoformat()
    assert second['source_checks']['quotes']!=first['source_checks']['quotes']
    fail[0]=True;now[0]+=timedelta(seconds=300)
    stale=service.view()
    assert stale['status']=='stale' and len(source.calls)==20
    assert stale['source_checks']['quotes']==second['source_checks']['quotes']
    assert stale['rows'][0]['quote_time']==second['rows'][0]['quote_time']
    assert 'PRIVATE_KEY' not in json.dumps(stale)
    recovered=FXCarryRiskService(path,transport=lambda url:pytest.fail('saved-only must not fetch'),clock=lambda:now[0],api_key='')
    assert recovered.view(saved_only=True)['rows']==stale['rows']
    fail[0]=False;now[0]+=timedelta(seconds=60)
    assert service.view()['status']=='ready'


def test_regressed_prices_or_quote_times_preserve_previous_inputs(tmp_path):
    source=Source();quote=[feed()]
    service=FXCarryRiskService(tmp_path/'risk.json',transport=source,clock=lambda:NOW,api_key='',quote_loader=lambda:quote[0])
    first=service.view()
    source.values.pop(max(source.values));quote[0]=feed(NOW-timedelta(minutes=1))
    result=service.view(refresh=True)
    assert result['status']=='stale' and len(result['errors'])==2
    assert result['rows']==first['rows']
    assert result['source_checks']==first['source_checks']


def test_no_feed_does_not_substitute_policy_rates_or_realized_vol(tmp_path,monkeypatch):
    monkeypatch.delenv('FX_CARRY_FEED_URL',raising=False)
    service=FXCarryRiskService(tmp_path/'risk.json',transport=Source(),clock=lambda:NOW,api_key='')
    result=service.view()
    assert result['status']=='awaiting_feed' and not result['feed_configured']
    assert all(row['usd_carry_pct'] is None and row['implied_volatility_pct'] is None for row in result['rows'])
    assert all(row['risk_correlation'] is not None for row in result['rows'])
    assert result['basis']=='observed_forwards_and_implied_volatility'
    assert 'policy-rate carry estimates' in result['methodology']


def test_old_quotes_are_flagged_and_partial_feed_coverage_is_explicit(tmp_path):
    service=FXCarryRiskService(tmp_path/'risk.json',transport=Source(),clock=lambda:NOW,api_key='',quote_loader=lambda:feed(NOW-timedelta(seconds=301)))
    result=service.view()
    assert result['status']=='stale' and all(row['stale'] for row in result['rows'])
    service.quote_loader=lambda:feed(NOW,pairs={'EUR/USD'})
    service.quotes={}
    result=service.view(refresh=True)
    assert result['status']=='partial'
    assert sum(row['usd_carry_pct'] is not None for row in result['rows'])==1


def test_api_query_window_and_no_store(tmp_path):
    app=create_app(tmp_path/'app.sqlite3')
    service=app.state.fx_carry_risk
    service.enabled=True;service.transport=Source();service.clock=lambda:NOW;service.api_key='test';service.quote_loader=feed
    with TestClient(app) as client:
        response=client.get('/api/fx/carry-risk?weeks=26')
        assert response.status_code==200,response.text
        body=response.json()
        assert body['weeks']==26 and body['basis']=='observed_forwards_and_implied_volatility'
        assert body['poll_interval_seconds']==60
        assert response.headers['cache-control']=='no-store'
        assert client.get('/api/fx/carry-risk?weeks=13').status_code==422
        assert client.get('/api/fx/carry-risk?weeks=104&saved_only=true').json()['weeks']==104
