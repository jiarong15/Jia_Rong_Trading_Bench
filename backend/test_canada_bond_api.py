from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from urllib.parse import parse_qs, urlsplit
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .canada_bond_api import API, SERIES, history_url, parse_history
from .sovereign_yields import BY_CODE, CountryDownload
from .treasury_yields import TreasuryYieldService, analytics

TODAY=date(2026,10,30)
NOW=datetime(2026,10,30,15,tzinfo=timezone.utc)


def fixture():
    levels=[[2.50,3.35,3.55,3.80,4.10],[2.53,3.41,3.62,3.87,4.15],[2.55,3.36,3.58,3.84,4.13]]
    return {'seriesDetail':{identifier:{'label':label,'dateFormat':'daily','dimension':{'key':'d','name':'Date'},
                                      'metadata':{'unit':'Percent','frequency':'Business daily'}} for _,identifier,label in SERIES},
            'observations':[{'d':stamp,**{identifier:{'v':str(level)} for (_,identifier,_),level in zip(SERIES,values)}}
                            for stamp,values in zip(['2026-10-27','2026-10-28','2026-10-29'],levels)]}


def test_five_daily_canadian_api_yields_produce_all_eight_same_date_plots():
    view=analytics(parse_history(json.dumps(fixture()),'2000-01-01',TODAY.isoformat()),BY_CODE['CAN']['tenors'])
    assert view['curve_date']=='2026-10-29' and len(view['curve'])==5
    assert view['quotes'][0]['series_id']=='TB.CDN.90D.MID' and view['quotes'][0]['value']==2.55
    assert {item['key']:item['value'] for item in view['metrics']}=={
        '2s5s':22,'2s10s':48,'3m10s':129,'5s30s':55,'5s10s':26,'10s30s':29,'2s5s10s':-4,'2s10s30s':19}
    assert all(quote['date']=='2026-10-29' for quote in view['quotes'])


@pytest.mark.parametrize('problem',['missing_series','wrong_maturity','weekly','wrong_unit','wrong_dimension','non_finite',
                                   'boolean','duplicate_date','future_date','missing_value_field'])
def test_wrong_series_frequency_units_dates_and_values_are_rejected(problem):
    data=fixture();bill=SERIES[0][1]
    if problem=='missing_series':data['seriesDetail'].pop(bill)
    elif problem=='wrong_maturity':data['seriesDetail'][bill]['label']='Treasury bills, 6-month'
    elif problem=='weekly':data['seriesDetail'][bill]['metadata']['frequency']='Weekly'
    elif problem=='wrong_unit':data['seriesDetail'][bill]['metadata']['unit']='Basis points'
    elif problem=='wrong_dimension':data['seriesDetail'][bill]['dimension']['key']='m'
    elif problem=='non_finite':data['observations'][-1][bill]['v']='NaN'
    elif problem=='boolean':data['observations'][-1][bill]['v']=True
    elif problem=='duplicate_date':data['observations'].append(deepcopy(data['observations'][0]))
    elif problem=='future_date':data['observations'][-1]['d']='2026-10-31'
    elif problem=='missing_value_field':data['observations'][-1][bill]={}
    with pytest.raises((ValueError,KeyError)):parse_history(json.dumps(data),'2000-01-01',TODAY.isoformat())


def test_missing_daily_bill_yield_never_uses_a_previous_day_or_weekly_rate():
    data=fixture();data['observations'][-1].pop(SERIES[0][1])
    view=analytics(parse_history(json.dumps(data),'2000-01-01',TODAY.isoformat()),BY_CODE['CAN']['tenors'])
    assert view['rows'][-1]['m3'] is None and view['rows'][-1]['3m10s'] is None
    assert view['curve_date']=='2026-10-28' and view['latest_observation_date']=='2026-10-29'
    assert view['rows'][-1]['2s10s']==48 and view['rows'][-1]['2s5s10s']==-4


def test_native_api_request_and_daily_history_cache_use_canadian_series_only(tmp_path):
    calls=[]
    def transport(url,data=None):
        calls.append(url);parsed=urlsplit(url);query=parse_qs(parsed.query)
        assert data is None and parsed.hostname=='www.bankofcanada.ca' and url.startswith(API)
        assert set(parsed.path.split('/')[-2].split(','))=={identifier for _,identifier,_ in SERIES}
        assert query['end_date']==['2026-10-30'] and 'api_key' not in query
        return json.dumps(fixture()).encode()
    downloader=CountryDownload(BY_CODE['CAN'],tmp_path,transport)
    first,route=downloader(TODAY);second,_=downloader(TODAY)
    assert first==second and route==BY_CODE['CAN']['source']
    assert len(calls)==3 and sum('start_date=2000-01-01' in url for url in calls)==1
    assert (tmp_path/'canada-daily-api.json').exists()


@pytest.mark.parametrize('missing_cell',[False,True])
def test_recent_api_response_cannot_truncate_dates_or_a_single_maturity(tmp_path,missing_cell):
    calls=[]
    def transport(url,data=None):
        calls.append(url);data=fixture()
        if len(calls)>1:
            if missing_cell:data['observations'][0].pop(SERIES[0][1])
            else:data['observations'].pop(0)
        return json.dumps(data).encode()
    with pytest.raises(ValueError,match='Truncated'):
        CountryDownload(BY_CODE['CAN'],tmp_path,transport)(TODAY)


def test_minute_refresh_loads_new_release_and_retains_dated_data_on_source_failure(tmp_path):
    clock=[NOW];data=fixture();data['observations'].pop();calls=[]
    def transport(url,data=None):calls.append(url);return json.dumps(dataset[0]).encode()
    dataset=[data]
    downloader=CountryDownload(BY_CODE['CAN'],tmp_path/'sources',transport)
    service=TreasuryYieldService(tmp_path/'can.json',api_key='',definition=BY_CODE['CAN'],downloader=downloader,clock=lambda:clock[0])
    assert service.view(saved_only=True)['rows']==[] and not calls
    first=service.view();assert first['status']=='ready' and first['poll_interval_seconds']==60
    dataset[0]=fixture();clock[0]+=timedelta(seconds=59)
    assert service.view()['latest_observation_date']=='2026-10-28'
    clock[0]+=timedelta(seconds=1);second=service.view()
    assert second['latest_observation_date']=='2026-10-29' and second['metrics'][1]['value']==129
    downloader.transport=lambda *_:b'<html>Unavailable</html>';clock[0]+=timedelta(seconds=60)
    failed=service.view()
    assert failed['status']=='stale' and failed['quotes']==second['quotes'] and failed['last_checked_at']==second['last_checked_at']
    assert failed['poll_interval_seconds']==30
    saved=TreasuryYieldService(service.path,api_key='',definition=BY_CODE['CAN'],clock=lambda:clock[0])
    assert saved.view(saved_only=True)['quotes']==second['quotes']
    other=TreasuryYieldService(service.path,api_key='',definition=BY_CODE['USA'],enabled=False,clock=lambda:clock[0])
    assert not other.view()['rows']


def test_country_endpoint_returns_canadian_sources_and_daily_bill_history(tmp_path):
    app=create_app(str(tmp_path/'app.sqlite3'));service=app.state.sovereign_yields.services['CAN']
    service.enabled=True;service.clock=lambda:NOW
    service.downloader=CountryDownload(BY_CODE['CAN'],tmp_path/'sources',lambda *_:json.dumps(fixture()).encode())
    client=TestClient(app);result=client.get('/api/bonds/treasury?country=CAN').json()
    assert result['country']['currency']=='CAD' and result['country']['code']=='CAN'
    assert result['source_route']==BY_CODE['CAN']['source'] and len(result['curve'])==5
    assert result['quotes'][0]['series_id']=='TB.CDN.90D.MID' and result['metrics'][1]['date']=='2026-10-29'
    assert all('bankofcanada.ca' in source['url'] for source in result['sources'])
    assert 'remaining maturity varies' in result['coverage_note'] and len(result['metrics'])==8
