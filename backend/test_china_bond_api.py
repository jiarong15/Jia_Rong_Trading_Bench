from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from urllib.parse import parse_qs, urlsplit
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .china_bond_api import API, CURVE_ID, SHANGHAI, TERMS, history_url, parse_history
from .sovereign_yields import BY_CODE, CountryDownload
from .treasury_yields import TreasuryYieldService, analytics

NOW=datetime(2026,10,30,10,tzinfo=timezone.utc)


def fixture():
    dates=['2026-10-28','2026-10-29','2026-10-30']
    levels={'m3':[1.1634,1.1594,1.1643],'y2':[1.23,1.2218,1.2457],
            'y5':[1.3407,1.3297,1.3532],'y10':[1.6345,1.6233,1.65],'y30':[2.0489,2.0467,2.0826]}
    return [{'ycDefId':CURVE_ID+suffix,'ycDefName':f'ChinaBond Government Bond Yield Curve(YTM)({term})',
             'seriesData':[[int(datetime.fromisoformat(at).replace(tzinfo=SHANGHAI).timestamp()*1000),level] for at,level in zip(dates,levels[key])]}
            for key,term,suffix in TERMS]


def test_exact_api_tenors_populate_all_five_yields_and_eight_analytics_on_native_china_dates():
    rows=parse_history(json.dumps(fixture()),'2006-03-01','2026-10-30')
    assert [row['date'] for row in rows]==['2026-10-28','2026-10-29','2026-10-30']
    assert rows[-1]['y2']==1.2457
    view=analytics(rows,BY_CODE['CHN']['tenors'])
    assert len(view['curve'])==5 and view['curve_date']=='2026-10-30'
    assert {metric['key']:metric['value'] for metric in view['metrics']}=={
        '2s5s':10.75,'2s10s':40.43,'3m10s':48.57,'5s30s':72.94,'5s10s':29.68,'10s30s':43.26,
        '2s5s10s':-18.93,'2s10s30s':-2.83}
    assert all(quote['series_id'] and quote['date']=='2026-10-30' for quote in view['quotes'])


@pytest.mark.parametrize('problem',['missing_series','duplicate_series','wrong_curve','spot_basis','non_finite','boolean',
                                   'duplicate_date','out_of_range','non_midnight','reversed_dates'])
def test_invalid_identity_basis_values_and_dates_are_rejected(problem):
    data=fixture()
    if problem=='missing_series':data.pop()
    elif problem=='duplicate_series':data[-1]=deepcopy(data[0])
    elif problem=='wrong_curve':data[0]['ycDefId']='another-curve'
    elif problem=='spot_basis':data[0]['ycDefName']=data[0]['ycDefName'].replace('(YTM)','(Spot)')
    elif problem=='non_finite':data[0]['seriesData'][0][1]=float('nan')
    elif problem=='boolean':data[0]['seriesData'][0][1]=True
    elif problem=='duplicate_date':data[0]['seriesData'][1][0]=data[0]['seriesData'][0][0]
    elif problem=='out_of_range':data[0]['seriesData'][-1][0]+=86400000
    elif problem=='non_midnight':data[0]['seriesData'][0][0]+=3600000
    elif problem=='reversed_dates':data[0]['seriesData'].reverse()
    with pytest.raises(ValueError):parse_history(json.dumps(data),'2006-03-01','2026-10-30')


def test_missing_api_point_stays_missing_in_dependent_spread_and_butterflies():
    data=fixture();data[1]['seriesData'].pop()
    view=analytics(parse_history(json.dumps(data),'2006-03-01','2026-10-30'),BY_CODE['CHN']['tenors'])
    assert view['rows'][-1]['y2'] is None and view['rows'][-1]['2s10s'] is None
    assert view['rows'][-1]['2s5s10s'] is None and view['curve_date']=='2026-10-29'
    assert view['rows'][-1]['3m10s']==48.57


def test_download_is_post_json_daily_and_rechecks_recent_observations_without_refetching_full_history(tmp_path):
    calls=[]
    def transport(url,data=None):
        calls.append(url)
        assert data==b'' and url.startswith(API+'?')
        query=parse_qs(urlsplit(url).query)
        assert query['par']==['day'] and query['qxlx']==['0,']
        assert query['ycDefIds']==[CURVE_ID+','] and 'api_key' not in query
        return json.dumps(fixture()).encode()
    downloader=CountryDownload(BY_CODE['CHN'],tmp_path,transport)
    first,route=downloader(NOW.date());second,_=downloader(NOW.date())
    assert first==second and route=='ChinaBond · CCDC API'
    assert len(calls)==3 and sum('2006-03-01' in url for url in calls)==1
    assert (tmp_path/'china-api-history.json').exists()


def test_partial_recent_api_history_cannot_truncate_validated_history(tmp_path):
    count=[0]
    def transport(url,data=None):
        count[0]+=1;dataset=fixture()
        if count[0]>1:
            for series in dataset:series['seriesData'].pop(0)
        return json.dumps(dataset).encode()
    downloader=CountryDownload(BY_CODE['CHN'],tmp_path,transport)
    with pytest.raises(ValueError,match='truncated'):downloader(NOW.date())


def test_china_api_polls_each_minute_updates_new_release_and_keeps_saved_data_on_outage(tmp_path):
    clock=[NOW];initial=fixture();calls=[]
    for series in initial:series['seriesData'].pop()
    data_container=[initial]
    def transport(url,data=None):calls.append(url);return json.dumps(data_container[0]).encode()
    downloader=CountryDownload(BY_CODE['CHN'],tmp_path/'sources',transport)
    source=TreasuryYieldService(tmp_path/'chn.json',clock=lambda:clock[0],api_key='',definition=BY_CODE['CHN'],downloader=downloader)
    first=source.view();assert first['status']=='ready' and first['poll_interval_seconds']==60
    assert first['latest_observation_date']=='2026-10-29'
    data_container[0]=fixture();clock[0]+=timedelta(seconds=60)
    first=source.view()
    assert first['latest_observation_date']=='2026-10-30' and all(item['value'] is not None for item in first['metrics'])
    for series in data_container[0]:series['seriesData']=series['seriesData'][:-1]
    # A source regression is rejected; saved values and check time remain original.
    clock[0]+=timedelta(seconds=60)
    failed=source.view()
    assert failed['status']=='stale' and failed['last_checked_at']==first['last_checked_at']
    before=len(calls);saved=source.view(saved_only=True)
    assert len(calls)==before and saved['rows']==first['rows']
    data_container[0]=fixture();clock[0]+=timedelta(seconds=30)
    assert source.view()['status']=='ready'


def test_saved_only_endpoint_does_not_trigger_any_china_source_requests(tmp_path):
    app=create_app(str(tmp_path/'test.sqlite3'));source=app.state.sovereign_yields.services['CHN']
    source.enabled=True
    source.downloader=lambda _:(_ for _ in ()).throw(AssertionError('Saved-only requests must not use network'))
    response=TestClient(app).get('/api/bonds/treasury?country=CHN&saved_only=true')
    assert response.status_code==200 and response.json()['country']['code']=='CHN'
    assert response.json()['rows']==[] and response.headers['Cache-Control']=='no-store'
