"""Independently audit Canada's bond plots against official Valet CSV downloads."""
import csv
from decimal import Decimal
import io
import json
from urllib.request import Request,urlopen

FORMULAS={'2s5s':{'y5':1,'y2':-1},'2s10s':{'y10':1,'y2':-1},'3m10s':{'y10':1,'m3':-1},'5s30s':{'y30':1,'y5':-1},
          '5s10s':{'y10':1,'y5':-1},'10s30s':{'y30':1,'y10':-1},
          '2s5s10s':{'y5':2,'y2':-1,'y10':-1},'2s10s30s':{'y10':2,'y2':-1,'y30':-1}}
IDS={'m3':'TB.CDN.90D.MID','y2':'BD.CDN.2YR.DQ.YLD','y5':'BD.CDN.5YR.DQ.YLD',
     'y10':'BD.CDN.10YR.DQ.YLD','y30':'BD.CDN.LONG.DQ.YLD'}


def fetch(url):
    with urlopen(Request(url,headers={'User-Agent':'Mozilla/5.0','Cache-Control':'no-cache'}),timeout=120) as response:return response.read()


def official(group):
    body=fetch(f'https://www.bankofcanada.ca/valet/observations/group/{group}/csv?recent=10')
    records=list(csv.reader(io.StringIO(body.decode('utf-8-sig'))))
    header=next(i for i,row in enumerate(records) if row and row[0].casefold()=='date')
    return {record['date']:record for record in csv.DictReader(io.StringIO('\n'.join(','.join(row) for row in records[header:])))}


def main():
    market=json.loads(fetch('http://127.0.0.1:8000/api/bonds/treasury?country=CAN&refresh=true'))
    assert market['country']['code']=='CAN' and market['country']['currency']=='CAD'
    assert market['status']=='ready' and not market['error'] and not market['source_warning']
    assert market['source_route']=='Bank of Canada \u00b7 Valet JSON API' and market['poll_interval_seconds']==60
    assert len(market['curve'])==5 and len(market['metrics'])==len(FORMULAS) and len(market['quotes'])==5
    assert all(item['value'] is not None for item in market['quotes']+market['metrics'])
    assert all('bankofcanada.ca' in item['url'] for item in market['sources'])
    bonds=official('bond_yields_benchmark');bills=official('money_market')
    for quote in market['quotes']:
        series=IDS[quote['key']];assert quote['series_id']==series
        source=bills if quote['key']=='m3' else bonds
        assert Decimal(str(quote['value']))==Decimal(source[quote['date']][series])
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
    print('PASS: all five Canadian JSON API yields match separate official CSV group downloads, including the daily 3M bill.')
    print('PASS:',checked,'same-date spread and butterfly calculations; one-minute automatic API checks.')
    print('Latest observation:',market['latest_observation_date'],'| checked:',market['last_checked_at'])
    for quote in market['quotes']:print(quote['label'],quote['value'],'percent |',quote['date'])
    print('30Y is the designated long-term government benchmark; remaining maturity varies. Official daily releases, not intraday quotes.')


if __name__=='__main__':main()
