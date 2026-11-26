"""Source-dated CFETS USD/CNY ATM volatility and forward-pricing parameters."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
from threading import Event, RLock, Thread
from time import monotonic
from urllib.request import HTTPSHandler, Request, build_opener

from .fx_exact_feed import NoRedirect, timestamp
from .fx_volatility import tls_context

SOURCE_URL = 'https://www.chinamoney.org.cn/english/bmkycvivc/'
PRICING_URL = 'https://www.chinamoney.org.cn/english/bmkycvdpp/'
# The official .com.cn host serves the same page and supports modern verified TLS.
ROOT = 'https://www.chinamoney.com.cn/ags/ms/cm-u-bk-fx/'
URLS = {'volatility': ROOT+'FoivltltyCurv?lang=EN&ccyPair=USD.CNY&volatilitySurface=0',
        'pricing': ROOT+'FodpParam?lang=EN&spotPriceType=28&rmbRateType=25&volatilitySurface=ATM'}
SGT = timezone(timedelta(hours=8))
POLL = 60
TENORS = {'1D':1/365, '1W':7/365, '2W':14/365, '3W':21/365, '1M':1/12,
          '2M':2/12, '3M':.25, '6M':.5, '9M':.75, '1Y':1., '18M':1.5, '2Y':2., '3Y':3.}
FIXINGS = ('10:00', '11:00', '14:00', '15:00', '16:00')


def public_json(url):
    if url not in URLS.values():
        raise ValueError('Unexpected CFETS endpoint.')
    request = Request(url, data=b'', headers={'User-Agent':'JRTradingWorkbench/1.0',
        'Accept':'application/json', 'Cache-Control':'no-cache', 'Referer':SOURCE_URL})
    opener = build_opener(NoRedirect(), HTTPSHandler(context=tls_context()))
    deadline = monotonic()+20
    body = bytearray()
    with opener.open(request, timeout=12) as response:
        while True:
            if monotonic()>deadline:
                raise TimeoutError('CFETS download exceeded its time budget.')
            chunk = response.read1(65536)
            if not chunk:
                break
            body.extend(chunk)
            if len(body)>500_000:
                raise ValueError('CFETS response exceeds size limit.')
    return bytes(body)


def document(body):
    if isinstance(body,(str,bytes)):
        if len(body)>500_000:
            raise ValueError('CFETS response exceeds size limit.')
        body=json.loads(body)
    if not isinstance(body,dict) or not isinstance(body.get('data'),dict) or body['data'].get('error'):
        raise ValueError('CFETS metadata is unavailable.')
    if not isinstance(body.get('records'),list) or len(body['records'])!=len(TENORS):
        raise ValueError('Incomplete CFETS curve.')
    return body


def number(value, lower=0, upper=300):
    if isinstance(value,bool) or not isinstance(value,(str,int,float)):
        raise ValueError('Invalid CFETS number.')
    result=float(value)
    if not math.isfinite(result) or not lower<result<=upper:
        raise ValueError('Invalid CFETS units or level.')
    return result


def source_date(value, now):
    result=date.fromisoformat(value)
    if result>now.astimezone(SGT).date():
        raise ValueError('Future CFETS publication date.')
    return result


def parse_volatility(body, now):
    data=document(body); meta=data['data']
    if meta['ccyPair']!='USD.CNY' or meta['volatilitySurface']!='0' or meta['ccyTime'] not in FIXINGS:
        raise ValueError('Expected CFETS onshore USD/CNY ATM volatility.')
    day=source_date(meta['ccyDate'],now)
    published=datetime.fromisoformat(day.isoformat()+'T'+meta['ccyTime']).replace(tzinfo=SGT)
    timestamp(published.isoformat(),now)
    nodes={}
    for row in data['records']:
        tenor=row['tenor']
        if tenor not in TENORS or tenor in nodes or row['ccyPair']!='USD.CNY' or row['volatilityTypeEN']!='ATM':
            raise ValueError('Wrong, repeated or unsupported CFETS volatility node.')
        mid=number(row['midVolatilityStr']); bid=number(row['bidVolatilityStr']); ask=number(row['askVolatilityStr'])
        if not bid<=mid<=ask:
            raise ValueError('Crossed CFETS volatility quote.')
        nodes[tenor]={'tenor':tenor,'years':TENORS[tenor],'volatility_pct':mid,'bid_pct':bid,'ask_pct':ask}
    if set(nodes)!=set(TENORS):
        raise ValueError('Missing CFETS volatility tenor.')
    return {'published_at':published.astimezone(timezone.utc).isoformat(),
            'nodes':[nodes[tenor] for tenor in TENORS]}


def parse_pricing(body, now):
    data=document(body); meta=data['data']
    if str(meta['spotPriceType'])!='28' or str(meta['rmbRateType'])!='25' or meta['volatilitySurface']!='ATM':
        raise ValueError('Expected CFETS OTC mean spot / Shibor3M / ATM pricing parameters.')
    day=source_date(meta['showDateCN'],now)
    rows={}; spot=None; previous=day
    for row in data['records']:
        tenor='18M' if row['tenor']=='1.5Y' else row['tenor']
        if tenor not in TENORS or tenor in rows:
            raise ValueError('Wrong or repeated CFETS pricing tenor.')
        expiry=date.fromisoformat(row['expiryDateCn'])
        days=number(row['expiryDays'],upper=1200)
        if (expiry-day).days!=days or not .4*TENORS[tenor]*365<=days<=TENORS[tenor]*365+15:
            raise ValueError('Inconsistent CFETS option expiry.')
        level=number(row['spotPrice'],upper=100)
        if spot is not None and spot!=level:
            raise ValueError('Inconsistent CFETS spot reference.')
        spot=level
        points=number(row['swapRate'],lower=-100_000,upper=100_000)
        forward=spot+points*.0001
        if not 0<forward or abs(math.log(forward/spot))>.5:
            raise ValueError('Invalid CFETS forward-point conversion.')
        number(row['volatility'])
        rows[tenor]={'tenor':tenor,'expiry':expiry.isoformat(),'forward':forward}
    if set(rows)!=set(TENORS):
        raise ValueError('Missing CFETS pricing tenor.')
    nodes=[rows[tenor] for tenor in TENORS]
    for node in nodes:
        current=date.fromisoformat(node['expiry'])
        if current<=previous:
            raise ValueError('CFETS forward maturities are not increasing.')
        previous=current
    return {'date':day.isoformat(),'spot':spot,'nodes':nodes}


def expected_fixing(now):
    local=now.astimezone(SGT)
    day=local.date()
    while True:
        if day.weekday()<5:
            slots=[datetime.fromisoformat(day.isoformat()+'T'+time).replace(tzinfo=SGT) for time in FIXINGS]
            ready=[slot for slot in slots if slot<=local-timedelta(minutes=10)]
            if ready:
                return ready[-1].astimezone(timezone.utc)
        day-=timedelta(days=1)


class CFETSFXSource:
    def __init__(self,path,enabled=True,transport=None,clock=None):
        self.path=Path(path); self.enabled=enabled; self.transport=transport or public_json
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.lock=RLock(); self.stop=Event(); self.thread=None; self.inflight=False; self.attempt=None
        self.values={}; self.raw={}; self.checks={}; self.errors={}
        try:
            saved=json.loads(self.path.read_text(encoding='utf-8'))
            if saved['schema']!=1:
                raise ValueError('Unknown CFETS cache schema.')
            for kind,parse in [('volatility',parse_volatility),('pricing',parse_pricing)]:
                if kind in saved['documents']:
                    parsed=parse(saved['documents'][kind],self.clock())
                    checked=timestamp(saved['checks'][kind],self.clock()).isoformat()
                    self.values[kind]=parsed; self.raw[kind]=saved['documents'][kind]; self.checks[kind]=checked
        except (OSError,ValueError,KeyError,TypeError,AttributeError):
            self.values={}; self.raw={}; self.checks={}

    def refresh(self,force=False):
        now=self.clock()
        with self.lock:
            if not self.enabled or self.inflight or not force and self.attempt and (now-self.attempt).total_seconds()<POLL:
                return
            self.attempt=now; self.inflight=True
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                jobs={kind:pool.submit(self.transport,url) for kind,url in URLS.items()}
                for kind,parse in [('volatility',parse_volatility),('pricing',parse_pricing)]:
                    try:
                        raw=document(jobs[kind].result()); parsed=parse(raw,now)
                        key='published_at' if kind=='volatility' else 'date'
                        with self.lock:
                            if kind in self.values and parsed[key]<self.values[kind][key]:
                                raise ValueError('Regressed CFETS publication.')
                            self.values[kind]=parsed; self.raw[kind]=deepcopy(raw)
                            self.checks[kind]=self.clock().isoformat(); self.errors.pop(kind,None)
                    except Exception:
                        with self.lock:
                            self.errors[kind]=f'CFETS {kind} check failed; retaining the last verified source-dated inputs and retrying automatically.'
            with self.lock:
                try:
                    self.path.parent.mkdir(parents=True,exist_ok=True)
                    temporary=self.path.with_suffix('.tmp')
                    temporary.write_text(json.dumps({'schema':1,'documents':self.raw,'checks':self.checks}),encoding='utf-8')
                    temporary.replace(self.path); self.errors.pop('cache',None)
                except OSError:
                    self.errors['cache']='CFETS inputs could not be saved; current verified inputs remain available.'
        finally:
            with self.lock:self.inflight=False

    def snapshot(self,now=None):
        now=now or self.clock()
        with self.lock:
            result=deepcopy({'volatility':self.values.get('volatility') if self.enabled else None,
                'pricing':self.values.get('pricing') if self.enabled else None,'checks':self.checks,'errors':self.errors})
        vol=result['volatility']; pricing=result['pricing']; warnings=list(result['errors'].values())
        result['usable']=bool(vol and pricing)
        if vol and timestamp(vol['published_at'],now)<expected_fixing(now):
            warnings.append('Awaiting a newer CFETS fixing; the displayed curve retains its publication time. CFETS holidays may differ from weekdays.')
        if vol and (now-timestamp(vol['published_at'],now)).total_seconds()>7*86400 or pricing and (now.astimezone(SGT).date()-date.fromisoformat(pricing['date'])).days>7:
            result['usable']=False; warnings.append('CFETS market inputs are over seven days old; Greek estimates are unavailable.')
        if not self.enabled:
            warnings.append('The CFETS USD/CNY source is disabled.')
        elif any((now-timestamp(checked,now)).total_seconds()>2*POLL for checked in result['checks'].values()):
            warnings.append('CFETS has not been successfully checked in over two minutes; source publication dates are unchanged.')
        result['warnings']=warnings
        return result

    def _run(self):
        while not self.stop.is_set():
            started=monotonic(); self.refresh()
            if self.stop.wait(max(.1,POLL-(monotonic()-started))):
                break

    def start(self):
        with self.lock:
            if not self.enabled or self.thread and self.thread.is_alive():return
            self.stop.clear(); self.thread=Thread(target=self._run,name='cfets-usdcny',daemon=True); self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=4)
