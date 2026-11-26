"""Independently audit the Japan JSON endpoint against fresh official releases."""
import csv
from decimal import Decimal
from html.parser import HTMLParser
import io
import json
from urllib.request import Request, urlopen

FORMULAS={'2s5s':{'y5':1,'y2':-1},'2s10s':{'y10':1,'y2':-1},'3m10s':{'y10':1,'m3':-1},'5s30s':{'y30':1,'y5':-1},
          '5s10s':{'y10':1,'y5':-1},'10s30s':{'y30':1,'y10':-1},
          '2s5s10s':{'y5':2,'y2':-1,'y10':-1},'2s10s30s':{'y10':2,'y2':-1,'y30':-1}}
ROOT='https://www.mof.go.jp/english/policy/jgbs/'


def fetch(url):
    with urlopen(Request(url,headers={'User-Agent':'Mozilla/5.0','Cache-Control':'no-cache'}),timeout=120) as response:return response.read()


class Cells(HTMLParser):
    def __init__(self,body):
        super().__init__(convert_charrefs=True);self.cells=[];self.cell=None;self.feed(body.decode('utf-8-sig'))
    def handle_starttag(self,tag,attrs):
        if tag in ('td','th'):self.cell=[]
        elif tag=='br' and self.cell is not None:self.cell.append(' ')
    def handle_data(self,data):
        if self.cell is not None:self.cell.append(data)
    def handle_endtag(self,tag):
        if tag in ('td','th') and self.cell is not None:self.cells.append(' '.join(''.join(self.cell).split()));self.cell=None


def main():
    market=json.loads(fetch('http://127.0.0.1:8000/api/bonds/treasury?country=JPN&refresh=true'))
    assert market['country']['code']=='JPN' and market['status']=='ready' and not market['error']
    assert market['source_route']=='Japan Ministry of Finance' and market['poll_interval_seconds']==60
    assert len(market['curve'])==5 and all(item['value'] is not None for item in market['quotes']+market['metrics'])
    quotes={item['key']:item for item in market['quotes']}
    raw=fetch(ROOT+'reference/interest_rate/jgbcme.csv').decode('cp932')
    lines=raw.splitlines();start=next(i for i,line in enumerate(lines) if line.startswith('Date,1Y,2Y,'))
    official=list(csv.DictReader(io.StringIO('\n'.join(lines[start:]))))
    for key,tenor in [('y2','2Y'),('y5','5Y'),('y10','10Y'),('y30','30Y')]:
        stamp=quotes[key]['date'];year,month,day=map(int,stamp.split('-'))
        row=next(row for row in official if row['Date']==f'{year}/{month}/{day}')
        assert Decimal(str(quotes[key]['value']))==Decimal(row[tenor])
    stamp=quotes['m3']['date'];year,month,day=map(int,stamp.split('-'))
    cells=Cells(fetch(ROOT+'auction/calendar/etbill/etbillresul/eresul'+stamp.replace('-','')+'.htm')).cells
    assert 'Yield at the Average Price' in cells
    index=cells.index(f'{month}/{day}/{year}')
    assert Decimal(cells[index+9].rstrip('%'))==Decimal(str(quotes['m3']['value']))
    checked=0
    for row in market['rows']:
        for key,weights in FORMULAS.items():
            if any(row[leg] is None for leg in weights):assert row[key] is None
            else:
                expected=sum(Decimal(str(row[leg]))*weight for leg,weight in weights.items())*100
                assert abs(expected-Decimal(str(row[key])))<=Decimal('0.000001');checked+=1
    for metric in market['metrics']:
        row=next(row for row in market['rows'] if row['date']==metric['date'])
        assert row[metric['key']]==metric['value']
    print('PASS: all five Japan API yields match fresh MOF daily/auction releases.')
    print('PASS:',checked,'same-date spread and butterfly calculations; one-minute source checks.')
    print('Latest observation:',market['latest_observation_date'],'| checked:',market['last_checked_at'])
    for item in market['quotes']:print(item['label'],item['value'],'percent |',item['date'])
    print('3M is an auction yield, published roughly weekly; other maturities are daily. No intraday feed is claimed.')


if __name__=='__main__':main()
