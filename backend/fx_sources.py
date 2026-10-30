"""Choose a whole FX quote/history response, with visible fallback provenance."""
from datetime import datetime, timedelta, timezone
from threading import RLock
from .twelvedata import TwelveDataError
from .yahoo_fx import YahooFXError

NAMES={'yahoo':'Yahoo Finance via yfinance','twelvedata':'Twelve Data'}

class FXSources:
    def __init__(self,yahoo,twelvedata,primary='yahoo',clock=None):
        if primary not in NAMES:raise ValueError('FX_PRIMARY_PROVIDER must be yahoo or twelvedata.')
        self.yahoo=yahoo;self.twelvedata=twelvedata;self.primary=primary;self.clock=clock or (lambda:datetime.now(timezone.utc));self.lock=RLock()
        self.health={p:{'state':'untried','last_success':None,'last_error':None,'retry_at':None} for p in NAMES};self.pair_failures={}
    def providers(self):
        with self.lock:
            now=self.clock();result=[]
            for key,name in NAMES.items():
                state=self.health[key].copy();retry=state.pop('retry_at')
                seconds=max(0,int((retry-now).total_seconds())+1) if retry else 0
                if not seconds and state['state']=='cooldown':state['state']='retry available'
                result.append({'id':key,'name':name,'role':'primary' if key==self.primary else 'backup','retry_after_seconds':seconds,**state})
            return result
    def quota(self):return {**self.twelvedata.quota(),'initialized':self.twelvedata.initialized,'provider':'Twelve Data'}
    def search(self,*args,**kwargs):return self.twelvedata.search(*args,**kwargs)
    def attempt(self,provider,pair,kind,interval=None):
        now=self.clock();failure_key=(provider,pair,kind,interval)
        with self.lock:
            retry=self.health[provider]['retry_at']
            if retry and retry>now:raise TwelveDataError(f'{NAMES[provider]} is cooling down after a failed request.',503,int((retry-now).total_seconds())+1)
            previous=self.pair_failures.get(failure_key)
            if previous and previous[0]>now:raise TwelveDataError(previous[1],503,int((previous[0]-now).total_seconds())+1)
        service=self.yahoo if provider=='yahoo' else self.twelvedata
        try:
            result=service.quote(pair) if kind=='quote' else service.history(pair,interval)
        except (YahooFXError,TwelveDataError) as exc:
            message=str(exc)
            with self.lock:
                self.health[provider].update(last_error=message)
                if provider=='yahoo':
                    if getattr(exc,'scope','pair')=='provider':self.health[provider].update(state='cooldown',retry_at=now+timedelta(minutes=5))
                    else:
                        # Invalid historical closes rarely change within a few
                        # minutes. Match the daily cache horizon before retrying.
                        pause=21600 if kind=='history' and interval=='1day' and ('candle prices' in message or 'closing prices' in message) else 300
                        self.pair_failures[failure_key]=(now+timedelta(seconds=pause),message)
                        if len(self.pair_failures)>100:self.pair_failures.pop(next(iter(self.pair_failures)))
                        self.health[provider]['state']='some requests unavailable'
                elif getattr(exc,'retry_after',0):self.health[provider].update(state='cooldown',retry_at=now+timedelta(seconds=exc.retry_after))
                else:self.health[provider]['state']='some requests unavailable'
            raise TwelveDataError(message,getattr(exc,'code',503),getattr(exc,'retry_after',0)) from None
        with self.lock:
            self.health[provider].update(state='ready',last_success=self.clock().isoformat(),last_error=None,retry_at=None)
            self.pair_failures.pop(failure_key,None)
        return {**result,'source':NAMES[provider],'provider':provider}
    def quote(self,pair):
        order=[self.primary,'twelvedata' if self.primary=='yahoo' else 'yahoo'];failures=[];stale=[]
        for provider in order:
            try:
                row=self.attempt(provider,pair,'quote')
                if not row.get('quote_time') or (row.get('stale') and row.get('market_open') is not False):
                    stale.append(row);failures.append(f'{NAMES[provider]} returned a stale or undated quote.');continue
                return {**row,'fallback':provider!=self.primary,'fallback_reason':' '.join(failures) if failures else None}
            except TwelveDataError as exc:failures.append(str(exc))
        if stale:
            row=max(stale,key=lambda r:r.get('quote_time') or '')
            return {**row,'stale':True,'fallback':row['provider']!=self.primary,'fallback_reason':' '.join(failures),'source_note':'No fresh source quote is available. This is the timestamped last available source snapshot.'}
        return {'symbol':pair,'base':pair[:3],'quote':pair[4:],'name':pair,'price':None,'quote_time':None,'change_pct':None,'market_open':None,
            'stale':True,'error':' '.join(failures),'source':None,'provider':None,'fallback':False,'fallback_reason':None}
    def market(self,pairs):
        rows=[self.quote(pair) for pair in pairs];sources=list(dict.fromkeys(row['source'] for row in rows if row.get('source')))
        return {'source':sources[0] if len(sources)==1 else 'Mixed FX sources' if sources else 'FX providers unavailable','primary_provider':self.primary,
            'rows':rows,'providers':self.providers(),'checked_at':self.clock().isoformat(),'quota':self.quota(),'refresh_seconds':300,
            'coverage':'Indicative FX reference snapshots, not executable bid/ask quotes. Five-minute polling/cache; Yahoo data may be delayed. Each row identifies its provider and any fallback. Market state is reported when available; Yahoo weekend closures may use an explicitly labelled New York weekend convention.',
            'portfolio_note':'Live FX remains independent of the simulated portfolio, research studio and other demo sources.'}
    def history(self,pair,interval='1day'):
        errors=[]
        for provider in [self.primary,'twelvedata' if self.primary=='yahoo' else 'yahoo']:
            try:
                result=self.attempt(provider,pair,'history',interval)
                return {**result,'primary_provider':self.primary,'fallback':provider!=self.primary,'fallback_reason':' '.join(str(e) for e in errors) if errors else None,
                    'providers':self.providers(),'quota':self.quota()}
            except TwelveDataError as exc:errors.append(exc)
        retry=min((e.retry_after for e in errors if e.retry_after>0),default=0)
        code=429 if any(e.code==429 for e in errors) else 503
        raise TwelveDataError('FX history unavailable from both sources. '+' '.join(str(e) for e in errors),code,retry)
