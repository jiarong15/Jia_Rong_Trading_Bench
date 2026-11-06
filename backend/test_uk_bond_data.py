from datetime import date, datetime, timedelta, timezone
import io
import json
from zipfile import ZipFile
import pytest
from fastapi.testclient import TestClient
from . import uk_bond_data as uk
from .app import create_app
from .sovereign_yields import BY_CODE, CountryDownload
from .test_sovereign_yields import workbook
from .treasury_yields import TreasuryYieldService, analytics

TODAY=date(2026,10,30)
NOW=datetime(2026,10,30,21,tzinfo=timezone.utc)


def tender(stamp='2026-10-30',level='4.166711000000',term='3 Months',matures='2027-02-01T00:00:00'):
    return (f'<di TBILL_MATURITY_TYPE="{term}" MATURITY_DATE="{matures}"><dd TENDER_DATE="{stamp}T00:00:00">'
            '<sdd ISSUE_DATE="2026-11-02T00:00:00" SIZE_MILLIONS="2000">'
            f'<far AVERAGE_YIELD_PERCENT="{level}" AVERAGE_PRICE_GBP="98.971856" YIELD_TAIL_BP="1.8289"/>'
            '</sdd></dd></di>')


def bills():
    return ('\r\n<Data>'+tender()+tender(stamp='2026-10-28',level='4.16')+tender(term='6 Months',level='99.9')+'</Data>').encode()


def test_dmo_parser_reads_only_gbp_three_month_tender_yields_on_original_dates():
    assert uk.parse_bills(bills(),TODAY)=={'2026-10-28':4.16,'2026-10-30':4.166711}
    assert uk.parse_bills(bills().replace(b'4.16',b'-0.16'),TODAY)['2026-10-28']==-0.16


@pytest.mark.parametrize('body',[
    bills().replace(b'AVERAGE_YIELD_PERCENT',b'AVERAGE_PRICE_GBP'),
    bills().replace(b'4.166711000000',b'NaN'),
    bills().replace(b'2026-10-30T00:00:00',b'2026-11-01T00:00:00'),
    bills().replace(b'2027-02-01',b'2027-11-01'),
    bills().replace(b'2026-10-30T00:00:00',b'2026-10-30T12:00:00'),
    bills().replace(b'</Data>',tender().encode()+b'</Data>'),
    bills().replace(b'3 Months',b'1 Month'),
    b'<!DOCTYPE Data [<!ENTITY x "oops">]><Data/>',b'<html>ShieldSquare Captcha</html>'
])
def test_wrong_yield_fields_nonfinite_future_dates_terms_duplicate_tenders_and_html_are_rejected(body):
    with pytest.raises((ValueError,KeyError,uk.ET.ParseError)):uk.parse_bills(body,TODAY)


def zipped(name,body):
    output=io.BytesIO()
    with ZipFile(output,'w') as archive:archive.writestr(name,body)
    return output.getvalue()


def transport_fixture():
    calls=[];blocked=[False];revision=[False]
    def transport(url,data=None):
        calls.append(url)
        if url==uk.BILL_API:return b'<html>ShieldSquare Captcha</html>' if blocked[0] else bills()
        body=workbook()
        if revision[0]:
            output=io.BytesIO()
            with ZipFile(io.BytesIO(body)) as source,ZipFile(output,'w') as target:
                for name in source.namelist():
                    raw=source.read(name)
                    if name.endswith('sheet1.xml'):raw=raw.replace(b'<v>3</v>',b'<v>3.1</v>')
                    target.writestr(name,raw)
            body=output.getvalue()
        if url==uk.UK+'glcnominalddata.zip':return zipped('GLC Nominal daily data_2000 to present.xlsx',body)
        if url==uk.UK+'latest-yield-curve-data.zip':return zipped('GLC Nominal daily data current month.xlsx',body)
        raise AssertionError('Unexpected country/source '+url)
    return transport,calls,blocked,revision


def test_uk_adapter_merges_only_matching_dates_and_checks_both_current_sources(tmp_path):
    transport,calls,blocked,revision=transport_fixture()
    downloader=CountryDownload(BY_CODE['GBR'],tmp_path,transport)
    rows,route,metadata=downloader(TODAY)
    view=analytics(rows,BY_CODE['GBR']['tenors'])
    assert route==BY_CODE['GBR']['source'] and not metadata['source_warning']
    assert view['latest_observation_date']=='2026-10-30' and view['curve_date']=='2026-10-28'
    assert len(view['curve'])==5 and all(item['value'] is not None for item in view['quotes']+view['metrics'])
    assert view['rows'][-1]['y10'] is None and view['rows'][-1]['3m10s'] is None
    assert view['rows'][1]['m3'] is None and view['rows'][1]['3m10s'] is None
    calls.clear();downloader(TODAY)
    assert calls==[uk.UK+'latest-yield-curve-data.zip',uk.BILL_API] or set(calls)=={uk.UK+'latest-yield-curve-data.zip',uk.BILL_API}


def test_blocked_bill_api_keeps_dated_bill_data_and_still_updates_gilts(tmp_path):
    transport,calls,blocked,revision=transport_fixture()
    downloader=CountryDownload(BY_CODE['GBR'],tmp_path/'sources',transport)
    clock=[NOW]
    service=TreasuryYieldService(tmp_path/'gbr.json',definition=BY_CODE['GBR'],api_key='',downloader=downloader,clock=lambda:clock[0])
    first=service.view();assert first['status']=='ready' and first['poll_interval_seconds']==60
    bill_checked=first['source_checks']['bills']['last_checked_at']
    blocked[0]=True;revision[0]=True;clock[0]+=timedelta(seconds=60)
    second=service.view()
    assert second['status']=='stale' and not second['error'] and '3M bill API is unavailable' in second['source_warning']
    assert second['quotes'][0]==first['quotes'][0] and second['quotes'][3]['value']==3.1
    assert second['source_checks']['bills']=={'status':'stale','last_checked_at':bill_checked}
    assert second['source_checks']['gilts']['status']=='ready' and second['metrics'][0]['value']==110
    restarted=TreasuryYieldService(service.path,definition=BY_CODE['GBR'],api_key='',clock=lambda:clock[0])
    assert restarted.view(saved_only=True)['source_warning']==second['source_warning']
    assert restarted.view(saved_only=True)['source_checks']==second['source_checks']


def test_cold_start_with_unavailable_bill_api_can_still_show_live_uk_gilts(tmp_path):
    transport,calls,blocked,revision=transport_fixture();blocked[0]=True
    downloader=CountryDownload(BY_CODE['GBR'],tmp_path/'sources',transport)
    service=TreasuryYieldService(tmp_path/'gbr.json',definition=BY_CODE['GBR'],api_key='',downloader=downloader,clock=lambda:NOW)
    view=service.view()
    assert view['status']=='stale' and view['quotes'][0]['value'] is None
    assert view['quotes'][3]['value']==3 and view['metrics'][0]['value']==100
    assert view['metrics'][1]['value'] is None and not view['error'] and view['source_checks']['bills']['status']=='unavailable'


def test_truncated_api_response_keeps_all_verified_bill_history(tmp_path):
    transport,*_=transport_fixture()
    downloader=CountryDownload(BY_CODE['GBR'],tmp_path,transport)
    original,status,_=uk.bill_snapshot(downloader,TODAY)
    downloader.transport=lambda *_:('<Data>'+tender()+tender(stamp='2026-10-29')+'</Data>').encode()
    retained,next_status,warning=uk.bill_snapshot(downloader,TODAY)
    assert retained==original and next_status['status']=='stale' and warning
    assert next_status['last_checked_at']==status['last_checked_at']


def test_uk_endpoint_has_uk_sources_and_saved_only_never_downloads(tmp_path):
    app=create_app(str(tmp_path/'app.sqlite3'));source=app.state.sovereign_yields.services['GBR']
    transport,calls,*_=transport_fixture()
    source.enabled=True;source.clock=lambda:NOW;source.downloader=CountryDownload(BY_CODE['GBR'],tmp_path/'sources',transport)
    client=TestClient(app)
    assert client.get('/api/bonds/treasury?country=GBR&saved_only=true').json()['rows']==[] and not calls
    response=client.get('/api/bonds/treasury?country=GBR');view=response.json()
    assert response.headers['Cache-Control']=='no-store' and view['country']['currency']=='GBP'
    assert view['country']['code']=='GBR' and len(view['curve'])==5
    assert view['sources'][0]['url']==uk.BILL_REFERENCE
    assert all('stlouisfed' not in item['url'] for item in view['sources'])
    assert len(view['metrics'])==7 and 'tender-date' in view['frequency']
