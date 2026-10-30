"""Read-only Alpaca adapter. Paper brokerage and market-data hosts are distinct."""
from datetime import datetime, timedelta, timezone
import json
from math import isfinite, sqrt
import re
from statistics import stdev
from threading import RLock
import time
import urllib.error
import urllib.parse
import urllib.request
from .credentials import alpaca_credentials

DEFAULT_SYMBOLS=['AAPL','MSFT','NVDA','JPM','AMZN','GOOGL','META','TSLA','XOM','JNJ','SPY','QQQ']
NAMES={'AAPL':'Apple','MSFT':'Microsoft','NVDA':'NVIDIA','JPM':'JPMorgan Chase','AMZN':'Amazon','GOOGL':'Alphabet','META':'Meta Platforms','TSLA':'Tesla','XOM':'Exxon Mobil','JNJ':'Johnson & Johnson','SPY':'SPDR S&P 500 ETF','QQQ':'Invesco QQQ ETF'}

def symbols_from(value):
    symbols=list(dict.fromkeys(x.strip().upper() for x in value.split(',') if x.strip()))
    if not 1<=len(symbols)<=25 or any(not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,9}',s) for s in symbols):
        raise ValueError('Provide 1–25 valid US equity symbols.')
    return symbols

class AlpacaError(ValueError):pass

class AlpacaService:
    def __init__(self,enabled=True,transport=None):
        self.enabled=enabled;self.transport=transport;self.cache={};self.lock=RLock()
    def get(self,host,path,params=None):
        if not self.enabled and self.transport is None:raise AlpacaError('Live equities are disabled for this isolated test instance.')
        if self.transport:return self.transport(host,path,params or {})
        key,secret=alpaca_credentials()
        url=host+path+('?' + urllib.parse.urlencode(params) if params else '')
        request=urllib.request.Request(url,headers={'APCA-API-KEY-ID':key,'APCA-API-SECRET-KEY':secret,'Accept':'application/json'})
        try:
            with urllib.request.urlopen(request,timeout=20) as response:return json.load(response)
        except urllib.error.HTTPError as exc:
            messages={401:'Credentials were rejected.',403:'This data feed or resource is not permitted by the Alpaca subscription.',429:'Alpaca rate limit reached; wait before refreshing.'}
            raise AlpacaError(f'Alpaca HTTP {exc.code}. '+messages.get(exc.code,'The upstream request failed.')) from None
        except (OSError,ValueError):raise AlpacaError('Alpaca is unavailable or returned an invalid response. No synthetic fallback was substituted.') from None
    def cached(self,key,ttl,loader):
        with self.lock:
            record=self.cache.get(key)
            if record and time.monotonic()-record[0]<ttl:return record[1]
            value=loader();self.cache[key]=(time.monotonic(),value);return value
    def clock(self):
        return self.cached('clock',20,lambda:self.get('https://paper-api.alpaca.markets','/v2/clock'))
    def universe(self):
        def load():
            payload=self.get('https://paper-api.alpaca.markets','/v2/assets',{'status':'active','asset_class':'us_equity'})
            if not isinstance(payload,list):raise AlpacaError('Alpaca returned an invalid asset directory.')
            rows=[]
            for asset in payload:
                if asset.get('status')!='active' or asset.get('class')!='us_equity':continue
                rows.append({'symbol':asset['symbol'],'name':asset['name'],'exchange':asset.get('exchange',''),
                    'tradable':bool(asset.get('tradable'))})
            return {'assets':sorted(rows,key=lambda a:a['symbol']),'fetched_at':datetime.now(timezone.utc).isoformat()}
        return self.cached('universe',3600,load)
    def search(self,query='',offset=0,limit=25):
        directory=self.universe();term=' '.join(query.casefold().split());tokens=term.split()
        matches=[a for a in directory['assets'] if all(t in f"{a['symbol']} {a['name']}".casefold() for t in tokens)]
        def order(asset):
            symbol=asset['symbol'].casefold();name=asset['name'].casefold()
            return (0 if symbol==term else 1 if symbol.startswith(term) else 2 if name.startswith(term) else 3,symbol)
        matches.sort(key=order)
        return {'results':matches[offset:offset+limit],'total':len(matches),'universe_count':len(directory['assets']),
            'offset':offset,'limit':limit,'query':query,'source':'Alpaca asset directory','fetched_at':directory['fetched_at'],
            'coverage':'Active US equities available through Alpaca. Quote/history availability depends on the IEX feed.'}
    def bars(self,symbols):
        def load():
            end=datetime.now(timezone.utc)-timedelta(minutes=16)
            params={'symbols':','.join(symbols),'timeframe':'1Day','start':(end-timedelta(days=600)).isoformat(),'end':end.isoformat(),'feed':'iex','adjustment':'all','limit':10000,'sort':'asc'}
            result={s:[] for s in symbols};seen=set()
            for _ in range(20):
                payload=self.get('https://data.alpaca.markets','/v2/stocks/bars',params)
                for s,bars in (payload.get('bars') or {}).items():
                    if s in result:result[s].extend(bars)
                token=payload.get('next_page_token')
                if not token:break
                if token in seen:raise AlpacaError('Alpaca returned a repeated history page token.')
                seen.add(token);params['page_token']=token
            else:raise AlpacaError('History pagination limit exceeded; narrow the universe.')
            clock=self.clock();market_time=datetime.fromisoformat(clock['timestamp'].replace('Z','+00:00'))
            # The history request ends 16 minutes behind now. Do not call a
            # same-day bar complete until that cutoff is past the regular close.
            today=market_time.date().isoformat();include_today=not clock['is_open'] and market_time.hour*60+market_time.minute>=16*60+16
            normalized={}
            for s,bars in result.items():
                dated={}
                for b in bars:
                    day=b['t'][:10]
                    if day>today or (day==today and not include_today):continue
                    if not all(isfinite(float(b[k])) and b[k]>0 for k in ['o','h','l','c']):raise AlpacaError(f'Invalid historical prices for {s}.')
                    dated[day]={'date':day,'open':b['o'],'high':b['h'],'low':b['l'],'close':b['c'],'volume':b['v']}
                normalized[s]=[dated[d] for d in sorted(dated)]
            return normalized
        return self.cached(('bars',tuple(symbols)),900,load)
    def market(self,symbols):
        def load():
            clock=self.clock()
            raw=self.get('https://data.alpaca.markets','/v2/stocks/snapshots',{'symbols':','.join(symbols),'feed':'iex'})
            history=self.bars(symbols);rows=[]
            names={a['symbol']:a['name'] for a in self.universe()['assets']}
            now=datetime.now(timezone.utc)
            for s in symbols:
                snapshot=raw.get(s) or {};trade=snapshot.get('latestTrade') or {};quote=snapshot.get('latestQuote') or {}
                daily=snapshot.get('dailyBar') or {};prior=snapshot.get('prevDailyBar') or {}
                price=trade.get('p');stamp=trade.get('t');previous=prior.get('c')
                if price is not None and (not isfinite(price) or price<=0):raise AlpacaError(f'Invalid latest price for {s}.')
                age=max(0,(now-datetime.fromisoformat(stamp.replace('Z','+00:00'))).total_seconds()) if stamp else None
                bars=history.get(s,[]);momentum=momentum_metrics(bars)
                bid=quote.get('bp');ask=quote.get('ap')
                spread=(ask-bid)/((ask+bid)/2)*10000 if bid and ask and ask>=bid else None
                rows.append({'symbol':s,'name':names.get(s,NAMES.get(s,s)),'kind':'ETF' if s in ['SPY','QQQ'] else 'Stock','price':price,
                    'change_pct':(price/previous-1)*100 if price and previous else None,'trade_time':stamp,'quote_time':quote.get('t'),
                    'bid':bid,'ask':ask,'spread_bps':spread,'volume':daily.get('v'),'age_seconds':age,
                    'stale':age is None or (clock['is_open'] and age>300),'history_count':len(bars),**momentum})
            rankable=[r for r in rows if r['kind']=='Stock' and r['momentum_12_1'] is not None]
            distinct=sorted(set(r['momentum_12_1'] for r in rankable))
            for r in rows:
                r['momentum_percentile']= (100*distinct.index(r['momentum_12_1'])/(len(distinct)-1) if len(distinct)>1 else 50.0) if r in rankable else None
            return {'source':'Alpaca Market Data','feed':'iex','coverage':'IEX exchange only; not consolidated SIP/NBBO. Polling quotes, not a streaming feed.',
                'fetched_at':now.isoformat(),'clock':clock,'rows':rows,'history':history,'adjustment':'all',
                'fundamentals_available':False,'fundamentals_note':'This connector supplies prices, quotes and bars. Financial statements and a cost-of-capital estimate require a separate source or your explicitly entered inputs.',
                'history_note':'Momentum uses completed, adjusted daily bars. IEX-only prices/volume may differ from consolidated data. Historical bars are cached for 15 minutes; snapshots for 20 seconds.'}
        return self.cached(('market',tuple(symbols)),20,load)

def momentum_metrics(bars):
    prices=[b['close'] for b in bars]
    result={'momentum_12_1':None,'momentum_6_1':None,'return_1m':None,'volatility_annual_pct':None,'average_daily_dollar_volume':None}
    if len(prices)>=253:result['momentum_12_1']=(prices[-22]/prices[-253]-1)*100
    if len(prices)>=127:result['momentum_6_1']=(prices[-22]/prices[-127]-1)*100
    if len(prices)>=22:result['return_1m']=(prices[-1]/prices[-22]-1)*100
    if len(prices)>=64:
        result['volatility_annual_pct']=stdev([prices[n]/prices[n-1]-1 for n in range(len(prices)-63,len(prices))])*sqrt(252)*100
        result['average_daily_dollar_volume']=sum(b['close']*b['volume'] for b in bars[-20:])/20
    return result
