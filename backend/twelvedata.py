"""Backend-only FX adapter with explicit provenance and a shared credit budget."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import isfinite, sqrt
import json
import re
from statistics import stdev
from threading import RLock
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .credentials import twelvedata_key

DEFAULT_PAIRS=['EUR/USD','GBP/USD','USD/JPY','AUD/USD','USD/SGD','USD/CHF']

class TwelveDataError(ValueError):
    def __init__(self,message,code=503,retry_after=0):
        super().__init__(message);self.code=code;self.retry_after=retry_after

def pair_from(value):
    pair=re.sub(r'[\s/\-]','',value.upper())
    if not re.fullmatch('[A-Z]{6}',pair) or pair[:3]==pair[3:]:raise ValueError('Use a currency pair such as EUR/USD or USD/JPY.')
    return pair[:3]+'/'+pair[3:]

def pairs_from(value):
    pairs=list(dict.fromkeys(pair_from(p) for p in value.split(',') if p.strip()))
    if not 1<=len(pairs)<=12:raise ValueError('Compare 1–12 FX pairs at a time.')
    return pairs

def finite_number(value,positive=False):
    try:number=float(value) if not isinstance(value,bool) else None
    except (TypeError,ValueError):return None
    return number if number is not None and isfinite(number) and (not positive or number>0) else None

def fx_metrics(bars):
    prices=[b['close'] for b in bars if b['complete']]
    result={'completed_observations':len(prices),'return_1m_pct':None,'volatility_20d_pct':None,'mean_50d':None,'distance_50d_pct':None}
    if len(prices)>=22:result['return_1m_pct']=(prices[-1]/prices[-22]-1)*100
    if len(prices)>=21:result['volatility_20d_pct']=stdev([prices[i]/prices[i-1]-1 for i in range(len(prices)-20,len(prices))])*sqrt(252)*100
    if len(prices)>=50:
        mean=sum(prices[-50:])/50;result.update(mean_50d=mean,distance_50d_pct=(prices[-1]/mean-1)*100)
    return result

class TwelveDataService:
    def __init__(self,enabled=True,transport=None,clock=None,key_getter=None):
        self.enabled=enabled;self.transport=transport;self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.key_getter=key_getter or twelvedata_key
        self.lock=RLock();self.cache={};self.minute=None;self.day=None;self.used=0;self.daily_used=0
        self.limit=8;self.daily_limit=800;self.plan='unknown';self.initialized=False;self.usage_observed_at=None;self.blocked_until=None
    def rollover(self):
        now=self.clock();minute=now.strftime('%Y-%m-%dT%H:%M');day=now.date().isoformat()
        if minute!=self.minute:self.minute=minute;self.used=0
        if day!=self.day:self.day=day;self.daily_used=0
        return now
    def quota(self):
        with self.lock:
            now=self.rollover();retry=max(0,int((self.blocked_until-now).total_seconds())+1) if self.blocked_until else 0
            return {'plan':self.plan,'minute_limit':self.limit,'minute_used':self.used,'minute_remaining':max(0,self.limit-self.used),
                'daily_limit':self.daily_limit,'daily_used_observed':self.daily_used,'daily_remaining_observed':max(0,self.daily_limit-self.daily_used) if self.daily_limit else None,
                'observed_at':self.usage_observed_at,'retry_after_seconds':retry,
                'note':'Observed account usage plus this server\'s requests; other applications may also use this key. Refresh and history requests share the credit budget.'}
    def request(self,path,params=None,metered=True):
        with self.lock:
            now=self.rollover()
            if not self.enabled and self.transport is None:raise TwelveDataError('Live FX is disabled in this isolated test instance.')
            key=None
            if self.transport is None:
                try:key=self.key_getter()
                except (OSError,ValueError,KeyError,TypeError):
                    raise TwelveDataError('Twelve Data is not configured on this computer. Run python scripts/configure-fx.py or set backend TWELVEDATA_API_KEY, then restart the server.') from None
                if not isinstance(key,str) or not key.strip():raise TwelveDataError('Twelve Data has no configured backend API key. Run python scripts/configure-fx.py, then restart the server.')
            if metered:
                if self.blocked_until and self.blocked_until>now:
                    wait=int((self.blocked_until-now).total_seconds())+1
                    raise TwelveDataError(f'Twelve Data credit budget is paused. Retry in {wait} seconds.',429,wait)
                if self.used>=self.limit or (self.daily_limit and self.daily_used>=self.daily_limit):
                    self.blocked_until=(now.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)) if self.daily_limit and self.daily_used>=self.daily_limit else now.replace(second=0,microsecond=0)+timedelta(minutes=1)
                    wait=int((self.blocked_until-now).total_seconds())+1
                    raise TwelveDataError(f'Twelve Data credit budget reached. Retry in {wait} seconds.',429,wait)
                # Reserve a credit even if the request fails; do not flood the
                # provider with failing retries or assume failed calls are free.
                self.used+=1;self.daily_used+=1
            try:
                if self.transport:result=self.transport(path,params or {});payload,headers=result if isinstance(result,tuple) else (result,{})
                else:
                    url='https://api.twelvedata.com'+path+('?' + urllib.parse.urlencode(params) if params else '')
                    req=urllib.request.Request(url,headers={'Authorization':'apikey '+key.strip(),'Accept':'application/json'})
                    with urllib.request.urlopen(req,timeout=20) as response:payload=json.load(response);headers=dict(response.headers)
            except urllib.error.HTTPError as exc:
                raise self.provider_error(exc.code) from None
            except (OSError,ValueError) as exc:
                if isinstance(exc,TwelveDataError):raise
                raise TwelveDataError('Twelve Data could not be reached or returned invalid data. Check this computer\'s network access. No demo quotes were substituted.') from None
            normalized={k.lower():v for k,v in headers.items()}
            try:
                used=int(normalized.get('api-credits-used',self.used))
                if metered:self.used=max(self.used,used)
            except (ValueError,TypeError):pass
            if not isinstance(payload,dict):raise TwelveDataError('Twelve Data returned an invalid response.')
            if payload.get('status')=='error':
                try:code=int(payload.get('code',503))
                except (TypeError,ValueError):code=503
                # Inspect only for quota classification; never expose provider
                # messages that could echo an API key or authenticated URL.
                raise self.provider_error(code,daily='daily' in str(payload.get('message','')).lower())
            return payload
    def provider_error(self,code,daily=False):
        messages={401:'Twelve Data rejected the backend API key.',403:'This Twelve Data endpoint or currency pair is not available on the account plan.',404:'Twelve Data has no data for this currency pair.',400:'Twelve Data could not resolve this currency pair or request.',429:'Twelve Data quota is exhausted. Wait for the credit reset before retrying.'}
        retry=0
        if code==429:
            now=self.clock();self.blocked_until=(now.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)) if daily else now.replace(second=0,microsecond=0)+timedelta(minutes=1)
            retry=int((self.blocked_until-now).total_seconds())+1
        return TwelveDataError(messages.get(code,'Twelve Data could not complete the request.'),429 if code==429 else 503,retry)
    def initialize(self):
        with self.lock:
            if self.initialized:return
            usage=self.request('/api_usage')
            try:
                limit=int(usage['plan_limit']);daily=usage.get('plan_daily_limit')
                if limit<1:raise ValueError()
                self.limit=limit;self.daily_limit=int(daily) if daily is not None else None
                self.used=max(self.used,int(usage.get('current_usage',self.used)))
                self.daily_used=max(self.daily_used,int(usage.get('daily_usage',self.daily_used)))
            except (KeyError,ValueError,TypeError):raise TwelveDataError('Twelve Data returned invalid credit limits.') from None
            self.plan=str(usage.get('plan_category','unknown'));self.usage_observed_at=self.clock().isoformat();self.initialized=True
    def cached(self,key,ttl,loader):
        with self.lock:
            old=self.cache.get(key)
            if old and time.monotonic()-old[0]<ttl:return deepcopy(old[1])
            value=loader()
            if len(self.cache)>=72:self.cache.pop(next(iter(self.cache)))
            self.cache[key]=(time.monotonic(),value);return deepcopy(value)
    def search(self,query='',offset=0,limit=25):
        def load():
            payload=self.request('/forex_pairs',metered=False)
            if not isinstance(payload.get('data'),list):raise TwelveDataError('Twelve Data returned an invalid FX directory.')
            rows=[]
            for item in payload['data']:
                symbol=pair_from(item['symbol'])
                rows.append({'symbol':symbol,'base':symbol[:3],'quote':symbol[4:],'name':f"{item.get('currency_base',symbol[:3])} / {item.get('currency_quote',symbol[4:])}",'group':item.get('currency_group','')})
            return sorted(rows,key=lambda r:r['symbol'])
        directory=self.cached('pairs',86400,load);tokens=query.casefold().split();compact=re.sub(r'[^A-Za-z]','',query).upper()
        rows=[r for r in directory if (compact and compact in r['symbol'].replace('/','')) or all(t in f"{r['symbol']} {r['name']} {r['group']}".casefold() for t in tokens)]
        rows.sort(key=lambda r:(r['symbol'].replace('/','')!=compact,not r['symbol'].replace('/','').startswith(compact),r['symbol']))
        return {'source':'Twelve Data FX directory','results':rows[offset:offset+limit],'total':len(rows),'universe_count':len(directory),'offset':offset,'limit':limit}
    def quote(self,pair):
        def load():
            self.initialize();payload=self.request('/quote',{'symbol':pair,'timezone':'UTC'})
            if payload.get('symbol')!=pair:raise TwelveDataError('Twelve Data quote identity did not match the requested currency pair.')
            price=finite_number(payload.get('close'),True)
            if price is None:raise TwelveDataError('Twelve Data returned no valid exchange rate.')
            now=self.clock();epoch=finite_number(payload.get('last_quote_at'),True) or finite_number(payload.get('timestamp'),True)
            try:stamp=datetime.fromtimestamp(epoch,timezone.utc) if epoch else None
            except (ValueError,OverflowError,OSError):stamp=None
            if stamp and stamp>now+timedelta(minutes=2):raise TwelveDataError('Twelve Data returned a quote timestamp in the future.')
            market_open=payload.get('is_market_open') if isinstance(payload.get('is_market_open'),bool) else None
            previous=finite_number(payload.get('previous_close'),True);change=finite_number(payload.get('percent_change'))
            if change is None and previous:change=(price/previous-1)*100
            return {'symbol':pair,'base':pair[:3],'quote':pair[4:],'name':payload.get('name',pair),'price':price,'previous_close':previous,'change_pct':change,
                'open':finite_number(payload.get('open'),True),'high':finite_number(payload.get('high'),True),'low':finite_number(payload.get('low'),True),
                'quote_time':stamp.isoformat() if stamp else None,'timestamp_kind':'last_quote_at' if payload.get('last_quote_at') else 'provider timestamp',
                'provider_session_date':payload.get('datetime'),'market_open':market_open,'fetched_at':now.isoformat(),'error':None}
        row=self.cached(('quote',pair),300,load)
        age=max(0,(self.clock()-datetime.fromisoformat(row['quote_time'])).total_seconds()) if row['quote_time'] else None
        row.update(age_seconds=age,stale=age is None or (row['market_open'] is not False and age>600))
        return row
    def market(self,pairs):
        rows=[]
        for pair in pairs:
            try:rows.append(self.quote(pair))
            except TwelveDataError as exc:rows.append({'symbol':pair,'base':pair[:3],'quote':pair[4:],'name':pair,'price':None,'quote_time':None,'change_pct':None,'market_open':None,'stale':True,'error':str(exc),'retry_after_seconds':exc.retry_after})
        return {'source':'Twelve Data','rows':rows,'checked_at':self.clock().isoformat(),'quota':self.quota(),'refresh_seconds':300,
            'coverage':'Indicative FX reference rates from Twelve Data; not executable bid/ask quotes. Polling with a five-minute cache, not a tick stream. The provider market-state flag is displayed as reported.',
            'portfolio_note':'Live FX is independent of the simulated portfolio, research studio and other demo sources.'}
    def history(self,pair,interval='1day'):
        if interval not in ['1day','1h','5min']:raise ValueError('Supported FX intervals: 1day, 1h, 5min.')
        def load():
            self.initialize();payload=self.request('/time_series',{'symbol':pair,'interval':interval,'outputsize':300 if interval=='1day' else 168 if interval=='1h' else 288,'timezone':'UTC','order':'asc'})
            meta=payload.get('meta',{})
            if meta.get('symbol')!=pair or meta.get('interval')!=interval:raise TwelveDataError('Twelve Data history identity or interval did not match the request.')
            if not isinstance(payload.get('values'),list) or not payload['values']:raise TwelveDataError('No FX history is available for this pair and interval.')
            now=self.clock();session_zone=meta.get('exchange_timezone');bars={};daily_cutoff=now.date()-timedelta(days=1)
            if session_zone:
                try:daily_cutoff=now.astimezone(ZoneInfo(session_zone)).date()
                except (ZoneInfoNotFoundError,ValueError):session_zone=None
            for item in payload['values']:
                prices={k:finite_number(item.get(k),True) for k in ['open','high','low','close']}
                if any(v is None for v in prices.values()) or prices['low']>min(prices['open'],prices['close']) or prices['high']<max(prices['open'],prices['close']):raise TwelveDataError('Twelve Data returned invalid FX OHLC prices.')
                label=item.get('datetime','')
                try:
                    if interval=='1day':
                        day=datetime.strptime(label,'%Y-%m-%d').date();complete=day<daily_cutoff
                        if day>now.date()+timedelta(days=1):raise ValueError()
                        stamp=label
                    else:
                        at=datetime.strptime(label,'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                        if at>now+timedelta(minutes=2):raise ValueError()
                        complete=at+timedelta(minutes=60 if interval=='1h' else 5)<=now;stamp=at.isoformat()
                except (ValueError,TypeError):raise TwelveDataError('Twelve Data returned an invalid FX candle timestamp.') from None
                bars[label]={'date':label,'time':stamp,'complete':complete,**prices}
            ordered=[bars[label] for label in sorted(bars)];completed=[b for b in ordered if b['complete']]
            return {'source':'Twelve Data','symbol':pair,'interval':interval,'bars':ordered,'fetched_at':now.isoformat(),'session_timezone':session_zone,
                'analytics':fx_metrics(ordered) if interval=='1day' else None,'analytics_as_of':completed[-1]['date'] if completed else None,
                'methodology':'Daily date labels follow the provider session calendar; the timezone override applies to intraday candles only. Analytics exclude developing bars. When the daily session timezone is unspecified, both today and yesterday UTC are excluded conservatively. Volatility uses 20 completed daily returns and a 252-observation annualization convention. Price trends exclude carry and trading costs.'}
        return self.cached(('history',pair,interval),21600 if interval=='1day' else 900 if interval=='1h' else 300,load)
