from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from . import japan_bond_data as jp
from .sovereign_yields import BY_CODE, CountryDownload
from .treasury_yields import TreasuryYieldService, analytics

TODAY=date(2026,10,30)
HEADERS=['Issue Number','Auction Date','Issue Date','Maturity Date','Amounts of Competitive Bids (billion yen)',
         'Amounts of Bids Accepted (billion yen)','Lowest Accepted Price (per 100 yen)',
         'Yield at the Lowest Accepted Price','Allotment for Bids at the Lowest Accepted Price',
         'Weighted Average Price (per 100 yen)','Yield at the Average Price','Amounts of Non-price-competitive Auction']


def result(stamp='2026-10-29',issue='1416',level='1.2706%'):
    auction=date.fromisoformat(stamp)
    cells=[issue,auction.strftime('%m/%d/%Y'),'11/2/2026','2/8/2027','9,803.20','2,529.68','99.6585','1.2762%',
           '47.7590%','99.6600',level,'770.30']
    return ('<table><tr>'+''.join('<th>'+s+'</th>' for s in HEADERS)+'</tr><tr>'+''.join('<td>'+s+'</td>' for s in cells)+'</tr></table>').encode()


def calendar(stamp='2026-10-29',issue='1416',term='3-month',linked=True):
    auction=date.fromisoformat(stamp)
    link=f'<a href="etbill/etbillresul/eresul{auction:%Y%m%d}.htm">Detail</a>' if linked else '-'
    return (f'<h1>Auction Calendar</h1><table><tr><td>{auction:%b}. {auction.day}, {auction.year}</td>'
            f'<td>Treasury Discount Bills ({term})({issue})</td><td>Announcement</td><td>{link}</td><td>-</td></tr></table>').encode()


def test_calendar_reads_only_three_month_result_links_and_skips_scheduled_auctions():
    parsed=jp.Calendar(calendar()+calendar(term='6-month')+calendar(stamp='2026-10-30',linked=False),2026,10)
    assert parsed.results==[(date(2026,10,29),'1416',jp.AUCTIONS+'calendar/etbill/etbillresul/eresul20261029.htm')]
    with pytest.raises(ValueError):jp.Calendar(calendar(),2026,9)
    with pytest.raises(ValueError):jp.Calendar(calendar().replace(b'etbill/etbillresul/eresul20261029.htm',b'https://example.com/result'),2026,10)


def test_calendar_handles_may_without_a_period_and_january_year_rollover():
    assert jp.Calendar(calendar(stamp='2026-05-08').replace(b'May.',b'May'),2026,5).results[0][0]==date(2026,5,8)
    assert jp.Calendar(calendar(stamp='2027-01-08'),2027,1).results[0][0]==date(2027,1,8)


@pytest.mark.parametrize('body,stamp,issue',[
    (result(level='1.2706'),TODAY-timedelta(days=1),'1416'),
    (result(),TODAY,'1416'),(result(),TODAY-timedelta(days=1),'9999'),
    (result().replace(b'2/8/2027',b'11/8/2027'),TODAY-timedelta(days=1),'1416'),
    (result().replace(b'Yield at the Average Price',b'Bond Price'),TODAY-timedelta(days=1),'1416')])
def test_results_reject_wrong_units_dates_issue_maturity_and_columns(body,stamp,issue):
    with pytest.raises(ValueError):jp.parse_bill_result(body,stamp,issue)


def test_result_uses_average_price_yield_not_price_or_stop_out_yield():
    assert jp.parse_bill_result(result(),date(2026,10,29),'1416')==1.2706
    assert jp.parse_bill_result(result(level='-0.1234%'),date(2026,10,29),'1416')==-0.1234


def fake_book(level=1.25,duplicate=False,header='Yield at the Average Price(%)'):
    headers=['Issue Number','Maturity','Auction Date','Issue Date','Maturity Date','','','','Average Price',header,'','','']
    def cells(stamp,term='3-month'):
        dates=[(date.fromisoformat(stamp)-date(1899,12,30)).days,46328,46426]
        raw=[1416,term,*dates,0,0,0,99.66,level,0,0,0]
        return [SimpleNamespace(value=value,ctype=jp.xlrd.XL_CELL_DATE if i in (2,3,4) else 2) for i,value in enumerate(raw)]
    records=[[],[],[],cells('2026-10-23'),cells('2026-10-29'),cells('2026-10-28','6-month')]
    if duplicate:records.append(cells('2026-10-29'))
    sheet=SimpleNamespace(name='FY2026',nrows=len(records),ncols=13,row_values=lambda _:headers,get_rows=lambda:iter(records))
    return SimpleNamespace(datemode=0,sheets=lambda:[sheet],release_resources=lambda:None)


def test_workbook_preserves_auction_dates_and_filters_maturity(monkeypatch):
    monkeypatch.setattr(jp.xlrd,'open_workbook',lambda **_:fake_book())
    assert jp.parse_bill_history(b'workbook',TODAY)=={'2026-10-23':1.25,'2026-10-29':1.25}


@pytest.mark.parametrize('options',[{'level':float('nan')},{'duplicate':True},{'header':'Price'}])
def test_workbook_rejects_nonfinite_duplicates_or_wrong_yield_column(monkeypatch,options):
    monkeypatch.setattr(jp.xlrd,'open_workbook',lambda **_:fake_book(**options))
    with pytest.raises(ValueError):jp.parse_bill_history(b'workbook',TODAY)


def test_adapter_checks_current_month_and_does_not_fill_daily_bill_gaps(tmp_path,monkeypatch):
    monkeypatch.setattr(jp,'parse_bill_history',lambda *_:{'2026-10-23':1.25,'2026-10-29':1.26})
    csv=b'Date,1Y,2Y,5Y,10Y,30Y\n2026/10/23,1,1.8,2.2,2.8,3.9\n2026/10/28,1,1.9,2.3,2.9,4.0\n2026/10/29,1,1.9,2.3,2.9,4.0\n'
    calls=[]; latest=[1.2706]
    def transport(url,data=None):
        calls.append(url)
        if url in (jp.JP,jp.JP_CURRENT):return csv
        if url==jp.BILLS:return b'workbook'
        if url.endswith('2609e.htm'):return b'<h1>Auction Calendar</h1>'
        if url.endswith('2610e.htm'):return calendar(stamp='2026-10-23',issue='1415')+calendar()
        if url.endswith('eresul20261023.htm'):return result('2026-10-23','1415','1.2680%')
        if url.endswith('eresul20261029.htm'):return result(level=f'{latest[0]}%')
        raise AssertionError('Unexpected download '+url)
    downloader=CountryDownload(BY_CODE['JPN'],tmp_path/'sources',transport)
    rows,_=downloader(TODAY)
    view=analytics(rows,BY_CODE['JPN']['tenors'])
    assert rows[1]['m3'] is None and view['rows'][1]['3m10s'] is None
    assert view['quotes'][0]['value']==1.2706 and view['curve_date']=='2026-10-29'
    assert len(view['curve'])==5 and all(item['value'] is not None for item in view['metrics'])
    assert view['rows'][-1]['3m10s']==162.94
    calls.clear();latest[0]=1.28
    revised,_=downloader(TODAY)
    assert revised[-1]['m3']==1.28 and jp.JP not in calls and jp.BILLS not in calls
    assert not any(url.endswith('eresul20261023.htm') for url in calls)
    assert any(url.endswith('eresul20261029.htm') for url in calls)


def test_saved_api_and_minute_refresh_preserve_all_five_maturities_and_retry_failures(tmp_path):
    clock=[datetime(2026,10,30,tzinfo=timezone.utc)]
    rows=[dict(date=stamp,m3=1.2,y2=1.9,y5=2.3,y10=2.9,y30=4.0) for stamp in ['2026-10-23','2026-10-29']]
    calls=[]
    def download(_):calls.append(True);return rows,'Japan Ministry of Finance'
    service=TreasuryYieldService(tmp_path/'jpn.json',definition=BY_CODE['JPN'],downloader=download,clock=lambda:clock[0],api_key='')
    assert service.view(saved_only=True)['rows']==[] and not calls
    first=service.view();assert first['status']=='ready' and first['poll_interval_seconds']==60
    saved=TreasuryYieldService(service.path,definition=BY_CODE['JPN'],downloader=download,clock=lambda:clock[0],api_key='')
    assert saved.view(saved_only=True)['quotes']==first['quotes'] and len(calls)==1
    clock[0]+=timedelta(seconds=59)
    assert service.view()['quotes']==first['quotes'] and len(calls)==1
    rows[-1]['y10']=3.0;clock[0]+=timedelta(seconds=1)
    assert service.view()['metrics'][0]['value']==110 and len(calls)==2
    def fail(_):raise ValueError('Invalid upstream release')
    service.downloader=fail;clock[0]+=timedelta(seconds=60)
    stale=service.view()
    assert stale['status']=='stale' and stale['quotes'][3]['value']==3.0 and stale['poll_interval_seconds']==30


def test_regressed_bill_history_cannot_hide_behind_newer_daily_jgb_yields(tmp_path):
    clock=[datetime(2026,10,30,tzinfo=timezone.utc)]
    rows=[dict(date=stamp,m3=1.2,y2=1.9,y5=2.3,y10=2.9,y30=4.0) for stamp in ['2026-10-16','2026-10-23','2026-10-29']]
    service=TreasuryYieldService(tmp_path/'jpn.json',definition=BY_CODE['JPN'],downloader=lambda _:(rows,'Japan Ministry of Finance'),clock=lambda:clock[0],api_key='')
    before=service.view()
    rows[-1]['m3']=None
    rows.append({**rows[-1],'date':'2026-10-30'})
    clock[0]+=timedelta(seconds=60)
    after=service.view()
    assert after['status']=='stale' and after['quotes']==before['quotes'] and after['last_checked_at']==before['last_checked_at']
