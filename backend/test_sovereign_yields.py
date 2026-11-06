from datetime import date, datetime, timedelta, timezone
import io
import json
from pathlib import Path
from zipfile import ZipFile
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .sovereign_yields import BY_CODE, CountryDownload, parse_canada, parse_china, parse_japan, parse_singapore, parse_uk_workbook
from .treasury_yields import TreasuryYieldService, analytics, validate_rows

def japan_rows():
    return [{**row,'m3':1.0} for row in parse_japan(JP)]


TODAY = date(2026,10,30)
NOW = datetime(2026,10,30,21,tzinfo=timezone.utc)
JP = b'Interest Rate\nDate,1Y,2Y,5Y,10Y,30Y\n2026/10/28,1,1.2,1.5,1.8,2.3\n2026/10/29,1,1.3,1.6,1.9,2.4\n'


def china_row(stamp, name='ChinaBond Government Bond Yield Curve'):
    return '<tr>'+''.join(f'<td>{value}</td>' for value in [name,stamp,1.2,1.3,1.4,1.45,1.5,1.6,1.8,2.3])+'</tr>'


def china_body():
    return ('<table><tr>'+''.join('<th>'+label+'</th>' for label in ['Yield Curve Name','Date','3M','6M','1Y','3Y','5Y','7Y','10Y','30Y'])+'</tr>'+china_row('2026-10-28')+china_row('2026-10-29')+'</table>').encode()


def test_country_analytics_never_substitute_another_tenor_or_us_data():
    rows=parse_japan(JP)
    view=analytics(rows,[(key,label,None if key=='m3' else identifier,years) for key,label,identifier,years in BY_CODE['JPN']['tenors']])
    assert view['quotes'][0]['value'] is None and view['quotes'][0]['series_id'] is None
    assert view['curve_date']=='2026-10-29' and len(view['curve'])==4
    assert view['rows'][-1]['2s10s']==60 and view['rows'][-1]['3m10s'] is None
    rows=parse_china(china_body(),'2026-01-01','2026-10-30')
    view=analytics(rows,[(key,label,None if key=='y2' else identifier,years) for key,label,identifier,years in BY_CODE['CHN']['tenors']])
    assert view['quotes'][1]['value'] is None and len(view['curve'])==4
    assert {key:view['rows'][-1][key] for key in ['3m10s','5s30s','5s10s','10s30s']}=={'3m10s':60,'5s30s':80,'5s10s':30,'10s30s':50}
    assert view['rows'][-1]['2s5s10s'] is None and view['rows'][-1]['2s10s30s'] is None


def test_japan_current_csv_accepts_its_published_cp932_footer_and_empty_rows():
    body=(JP.decode()+'\n,,,,,\n"  \u203bIf you cannot download the latest csv data, clear the browser cache.",,,,,\n').encode('cp932')
    assert parse_japan(body)==parse_japan(JP)


@pytest.mark.parametrize('replacement',[(b'Government Bond',b'Corporate Bond'),(b'2026-10-29',b'2026-11-01'),(b'1.5',b'NaN'),(b'<th>10Y</th>',b'<th>20Y</th>')])
def test_chinabond_rejects_wrong_curve_dates_values_and_columns(replacement):
    with pytest.raises(ValueError):parse_china(china_body().replace(*replacement),'2026-01-01','2026-10-30')


def test_canada_weekly_bill_never_fills_unpublished_daily_observations():
    ids=['BD.CDN.2YR.DQ.YLD','BD.CDN.5YR.DQ.YLD','BD.CDN.10YR.DQ.YLD','BD.CDN.LONG.DQ.YLD']
    bonds={'seriesDetail':dict.fromkeys(ids,{}),'observations':[{'d':stamp,**{key:{'v':str(value)} for key,value in zip(ids,[2,2.5,3,3.5])}} for stamp in ['2026-10-28','2026-10-29']]}
    bills={'seriesDetail':{'V80691344':{}},'observations':[{'d':'2026-10-28','V80691344':{'v':'1.5'}}]}
    rows=sorted(parse_canada(json.dumps(bonds),json.dumps(bills)),key=lambda row:row['date'])
    assert rows[-1]['m3'] is None and rows[0]['m3']==1.5
    view=analytics(rows,BY_CODE['CAN']['tenors'])
    assert view['curve_date']=='2026-10-28' and view['latest_observation_date']=='2026-10-29'
    assert view['rows'][-1]['3m10s'] is None and view['rows'][-1]['2s10s']==100
    bonds['observations'].append(bonds['observations'][0])
    with pytest.raises(ValueError):parse_canada(json.dumps(bonds),json.dumps(bills))


def test_mas_dates_carry_within_year_and_month_without_reading_prices_or_discontinued_bill():
    header='<tr><th>End of Period</th>'+''.join(f'<th>{label} Yield</th>' for label in ['3-Month T-Bill','2-Year Bond','5-Year Bond','10-Year Bond','30-Year Bond'])+'</tr>'
    body=('<table>'+header+'<tr><td>2026</td><td>Oct</td><td>28</td><td>99</td><td>1.2</td><td>1.4</td><td>1.6</td><td>1.8</td></tr>'
          '<tr><td></td><td></td><td>29</td><td>99</td><td>1.3</td><td>1.5</td><td>1.7</td><td>1.9</td></tr></table>').encode()
    rows=parse_singapore(body)
    assert [row['date'] for row in rows]==['2026-10-28','2026-10-29']
    assert rows[-1]['m3'] is None and rows[-1]['y30']==1.9
    with pytest.raises(ValueError):parse_singapore(body.replace(b'5-Year Bond',b'7-Year Bond'))


def workbook(missing_thirty=False, bad_tenor=False):
    output=io.BytesIO()
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    columns=[('B',2),('C',5),('D',10)]+([] if missing_thirty else [('E',30)])
    if bad_tenor: columns[1]=('C',7)
    with ZipFile(output,'w') as z:
        z.writestr('xl/workbook.xml',f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="4. spot curve" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        headers='<row r="4">'+''.join(f'<c r="{col}4"><v>{years}</v></c>' for col,years in columns)+'</row>'
        rows='<row r="5"><c r="A5" t="e"><v>#VALUE!</v></c></row>'
        for i,stamp in enumerate([date(2026,10,28),date(2026,10,29)],6):
            serial=(stamp-date(1899,12,30)).days
            rows+=f'<row r="{i}"><c r="A{i}"><v>{serial}</v></c>'+''.join(f'<c r="{col}{i}"><v>{value}</v></c>' for (col,_),value in zip(columns,[2,2.5,3,3.5]))+'</row>'
        z.writestr('xl/worksheets/sheet1.xml',f'<worksheet xmlns="{ns}"><sheetData>{headers}{rows}</sheetData></worksheet>')
    return output.getvalue()


def test_uk_reads_exact_published_spot_cells_and_preserves_historical_maturity_gaps():
    rows=parse_uk_workbook(workbook())
    assert rows[-1]=={'date':'2026-10-29','m3':None,'y2':2,'y5':2.5,'y10':3,'y30':3.5}
    assert all(row['y30'] is None for row in parse_uk_workbook(workbook(missing_thirty=True)))
    with pytest.raises(ValueError):parse_uk_workbook(workbook(bad_tenor=True))


def test_country_snapshots_are_isolated_and_automatic_checks_pick_up_new_releases(tmp_path):
    clock=[NOW]
    rows=japan_rows()
    calls=[]
    def download(_):calls.append(True);return rows,'Japan Ministry of Finance'
    jp=TreasuryYieldService(tmp_path/'japan.json',clock=lambda:clock[0],api_key='',definition=BY_CODE['JPN'],downloader=download)
    first=jp.view()
    assert first['poll_interval_seconds']==60 and first['status']=='ready'
    rows.append({**rows[-1],'date':'2026-10-30','y10':2.1})
    clock[0]+=timedelta(seconds=59)
    assert jp.view()['latest_observation_date']=='2026-10-29'
    clock[0]+=timedelta(seconds=1)
    assert jp.view()['latest_observation_date']=='2026-10-30' and len(calls)==2
    other=TreasuryYieldService(jp.path,enabled=False,clock=lambda:clock[0],api_key='',definition=BY_CODE['GBR'])
    assert other.view()['rows']==[]
    assert json.loads(jp.path.read_text())['country']=='JPN'


def test_country_endpoint_selects_the_right_service_and_rejects_unknown_codes(tmp_path):
    app=create_app(str(tmp_path/'app.sqlite3'))
    jp=app.state.sovereign_yields.services['JPN']
    jp.enabled=True;jp.api_key='';jp.clock=lambda:NOW;jp.downloader=lambda _: (japan_rows(),'Japan Ministry of Finance')
    client=TestClient(app)
    result=client.get('/api/bonds/treasury?country=JPN').json()
    assert result['country']['code']=='JPN' and result['country']['currency']=='JPY'
    assert len(result['countries'])==6 and result['source']=='Japan Ministry of Finance'
    assert result['quotes'][1]['value']==1.3 and result['coverage_note']
    assert client.get('/api/bonds/treasury?country=ZZZ').status_code==422
    assert client.get('/api/bonds/treasury').json()['country']['code']=='USA'


def test_truncated_source_does_not_replace_saved_country_data(tmp_path):
    clock=[NOW]
    source=TreasuryYieldService(tmp_path/'jp.json',clock=lambda:clock[0],api_key='',definition=BY_CODE['JPN'],downloader=lambda _:(japan_rows(),'Japan Ministry of Finance'))
    first=source.view()
    clock[0]+=timedelta(minutes=5)
    source.downloader=lambda _:([parse_japan(JP)[-1]],'Japan Ministry of Finance')
    result=source.view()
    assert result['status']=='stale' and result['last_checked_at']==first['last_checked_at']
    assert result['rows']==first['rows']
