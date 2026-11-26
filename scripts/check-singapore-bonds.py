"""Independent MAS daily closing-table check and audit of every Singapore plot."""
from datetime import datetime
from decimal import Decimal
from html.parser import HTMLParser
import json
from urllib.request import Request,urlopen

FORMULAS={'2s5s':{'y5':1,'y2':-1},'2s10s':{'y10':1,'y2':-1},'3m10s':{'y10':1,'m3':-1},'5s30s':{'y30':1,'y5':-1},
          '5s10s':{'y10':1,'y5':-1},'10s30s':{'y30':1,'y10':-1},
          '2s5s10s':{'y5':2,'y2':-1,'y10':-1},'2s10s30s':{'y10':2,'y2':-1,'y30':-1}}


def fetch(url):
    with urlopen(Request(url,headers={'User-Agent':'Mozilla/5.0','Cache-Control':'no-cache'}),timeout=120) as response:return response.read()


class ClosingTables(HTMLParser):
    def __init__(self,body):
        super().__init__(convert_charrefs=True);self.tables=[];self.table=None;self.row=None;self.cell=None;self.feed(body.decode('utf-8-sig'))
    def handle_starttag(self,tag,attrs):
        if tag=='table':self.table=[]
        elif tag=='tr' and self.table is not None:self.row=[]
        elif tag in ('td','th') and self.row is not None:self.cell=[]
        elif tag=='br' and self.cell is not None:self.cell.append(' ')
    def handle_data(self,text):
        if self.cell is not None:self.cell.append(text)
    def handle_endtag(self,tag):
        if tag in ('td','th') and self.cell is not None:self.row.append(' '.join(''.join(self.cell).split()));self.cell=None
        elif tag=='tr' and self.row is not None:self.table.append(self.row);self.row=None
        elif tag=='table' and self.table is not None:self.tables.append(self.table);self.table=None


def main():
    market=json.loads(fetch('http://127.0.0.1:8000/api/bonds/treasury?country=SGP&refresh=true'))
    assert market['country']['code']=='SGP' and market['country']['currency']=='SGD'
    assert market['status']=='ready' and not market['error'] and market['poll_interval_seconds']==60
    assert market['source_route']=='MAS \u00b7 official SGS benchmarks'
    assert len(market['curve'])==4 and len(market['metrics'])==len(FORMULAS)
    assert market['quotes'][0]['value'] is None and market['quotes'][0]['series_id'] is None
    assert all('mas.gov.sg' in source['url'] for source in market['sources'])
    tables=ClosingTables(fetch('https://eservices.mas.gov.sg/Statistics/fdanet/SgsBenchmarkIssuePrices.aspx')).tables
    table=next(table for table in tables if len(table)>4 and table[3]==['Yield','Yield']+['Price','Yield']*7)
    assert table[1]==['','6-Mth','1-Year','2-Year','5-Year','10-Year','15-Year','20-Year','30-Year','50-Year']
    official={datetime.strptime(row[0],'%d %b %Y').date().isoformat():row for row in table[4:] if len(row)==17}
    columns={'y2':4,'y5':6,'y10':8,'y30':14}
    for quote in market['quotes'][1:]:assert Decimal(str(quote['value']))==Decimal(official[quote['date']][columns[quote['key']]])
    checked=0
    for row in market['rows']:
        assert row['m3'] is None and row['3m10s'] is None
        for key,weights in FORMULAS.items():
            if any(row[leg] is None for leg in weights):assert row[key] is None
            else:
                expected=sum(Decimal(str(row[leg]))*weight for leg,weight in weights.items())*100
                assert abs(expected-Decimal(str(row[key])))<=Decimal('0.000001');checked+=1
    print('PASS: four Singapore benchmark yields match the separate official MAS daily closing table.')
    print('PASS:',checked,'same-date spread/butterfly calculations; no substituted 3M rate; one-minute source checks.')
    print('Latest observation:',market['latest_observation_date'],'| checked:',market['last_checked_at'])
    for quote in market['quotes'][1:]:print(quote['label'],quote['value'],'percent |',quote['date'])
    print('3M SGS Treasury-bill issuance ended in June 2013. 3M and 3m10s remain unavailable.')


if __name__=='__main__':main()
