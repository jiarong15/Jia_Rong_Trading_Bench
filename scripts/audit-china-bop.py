"""Independent audit of all seven China charts against retained live inputs.

Uses openpyxl (only this audit tool) to independently read SAFE's native cells;
the dashboard's XLSX reader and transformation functions are not used here.
"""
import argparse
from datetime import datetime,timezone
from decimal import Decimal
from hashlib import sha256
import csv
import io
import json
from pathlib import Path
import re
import sys
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1]
for folder in (ROOT,ROOT.parent/'.runtime'/'python',ROOT.parent/'.runtime'/'yfinance'):sys.path.insert(0,str(folder))
from openpyxl import load_workbook
from backend.bop_china import ChinaBOPService

D=lambda value:Decimal(str(value))
LABELS={
 'current_account':'1. Current account','goods':'1.A.a Goods','services':'1.A.b Services',
 'primary_income':'1.B Primary income','secondary_income':'1.C Secondary income',
 'capital_account':'2.1 Capital account','financial_account':'2.2 Financial account',
 'statistical_discrepancy':'3.Net errors and omissions',
 'direct_assets':'2.2.1.1.1 Assets','direct_liabilities':'2.2.1.1.2 Liabilities',
 'equity_assets':'2.2.1.2.1.1 Equity and investment fund shares','equity_liabilities':'2.2.1.2.2.1 Equity and investment fund shares',
 'debt_assets':'2.2.1.2.1.2 Debt securities','debt_liabilities':'2.2.1.2.2.2 Debt securities',
 'other_assets':'2.2.1.4.1 Assets','other_liabilities':'2.2.1.4.2 Liabilities','reserve_transactions':'2.2.2 Reserve assets',
}


def main():
 parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true');args=parser.parse_args()
 started=datetime.now(timezone.utc);checks,failures=0,[]
 def check(actual,expected,context):
  nonlocal checks
  checks+=1
  valid=actual is None if expected is None else actual is not None and abs(D(actual)-expected)<=D('.0000011')
  if not valid:failures.append({'context':context,'actual':actual,'expected':str(expected)})
 files={};inputs=[]
 folder=ROOT.parent/'.runtime'/'china-bop-research'/'live-inputs'
 for manifest in folder.glob('*.json'):
  request=json.loads(manifest.read_text(encoding='utf-8'));binary=manifest.with_suffix('.bin');body=binary.read_bytes()
  inputs.append({'request':request,'sha256':sha256(body).hexdigest()})
  if body.startswith(b'PK'):
   wb=load_workbook(io.BytesIO(body),read_only=True,data_only=True)
   if 'quarterly(USD)' in wb.sheetnames:files['bop']=wb
   elif 'Quarterly(USD)' in wb.sheetnames:files['positions']=wb
   elif '按美元计值' in wb.sheetnames:files['trade']=wb
   else:files['debt']=wb
  elif request.get('payload'):
   payload=json.loads(body,parse_float=Decimal)
   key='gdp' if len(request['payload']['indicatorIds'])==1 else 'customs'
   files[key]=payload
  elif 'NBCNBIS' in request['url']:files['currency']=body.decode('utf-8-sig')
 assert len(files)==7,'Run refresh-china-bop.py first to save all live inputs.'
 def normalized(value):return ' '.join(str(value or '').replace('\xa0',' ').split())
 def number(value):return None if value is None or str(value).strip() in ('/','','--','—') else D(value)
 cached_rows={}
 def sheet_rows(sheet):
  if id(sheet) not in cached_rows:cached_rows[id(sheet)]=list(sheet.rows)
  return cached_rows[id(sheet)]
 def getrow(sheet,label):
  result=[row for row in sheet_rows(sheet) if normalized(row[0].value)==label]
  assert len(result)==1,label
  return result[0]
 def quarter_code(label):
  match=re.fullmatch(r'(\d{4})Q([1-4])',str(label));return int(match[1])*4+int(match[2])-1 if match else None
 usd=files['bop']['quarterly(USD)'];rmb=files['bop']['quarterly(RMB)']
 raw={}
 native_rows={field:getrow(usd,label) for field,label in LABELS.items()}
 income=native_rows['primary_income'][0].row
 native_rows.update(income_receipts=sheet_rows(usd)[income],income_payments=sheet_rows(usd)[income+1])
 for column in range(2,usd.max_column+1):
  quarter=quarter_code(sheet_rows(usd)[3][column-1].value)
  if quarter is None:continue
  row={}
  for field,cells in native_rows.items():
   value=number(cells[column-1].value)
   reverse=field.endswith('_assets') or field in ('income_payments','financial_account','reserve_transactions')
   row[field]=(-value if reverse else value)/10 if value is not None else None
  raw[quarter]=row
 current_cny=getrow(rmb,'1. Current account')
 for column in range(2,rmb.max_column+1):
  quarter=quarter_code(sheet_rows(rmb)[3][column-1].value)
  if quarter in raw:raw[quarter]['current_account_cny']=number(current_cny[column-1].value)/10
 def date_quarter(value):
  if isinstance(value,datetime):return value.year*4+(value.month-1)//3
  for pattern in ('%d/%m/%Y','%d-%m-%Y','%Y-%m-%d','%m-%d-%Y','%m/%d/%Y'):
   try:
    at=datetime.strptime(str(value),pattern);return at.year*4+(at.month-1)//3
   except ValueError:pass
  raise ValueError(value)
 positions={};ws=files['positions']['Quarterly(USD)'];stock=getrow(ws,'5 Reserve assets')
 for column in range(2,ws.max_column+1):
  header=sheet_rows(ws)[2][column-1].value
  if header is not None and number(stock[column-1].value) is not None:positions[date_quarter(header)]=number(stock[column-1].value)/10
 debt={};ws=files['debt']['Sheet1'];sector_rows=[row for row in sheet_rows(ws) if len(row)>1 and normalized(row[1].value)=='Short-term']
 assert len(sector_rows)==4
 for column in range(3,ws.max_column+1):
  quarter=quarter_code(sheet_rows(ws)[1][column-1].value)
  if quarter is not None:debt[quarter]=sum(number(row[column-1].value) for row in sector_rows)/10
 gdp={};customs={}
 for group in ('gdp','customs'):
  for item in files[group]['data']:
   code=item['code'];year,part=int(code[:4]),int(code[4:6]);key=year*4+part-1 if group=='gdp' else year*12+part-1
   values={value['_id']:number(value.get('value')) for value in item['values']}
   if group=='gdp':
    value=values['d22612f09aeb4241bc557ef0ac61b3ba']
    if value is not None:gdp[key]=value/10
   else:
    exp,imp=values['a349b30535404467b37e68871e99762c'],values['6545926705124075961bf28602fd50bb']
    if exp is not None and imp is not None:customs[key]={'exports_ytd':exp/1_000_000,'imports_ytd':imp/1_000_000}
 custom_quarters={}
 for key,row in customs.items():
  if key%3!=2:continue
  prior={'exports_ytd':D(0),'imports_ytd':D(0)} if key%12==2 else customs.get(key-3)
  if prior:custom_quarters[key//3]={field:row[field+'_ytd']-prior[field+'_ytd'] for field in ('exports','imports')}
 currency={}
 for row in csv.DictReader(io.StringIO(files['currency'])):
  at=row['observation_date'];key=int(at[:4])*12+int(at[5:7])-1
  currency[key]=number(row['NBCNBIS'])
 trade={};monthly={};ws=files['trade']['按美元计值']
 for column in range(2,ws.max_column+1):
  credit,debit=number(sheet_rows(ws)[5][column-1].value),number(sheet_rows(ws)[6][column-1].value)
  if credit is None or debit is None:continue
  # Independent calendar position, rather than production's header parser.
  month=2015*12+column-2;span=2 if '1-2' in str(sheet_rows(ws)[3][column-1].value) else 1
  expected={'exports':credit/10,'imports':-debit/10,'trade_balance':(credit+debit)/10,'span_months':span}
  monthly[month]=expected
  quarter=month//3;group=trade.setdefault(quarter,{'exports':D(0),'imports':D(0),'covered':set()})
  covered=set(range(month-span+1,month+1));assert not group['covered']&covered
  group['covered'].update(covered)
  for field in ('exports','imports'):group[field]+=expected[field]
 trade={key:row for key,row in trade.items() if row['covered']==set(range(key*3,key*3+3))}
 def summed(lookup,indices,field):
  values=[lookup.get(index,{}).get(field) for index in indices]
  return sum(values) if values and all(value is not None for value in values) else None
 service=ChinaBOPService(ROOT/'data'/'bop-cache'/'china.json',enabled=False)
 # Verify native observations before period transformations.
 for name,group in service.snapshot['groups'].items():
  for row in group['rows']:
   at=row['date'];month=int(at[:4])*12+int(at[5:7])-1;quarter=month//3
   if name=='bop':expected=raw[quarter]
   elif name=='positions':expected={'reserve_stock':positions[quarter]}
   elif name=='debt':expected={'short_term_external_debt':debt[quarter]}
   elif name=='gdp':expected={'gdp_cny':gdp[quarter]}
   elif name=='customs':expected=customs[month]
   elif name=='currency':expected={'NBCNBIS':currency[month]}
   else:expected=monthly[month]
   for field,value in expected.items():check(row.get(field),D(value) if value is not None else None,'native/'+name+'/'+at+'/'+field)
 live_verified=False
 for frequency in ('quarterly','trailing4q'):
  if args.live:
   with urlopen('http://127.0.0.1:8000/api/fx/bop?country=CHN&frequency='+frequency+'&refresh='+('true' if frequency=='quarterly' else 'false'),timeout=150) as response:
    assert response.headers['Cache-Control']=='no-store';view=json.load(response)
   if frequency=='quarterly':
    assert view['delivery']=='source_checked' and not view['macro']['warnings']
    assert all(datetime.fromisoformat(source['checked_at'])>=started for source in view['sources'])
    live_verified=True
  else:view=service.view(frequency)
  count=4 if frequency=='trailing4q' else 1
  selected_trade={}
  for key in trade:
   selected_trade[key]={field:summed(trade,range(key-count+1,key+1),field) for field in ('exports','imports')}
  for row in view['series']:
   at=row['date'];quarter=int(at[:4])*4+(int(at[5:7])-1)//3;indices=range(quarter-count+1,quarter+1)
   for field in ('current_account','goods','services','primary_income','secondary_income'):
    check(row[field],summed(raw,indices,field),frequency+'/core/'+at+'/'+field)
   exports=summed(custom_quarters,indices,'exports');imports=summed(custom_quarters,indices,'imports')
   expected=exports-imports if exports is not None and imports is not None else None
   check(row['customs_goods'],expected,frequency+'/customs/'+at)
   check(row['goods_gap'],expected-summed(raw,indices,'goods') if expected is not None else None,frequency+'/gap/'+at)
  for row in view['macro']['quarterly']:
   at=row['date'];quarter=int(at[:4])*4+(int(at[5:7])-1)//3;indices=range(quarter-count+1,quarter+1)
   numerator=summed(raw,indices,'current_account_cny');gdps=[gdp.get(key) for key in indices]
   expected=100*numerator/sum(gdps) if numerator is not None and all(value is not None for value in gdps) else None
   check(row['current_account_gdp_pct'],expected,frequency+'/CA-GDP/'+at)
   for kind in ('direct','equity','debt','other'):
    assets,liabilities=summed(raw,indices,kind+'_assets'),summed(raw,indices,kind+'_liabilities')
    check(row['net_'+kind],liabilities-assets if liabilities is not None and assets is not None else None,frequency+'/net-'+kind+'/'+at)
   check(row['reserve_transactions'],summed(raw,indices,'reserve_transactions'),frequency+'/reserve-flow/'+at)
   expected=100*positions[quarter]/debt[quarter] if quarter in positions and quarter in debt else None
   check(row['reserves_debt_pct'],expected,frequency+'/reserve-ratio/'+at)
   values=[currency.get(month) for month in range((quarter-count+1)*3,quarter*3+3)]
   check(row['currency_index'],sum(values)/len(values) if all(value is not None for value in values) else None,frequency+'/RMB-index/'+at)
  for row in view['macro']['trade']:
   at=row['date'];quarter=int(at[:4])*4+(int(at[5:7])-1)//3
   for field in ('exports','imports'):
    value=selected_trade.get(quarter,{}).get(field);previous=selected_trade.get(quarter-4,{}).get(field)
    check(row[field],value,frequency+'/trade-'+field+'/'+at)
    check(row[field+'_yoy'],100*(value/previous-1) if value is not None and previous is not None else None,frequency+'/YoY-'+field+'/'+at)
    window=[selected_trade.get(key,{}).get(field) for key in range(quarter-2,quarter+1)]
    check(row[field+'_ma3'],sum(window)/3 if all(value is not None for value in window) else None,frequency+'/MA3-'+field+'/'+at)
   exports,imports=(selected_trade.get(quarter,{}).get(field) for field in ('exports','imports'))
   check(row['trade_balance'],exports-imports if exports is not None and imports is not None else None,frequency+'/balance/'+at)
 report={'checked_at':started.isoformat(),'checks':checks,'failures':failures,'live_refresh_verified':live_verified,'source_inputs':inputs}
 destination=ROOT/'data'/('china-bop-audit-'+started.date().isoformat()+'.json')
 destination.write_text(json.dumps(report,indent=2),encoding='utf-8')
 print(json.dumps({'checks':checks,'failures':len(failures),'live_refresh_verified':live_verified,'report':str(destination)}))
 if failures:print(json.dumps(failures[:10],indent=2));raise SystemExit(1)


if __name__=='__main__':main()
