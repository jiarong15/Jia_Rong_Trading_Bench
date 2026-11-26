"""Audit UK-only API plots and independently compare official release values."""
import argparse
from datetime import date,timedelta
from decimal import Decimal
import io
import json
from urllib.request import Request,urlopen
from xml.etree import ElementTree as ET
from zipfile import ZipFile

FORMULAS={'2s5s':{'y5':1,'y2':-1},'2s10s':{'y10':1,'y2':-1},'3m10s':{'y10':1,'m3':-1},'5s30s':{'y30':1,'y5':-1},
          '5s10s':{'y10':1,'y5':-1},'10s30s':{'y30':1,'y10':-1},
          '2s5s10s':{'y5':2,'y2':-1,'y10':-1},'2s10s30s':{'y10':2,'y2':-1,'y30':-1}}
NS='{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'


def fetch(url):
    with urlopen(Request(url,headers={'User-Agent':'Mozilla/5.0','Cache-Control':'no-cache'}),timeout=120) as response:return response.read()


def official_gilts():
    body=fetch('https://www.bankofengland.co.uk/-/media/boe/files/statistics/yield-curves/latest-yield-curve-data.zip')
    with ZipFile(io.BytesIO(body)) as outer,ZipFile(io.BytesIO(outer.read('GLC Nominal daily data current month.xlsx'))) as book:
        strings=[''.join(item.itertext()) for item in ET.fromstring(book.read('xl/sharedStrings.xml'))]
        sheets=ET.fromstring(book.read('xl/workbook.xml')).find(NS+'sheets')
        sheet=next(item for item in sheets if item.attrib['name'] in ('4. spot curve','4. nominal spot curve'))
        relation=sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
        target=next(item.attrib['Target'] for item in ET.fromstring(book.read('xl/_rels/workbook.xml.rels')) if item.attrib['Id']==relation)
        path=target.lstrip('/') if target.startswith('/') else 'xl/'+target
        rows={};columns=None
        for row in ET.fromstring(book.read(path)).find(NS+'sheetData'):
            cells={}
            for cell in row:
                raw=cell.findtext(NS+'v')
                cells[cell.attrib['r'].rstrip('0123456789')]=strings[int(raw)] if cell.attrib.get('t')=='s' else raw if cell.attrib.get('t')!='e' else None
            if row.attrib['r']=='4':
                columns={key:next(column for column,value in cells.items() if value is not None and value.replace('.','',1).isdigit() and Decimal(value)==years) for key,years in [('y2',2),('y5',5),('y10',10),('y30',30)]}
            elif columns and cells.get('A') and cells['A'].replace('.','',1).isdigit():
                stamp=(date(1899,12,30)+timedelta(days=int(float(cells['A'])))).isoformat()
                rows[stamp]={key:Decimal(cells[column]) if cells.get(column) is not None else None for key,column in columns.items()}
        return rows


def main():
    args=argparse.ArgumentParser();args.add_argument('--require-all-sources',action='store_true');options=args.parse_args()
    market=json.loads(fetch('http://127.0.0.1:8000/api/bonds/treasury?country=GBR&refresh=true'))
    assert market['country']['code']=='GBR' and market['country']['currency']=='GBP'
    assert market['status'] in ('ready','stale') and not market['error'] and market['poll_interval_seconds']==60
    assert len(market['curve'])==5 and all(item['value'] is not None for item in market['quotes']+market['metrics'])
    assert all('stlouisfed' not in item['url'] for item in market['sources'])
    gilts=official_gilts()
    for quote in market['quotes'][1:]:assert abs(Decimal(str(quote['value']))-gilts[quote['date']][quote['key']])<Decimal('0.000000000001')
    print('PASS: four gilt maturities match a separate fresh Bank of England release download.')
    try:
        root=ET.fromstring(fetch('https://www.dmo.gov.uk/data/XmlDataReport?reportCode=D2.2D'))
        if root.tag!='Data':raise ValueError('DMO did not return its XML data schema.')
        official={tender.attrib['TENDER_DATE'][:10]:Decimal(result.attrib['AVERAGE_YIELD_PERCENT'])
                  for instrument in root if instrument.attrib.get('TBILL_MATURITY_TYPE')=='3 Months'
                  for tender in instrument.findall('dd') for result in tender.findall('sdd/far')}
        quote=market['quotes'][0];assert Decimal(str(quote['value']))==official[quote['date']]
        print('PASS: 3M matches a separate fresh UK DMO XML API response.')
    except (OSError,ET.ParseError,ValueError):
        print('NOTE: independent DMO API check unavailable; retained 3M data must be read with its source status.')
        if options.require_all_sources:raise SystemExit('FAIL: all-source freshness could not be verified.')
    checked=0
    for row in market['rows']:
        for key,weights in FORMULAS.items():
            if any(row[leg] is None for leg in weights):assert row[key] is None
            else:
                expected=sum(Decimal(str(row[leg]))*weight for leg,weight in weights.items())*100
                assert abs(expected-Decimal(str(row[key])))<=Decimal('0.000001');checked+=1
    for metric in market['metrics']:
        latest=next(row for row in reversed(market['rows']) if row[metric['key']] is not None)
        assert metric['date']==latest['date'] and metric['value']==latest[metric['key']]
    print('PASS:',checked,'same-date spread/butterfly calculations; five-point curve on',market['curve_date'])
    print('Source status:',json.dumps(market['source_checks']))
    if market['source_warning']:print('SOURCE WARNING:',market['source_warning'])
    for quote in market['quotes']:print(quote['label'],quote['value'],'percent |',quote['date'])
    if options.require_all_sources:assert market['status']=='ready' and not market['source_warning']


if __name__=='__main__':main()
