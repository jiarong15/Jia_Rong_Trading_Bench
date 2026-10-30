"""Yahoo FX snapshots and candles through the optional yfinance client.

No fabricated ticks, inverse-pair guessing, API keys or cross-provider bar merges.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from threading import RLock
import time
import warnings
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .twelvedata import pair_from, finite_number, fx_metrics

ALIASES={'USD/JPY':'JPY=X','USD/SGD':'SGD=X','USD/CHF':'CHF=X','USD/CAD':'CAD=X','USD/CNY':'CNY=X','USD/HKD':'HKD=X','USD/INR':'INR=X','USD/ZAR':'ZAR=X'}

class YahooFXError(ValueError):
    def __init__(self,message,scope='pair'):
        super().__init__(message);self.scope=scope

def yahoo_symbol(pair):
    pair=pair_from(pair)
    return ALIASES.get(pair,pair.replace('/','')+'=X')

def weekend_closed(now):
    try:ny=now.astimezone(ZoneInfo('America/New_York'))
    except ZoneInfoNotFoundError:return None
    return (ny.weekday()==4 and ny.hour>=17) or ny.weekday()==5 or (ny.weekday()==6 and ny.hour<17)

class YahooFXService:
    name='Yahoo Finance via yfinance'
    def __init__(self,cache_dir,enabled=True,transport=None,clock=None):
        self.cache_dir=Path(cache_dir);self.enabled=enabled;self.transport=transport;self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.lock=RLock();self.cache={};self.configured=False
    def fetch(self,pair,interval,period):
        ticker=yahoo_symbol(pair)
        if not self.enabled and self.transport is None:raise YahooFXError('Yahoo FX is disabled for this isolated instance.','provider')
        try:
            if self.transport:return self.transport(ticker,interval,period)
            try:import yfinance as yf
            except ImportError:raise YahooFXError('yfinance is not installed in the backend runtime. Run bootstrap.py to install it.','provider') from None
            if not self.configured:
                self.cache_dir.mkdir(parents=True,exist_ok=True);yf.set_tz_cache_location(str(self.cache_dir))
                logging.getLogger('yfinance').setLevel(logging.CRITICAL);self.configured=True
            instrument=yf.Ticker(ticker)
            with warnings.catch_warnings():
                warnings.simplefilter('ignore',DeprecationWarning)
                frame=instrument.history(period=period,interval=interval,auto_adjust=False,actions=False,repair=False,keepna=False,timeout=10,raise_errors=True)
            if frame.empty:raise YahooFXError('Yahoo returned no FX candles for this pair and interval.')
            metadata=instrument.get_history_metadata()
            # Do not materialize lazy tradingPeriods or expose cookies/crumbs.
            keys=['symbol','currency','instrumentType','exchangeTimezoneName','regularMarketPrice','regularMarketTime','previousClose',
                'regularMarketPreviousClose','chartPreviousClose','regularMarketOpen','regularMarketDayHigh','regularMarketDayLow','marketState','shortName']
            meta={k:metadata.get(k) for k in keys}
            bars=[]
            for at,row in frame.iterrows():
                bars.append({'time':at.isoformat(),'open':float(row['Open']),'high':float(row['High']),'low':float(row['Low']),'close':float(row['Close'])})
            return {'meta':meta,'bars':bars}
        except YahooFXError:raise
        except Exception as exc:
            kind=type(exc).__name__
            if kind=='YFRateLimitError':raise YahooFXError('Yahoo Finance rate-limited the request. The provider will be retried after a cooldown.','provider') from None
            if kind in ['YFPricesMissingError','YFTzMissingError','YFInvalidPeriodError']:raise YahooFXError('Yahoo has no usable FX data for this pair and interval.') from None
            raise YahooFXError('Yahoo Finance could not be reached or returned an unusable response.','provider') from None
    def cached(self,key,ttl,loader):
        with self.lock:
            record=self.cache.get(key)
            if record and time.monotonic()-record[0]<ttl:return deepcopy(record[1])
            result=loader()
            if len(self.cache)>=72:self.cache.pop(next(iter(self.cache)))
            self.cache[key]=(time.monotonic(),result);return deepcopy(result)
    def series(self,pair,interval):
        native,period,ttl={'1day':('1d','2y',21600),'1h':('1h','1mo',900),'5min':('5m','5d',300)}[interval]
        def load():
            data=self.fetch(pair,native,period);meta=data.get('meta',{});symbol=yahoo_symbol(pair)
            allowed={symbol,pair.replace('/','')+'=X'}
            if meta.get('symbol') not in allowed or meta.get('currency')!=pair[4:] or meta.get('instrumentType')!='CURRENCY':
                raise YahooFXError('Yahoo FX symbol, quote currency or instrument type did not match the requested pair.')
            if not isinstance(data.get('bars'),list) or not data['bars']:raise YahooFXError('Yahoo returned no usable FX history.')
            now=self.clock();bars={}
            # The UI requests at most 300 daily observations. Validate the
            # requested window rather than unrelated older years.
            requested=data['bars'][-300:] if interval=='1day' else data['bars']
            for item in requested:
                prices={k:finite_number(item.get(k),True) for k in ['open','high','low','close']}
                if any(v is None for v in prices.values()) or prices['low']>min(prices['open'],prices['close']) or prices['high']<max(prices['open'],prices['close']):
                    raise YahooFXError('Yahoo returned invalid FX candle prices.')
                try:
                    at=datetime.fromisoformat(item['time'])
                    if at.tzinfo is None:raise ValueError()
                    if at>now+timedelta(minutes=2):raise ValueError()
                except (KeyError,ValueError,TypeError):raise YahooFXError('Yahoo returned an invalid or future FX candle timestamp.') from None
                if interval=='1day':
                    label=at.date().isoformat()
                    try:today=now.astimezone(ZoneInfo(meta['exchangeTimezoneName'])).date();complete=at.date()<today
                    except (KeyError,TypeError,ZoneInfoNotFoundError):complete=at.date()<now.date()-timedelta(days=1)
                else:label=at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S');complete=at+timedelta(minutes=60 if interval=='1h' else 5)<=now
                row={'date':label,'time':at.astimezone(timezone.utc).isoformat(),'complete':complete,**prices}
                if label in bars and bars[label]!=row:raise YahooFXError('Yahoo returned conflicting candles for the same FX timestamp.')
                bars[label]=row
            return {'meta':meta,'bars':[bars[label] for label in sorted(bars)],'fetched_at':now.isoformat()}
        return self.cached(('series',pair,interval),ttl,load)
    def quote(self,pair):
        data=self.series(pair,'5min');meta=data['meta'];price=finite_number(meta.get('regularMarketPrice'),True);raw_time=meta.get('regularMarketTime')
        # get_history_metadata() converts Yahoo epoch seconds to a pandas
        # Timestamp. Accept its actual aware time as well as unformatted epochs.
        try:
            if isinstance(raw_time,datetime):
                at=raw_time.to_pydatetime() if hasattr(raw_time,'to_pydatetime') else raw_time
                if at.tzinfo is None:raise ValueError()
                at=at.astimezone(timezone.utc)
            else:
                epoch=finite_number(raw_time,True)
                if epoch is None:raise ValueError()
                at=datetime.fromtimestamp(epoch,timezone.utc)
        except (ValueError,OverflowError,OSError):raise YahooFXError('Yahoo did not provide a valid timestamped FX quote; historical candles were not substituted for a live quote.') from None
        if price is None:raise YahooFXError('Yahoo did not provide a valid FX reference price.')
        now=self.clock()
        if at>now+timedelta(minutes=2):raise YahooFXError('Yahoo returned an FX quote timestamp in the future.')
        previous=finite_number(meta.get('previousClose'),True) or finite_number(meta.get('regularMarketPreviousClose'),True)
        # chartPreviousClose refers to the start of the requested five-day
        # range, not necessarily yesterday; never label it a daily change.
        state=meta.get('marketState');market_open=False if state in ['CLOSED','POSTPOST'] else True if state=='REGULAR' else None
        basis='Yahoo marketState' if market_open is not None else 'Unavailable'
        if market_open is None and weekend_closed(now):market_open=False;basis='Weekend convention: Friday 17:00 to Sunday 17:00 America/New_York; not a venue calendar'
        age=max(0,(now-at).total_seconds())
        return {'symbol':pair,'base':pair[:3],'quote':pair[4:],'name':meta.get('shortName') or pair,'price':price,'previous_close':previous,
            'change_pct':(price/previous-1)*100 if previous else None,'open':finite_number(meta.get('regularMarketOpen'),True),
            'high':finite_number(meta.get('regularMarketDayHigh'),True),'low':finite_number(meta.get('regularMarketDayLow'),True),
            'quote_time':at.isoformat(),'timestamp_kind':'Yahoo regularMarketTime','market_open':market_open,'market_state_basis':basis,
            'fetched_at':data['fetched_at'],'source':self.name,'provider':'yahoo','yahoo_symbol':yahoo_symbol(pair),'age_seconds':age,
            'stale':market_open is not False and age>900,'stale_after_seconds':900,'error':None,
            'source_note':'Unofficial Yahoo Finance snapshot through yfinance; may be delayed. Not an executable FX bid/ask quote.'}
    def history(self,pair,interval='1day'):
        if interval not in ['1day','1h','5min']:raise ValueError('Supported FX intervals: 1day, 1h, 5min.')
        data=self.series(pair,interval);bars=data['bars'][-300:] if interval=='1day' else data['bars'][-168:] if interval=='1h' else data['bars'][-288:]
        completed=[b for b in bars if b['complete']]
        return {'source':self.name,'provider':'yahoo','yahoo_symbol':yahoo_symbol(pair),'symbol':pair,'interval':interval,'bars':bars,
            'fetched_at':data['fetched_at'],'session_timezone':data['meta'].get('exchangeTimezoneName'),'analytics':fx_metrics(bars) if interval=='1day' else None,
            'analytics_as_of':completed[-1]['date'] if completed else None,
            'methodology':'Yahoo candles through yfinance; may be delayed. Daily dates follow the reported Yahoo session timezone; intraday times are UTC. Analytics use only completed candles, with 20 daily returns and a 252-observation volatility convention. No bars are spliced across providers, and carry/costs are excluded.'}
