"""Canonical fixture provider. No remote quotes or fabricated live timestamps."""
from datetime import date, timedelta
from typing import Protocol
import hashlib
import math
import random

AS_OF = '2026-10-23T21:00:00Z'
ASSET_CLASSES = ['bonds', 'rates', 'fx', 'equities', 'commodities']

# id, name, class, price, change %, unit, display symbol, paper eligibility, quantity step, value multiplier
ROWS = [
 ('UST2','US Treasury 2Y','bonds',99.52,.12,'USD per 100','US 2Y',True,1000,.01),
 ('UST5','US Treasury 5Y','bonds',98.67,.19,'USD per 100','US 5Y',True,1000,.01),
 ('UST10','US Treasury 10Y','bonds',96.84,.31,'USD per 100','US 10Y',True,1000,.01),
 ('UST30','US Treasury 30Y','bonds',91.23,.46,'USD per 100','US 30Y',True,1000,.01),
 ('IG','US investment grade index','bonds',103.42,.08,'index','US IG',False,1,1),
 ('HY','US high yield index','bonds',106.18,-.14,'index','US HY',False,1,1),
 ('SOFR','SOFR overnight','rates',3.91,-.01,'percent','SOFR',False,1,1),
 ('US2Y','US Treasury 2Y yield','rates',3.84,-.02,'percent','US 2Y',False,1,1),
 ('US10Y','US Treasury 10Y yield','rates',4.18,-.03,'percent','US 10Y',False,1,1),
 ('US30Y','US Treasury 30Y yield','rates',4.61,-.04,'percent','US 30Y',False,1,1),
 ('DE10Y','Germany 10Y yield','rates',2.62,.02,'percent','DE 10Y',False,1,1),
 ('GB10Y','UK 10Y yield','rates',4.37,.01,'percent','UK 10Y',False,1,1),
 ('EURUSD','Euro / US dollar','fx',1.0864,.24,'USD per EUR','EUR/USD',False,1,1),
 ('GBPUSD','Sterling / US dollar','fx',1.2982,.18,'USD per GBP','GBP/USD',False,1,1),
 ('USDJPY','US dollar / Japanese yen','fx',151.62,-.32,'JPY per USD','USD/JPY',False,1,1),
 ('AUDUSD','Australian dollar / US dollar','fx',.6642,.41,'USD per AUD','AUD/USD',False,1,1),
 ('USDSGD','US dollar / Singapore dollar','fx',1.3187,-.09,'SGD per USD','USD/SGD',False,1,1),
 ('USDCHF','US dollar / Swiss franc','fx',.8663,-.16,'CHF per USD','USD/CHF',False,1,1),
 ('SPX','S&P 500 index','equities',5842.47,.82,'index','SPX',False,1,1),
 ('NDX','Nasdaq 100 index','equities',20352.18,1.14,'index','NDX',False,1,1),
 ('AAPL','Apple Inc.','equities',243.16,.64,'USD','AAPL',True,1,1),
 ('MSFT','Microsoft Corp.','equities',425.52,1.27,'USD','MSFT',True,1,1),
 ('NVDA','NVIDIA Corp.','equities',135.72,2.31,'USD','NVDA',True,1,1),
 ('JPM','JPMorgan Chase & Co.','equities',221.48,-.38,'USD','JPM',True,1,1),
 ('XAU','Gold spot','commodities',2736.40,.67,'USD per oz','XAU/USD',False,1,1),
 ('WTI','WTI crude front month','commodities',71.82,-1.23,'USD per bbl','WTI',False,1,1000),
 ('BRENT','Brent crude front month','commodities',75.96,-.91,'USD per bbl','BRENT',False,1,1000),
 ('COPPER','Copper front month','commodities',4.38,.84,'USD per lb','COPPER',False,1,25000),
 ('NATGAS','Natural gas front month','commodities',2.91,-.73,'USD per MMBtu','NATGAS',False,1,10000),
 ('SILVER','Silver spot','commodities',33.72,1.12,'USD per oz','XAG/USD',False,1,1),
]

def history(key: str, last: float, count: int = 260) -> list[dict]:
    rng = random.Random(int(hashlib.sha256(key.encode()).hexdigest()[:10],16))
    dates=[]; day=date(2026,10,23)
    while len(dates)<count:
        if day.weekday()<5: dates.append(day.isoformat())
        day-=timedelta(days=1)
    values=[100.0]
    scale=.002 if key.startswith('UST') or key in ['SOFR','US2Y','US10Y','US30Y','DE10Y','GB10Y'] else .009
    for n in range(1,count): values.append(values[-1]*(1+.00035+math.sin(n/14)*.0009+rng.gauss(0,scale)))
    ratio=last/values[-1]
    return [{'date':d,'close':round(v*ratio,6),'open':round((values[max(n-1,0)]*ratio)*(1+.0004*math.sin(n)),6)} for n,(d,v) in enumerate(zip(reversed(dates),values))]

class MarketProvider(Protocol):
    """Future adapters return canonical records; unavailable/auth failures must raise."""
    def instruments(self) -> list[dict]: ...
    def series(self, instrument_id: str) -> list[dict]: ...

class DemoProvider:
    def __init__(self):
        self.records=[]; self.histories={}
        yields=[3.84,3.97,4.18,4.61,4.92,7.23]
        for n,row in enumerate(ROWS):
            key,name,asset,price,change,unit,symbol,eligible,step,multiplier=row
            data={'id':key,'name':name,'asset_class':asset,'price':price,'change_pct':change,'unit':unit,'symbol':symbol,
              'currency':'USD','paper_eligible':eligible,'quantity_step':step,'multiplier':multiplier,
              'source_id':'demo-market','quality':'synthetic','event_time':AS_OF,'available_at':AS_OF,'ingested_at':AS_OF,
              'vintage':'demo-v1','quote_convention':unit,'yield_pct':yields[n] if asset=='bonds' else None,
              'duration':[1.9,4.5,8.1,16.4,6.2,3.4][n] if asset=='bonds' else 0,
              'sector': ['Technology','Financials','Consumer','Industrials'][n%4]}
            self.records.append(data); self.histories[key]=history(key,price)
    def instruments(self): return self.records
    def series(self,instrument_id): return self.histories[instrument_id]
    def instrument(self,key): return next((r for r in self.records if r['id']==key),None)

def curves():
    return [{'tenor':t,'current':v,'previous':round(v+d,2)} for t,v,d in zip(['1M','3M','6M','1Y','2Y','5Y','10Y','20Y','30Y'],[3.91,3.93,3.89,3.81,3.84,3.97,4.18,4.49,4.61],[.01,.01,.02,.02,.02,.025,.03,.035,.04])]
