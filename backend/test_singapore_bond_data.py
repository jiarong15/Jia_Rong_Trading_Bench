from datetime import date, datetime, timedelta, timezone
import json
from urllib.parse import parse_qs
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .singapore_bond_data import PREFIX, SG, download, query
from .sovereign_yields import BY_CODE, CountryDownload
from .treasury_yields import TreasuryYieldService, analytics

TODAY=date(2026,10,30)
NOW=datetime(2026,10,30,15,tzinfo=timezone.utc)
FORM=b'<input type="hidden" name="__VIEWSTATE" value="view"><input type="hidden" name="__EVENTVALIDATION" value="validation">'


def table(dates=('2026-10-28','2026-10-29','2026-10-30'),missing=False):
    header='<table><tr><th>End of Period</th>'+''.join('<th>'+name+' Yield</th>' for name in ['3-Month T-Bill','2-Year Bond','5-Year Bond','10-Year Bond','30-Year Bond'])+'</tr>'
    rows=[]
    for stamp in dates:
        day=int(stamp[-2:]);values=['2026','Oct',str(day),'99','1.87','2.03','2.24','2.27']
        if missing and day==30:values[4]='-'
        rows.append('<tr>'+''.join('<td>'+value+'</td>' for value in values)+'</tr>')
    return (header+''.join(rows)+'</table>').encode()


def transport_fixture():
    calls=[];body=[table()];form=[FORM]
    def transport(url,data=None):
        assert url==SG  # No US, SORA, interbank or central-bank bill source.
        calls.append(data)
        if data is None:return form[0]
        fields=parse_qs(data.decode())
        assert fields[PREFIX+'FrequencyDropDownList']==['D']
        assert fields[PREFIX+'EndYearDropDownList']==['2026'] and fields[PREFIX+'EndMonthDropDownList']==['10']
        for name in ['ThreeMonthTreasuryBillYield','TwoYearBondYield','FiveYearBondYield','TenYearBondYield','ThirtyYearBondYield']:
            assert fields[PREFIX+name+'CheckBox']==['on']
        return body[0]
    return transport,calls,body,form


def test_official_benchmark_query_reads_yields_and_never_revives_the_discontinued_bill(tmp_path):
    transport,calls,*_=transport_fixture()
    rows=query(CountryDownload(BY_CODE['SGP'],tmp_path,transport),'2000-01-01',TODAY)
    assert all(row['m3'] is None for row in rows)
    assert rows[-1]=={'date':'2026-10-30','m3':None,'y2':1.87,'y5':2.03,'y10':2.24,'y30':2.27}
    view=analytics(rows,BY_CODE['SGP']['tenors'])
    assert len(view['curve'])==4 and view['curve_date']=='2026-10-30'
    assert view['quotes'][0]['value'] is None and view['quotes'][0]['series_id'] is None
    assert {item['key']:item['value'] for item in view['metrics']}=={
        '2s10s':37,'3m10s':None,'5s30s':24,'5s10s':21,'10s30s':3,'2s5s10s':-5,'2s10s30s':34}
    assert parse_qs(calls[-1].decode())[PREFIX+'StartYearDropDownList']==['2000']


@pytest.mark.parametrize('problem',['missing_tokens','wrong_maturity','wrong_measure','nonfinite','future_date','duplicate_date'])
def test_changed_form_schema_prices_maturities_and_bad_dates_fail_validation(tmp_path,problem):
    transport,calls,body,form=transport_fixture()
    if problem=='missing_tokens':form[0]=b'<html>Unavailable</html>'
    elif problem=='wrong_maturity':body[0]=body[0].replace(b'30-Year Bond',b'20-Year Bond')
    elif problem=='wrong_measure':body[0]=body[0].replace(b'5-Year Bond Yield',b'5-Year Bond Price')
    elif problem=='nonfinite':body[0]=body[0].replace(b'2.03',b'NaN')
    elif problem=='future_date':body[0]=table(dates=('2026-10-29','2026-10-30','2026-10-31'))
    elif problem=='duplicate_date':body[0]=table(dates=('2026-10-28','2026-10-29','2026-10-29'))
    with pytest.raises(ValueError):query(CountryDownload(BY_CODE['SGP'],tmp_path,transport),'2000-01-01',TODAY)


def test_native_gaps_stay_missing_for_their_dependent_spreads_and_butterflies(tmp_path):
    transport,calls,body,_=transport_fixture();body[0]=table(missing=True)
    rows=query(CountryDownload(BY_CODE['SGP'],tmp_path,transport),'2000-01-01',TODAY)
    view=analytics(rows,BY_CODE['SGP']['tenors'])
    assert view['rows'][-1]['y2'] is None and view['rows'][-1]['2s10s'] is None
    assert view['rows'][-1]['2s5s10s'] is None and view['rows'][-1]['2s10s30s'] is None
    assert view['rows'][-1]['5s30s']==24 and view['curve_date']=='2026-10-29'


def test_full_source_history_is_cached_but_recent_releases_are_checked_each_time(tmp_path):
    transport,calls,*_=transport_fixture();downloader=CountryDownload(BY_CODE['SGP'],tmp_path,transport)
    first,route=downloader(TODAY);second,_=downloader(TODAY)
    assert first==second and route==BY_CODE['SGP']['source'] and len(calls)==4
    assert parse_qs(calls[1].decode())[PREFIX+'StartYearDropDownList']==['2000']
    assert parse_qs(calls[3].decode())[PREFIX+'StartMonthDropDownList']==['8']
    path=tmp_path/'singapore-sgs-history.json';saved=json.loads(path.read_text())
    saved['checked_at']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat();path.write_text(json.dumps(saved))
    downloader(TODAY)
    assert parse_qs(calls[-1].decode())[PREFIX+'StartYearDropDownList']==['2000']


@pytest.mark.parametrize('missing_cell',[False,True])
def test_truncated_recent_history_cannot_overwrite_verified_source_rows(tmp_path,missing_cell):
    transport,calls,body,_=transport_fixture();downloader=CountryDownload(BY_CODE['SGP'],tmp_path,transport)
    downloader(TODAY)
    body[0]=table(missing=True) if missing_cell else table(dates=('2026-10-29','2026-10-30'))
    with pytest.raises(ValueError,match='Truncated'):downloader(TODAY)


def test_minute_refresh_updates_new_release_and_retains_original_dates_during_outage(tmp_path):
    transport,calls,body,form=transport_fixture();body[0]=table(dates=('2026-10-28','2026-10-29'))
    clock=[NOW];downloader=CountryDownload(BY_CODE['SGP'],tmp_path/'sources',transport)
    service=TreasuryYieldService(tmp_path/'sgp.json',api_key='',definition=BY_CODE['SGP'],downloader=downloader,clock=lambda:clock[0])
    assert service.view(saved_only=True)['rows']==[] and not calls
    first=service.view();assert first['status']=='ready' and first['poll_interval_seconds']==60
    body[0]=table();clock[0]+=timedelta(seconds=59)
    assert service.view()['latest_observation_date']=='2026-10-29'
    clock[0]+=timedelta(seconds=1);second=service.view()
    assert second['latest_observation_date']=='2026-10-30' and second['metrics'][0]['value']==37
    form[0]=b'<html>Unavailable</html>';clock[0]+=timedelta(seconds=60)
    failed=service.view()
    assert failed['status']=='stale' and failed['quotes']==second['quotes'] and failed['last_checked_at']==second['last_checked_at']
    assert failed['poll_interval_seconds']==30
    saved=TreasuryYieldService(service.path,api_key='',definition=BY_CODE['SGP'],clock=lambda:clock[0])
    assert saved.view(saved_only=True)['quotes']==second['quotes']


def test_endpoint_returns_only_singapore_sources_and_discloses_unavailable_maturity(tmp_path):
    app=create_app(str(tmp_path/'app.sqlite3'));service=app.state.sovereign_yields.services['SGP']
    transport,*_=transport_fixture();service.enabled=True;service.clock=lambda:NOW
    service.downloader=CountryDownload(BY_CODE['SGP'],tmp_path/'sources',transport)
    response=TestClient(app).get('/api/bonds/treasury?country=SGP');view=response.json()
    assert response.headers['Cache-Control']=='no-store' and view['country']['currency']=='SGD'
    assert view['country']['code']=='SGP' and view['source_route']==BY_CODE['SGP']['source']
    assert len(view['curve'])==4 and len(view['metrics'])==7 and view['metrics'][1]['value'] is None
    assert all('mas.gov.sg' in source['url'] for source in view['sources'])
    assert view['quotes'][0]['value'] is None and '20 June 2013' in view['coverage_note']
