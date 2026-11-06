"""Exact forward carry/ATM implied volatility with FRED weekly risk correlation."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import csv
import io
import json
import math
import os
from pathlib import Path
from statistics import correlation
from threading import RLock
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import ssl
from .fx_exact_feed import configured_feed, validate_feed
from .investing_forwards import InvestingForwardSource
from .fx_volatility import FXVolatilitySource

# FRED units: EUR/GBP/AUD/NZD series are USD per foreign currency; all
# remaining series are foreign currency per USD. Pair returns respect this.
PAIRS = [
    ('EUR', 'XM', 'EUR/USD', 'DEXUSEU', True),
    ('GBP', 'GB', 'GBP/USD', 'DEXUSUK', True),
    ('JPY', 'JP', 'USD/JPY', 'DEXJPUS', False),
    ('CHF', 'CH', 'USD/CHF', 'DEXSZUS', False),
    ('CAD', 'CA', 'USD/CAD', 'DEXCAUS', False),
    ('AUD', 'AU', 'AUD/USD', 'DEXUSAL', True),
    ('NZD', 'NZ', 'NZD/USD', 'DEXUSNZ', True),
    ('SEK', 'SE', 'USD/SEK', 'DEXSDUS', False),
    ('NOK', 'NO', 'USD/NOK', 'DEXNOUS', False),
]
IDS = ['SP500'] + [item[3] for item in PAIRS]
POLL = 60
HISTORY_POLL = 300


def fetch(url):
    context = ssl.create_default_context()
    certificate = os.environ.get('FX_ANALYTICS_CA_FILE', '').strip()
    if certificate:
        path = Path(certificate).expanduser()
        if not path.is_absolute(): path = Path(__file__).resolve().parents[1] / path
        context.load_verify_locations(cafile=path)
    request = Request(url, headers={'User-Agent': 'MeridianLocalDashboard/1.0', 'Cache-Control': 'no-cache'})
    with urlopen(request, timeout=18, context=context) as response:
        body = response.read(4_000_001)
    if len(body) > 4_000_000: raise ValueError('Source response exceeds the size limit.')
    return body.decode('utf-8-sig')


def market_series(body, identifier, start, today, authenticated):
    if authenticated:
        document = json.loads(body)
        rows = document['observations']
        if (not isinstance(rows, list) or document.get('count') != len(rows)
                or document.get('offset') != 0 or document.get('units') != 'lin'):
            raise ValueError('Incomplete or transformed FRED observations.')
        observations = [(item['date'], item['value']) for item in rows]
    else:
        reader = csv.DictReader(io.StringIO(body))
        if reader.fieldnames != ['observation_date', identifier]:
            raise ValueError('Unexpected FRED series schema.')
        observations = [(item['observation_date'], item[identifier]) for item in reader]
    result = {}
    for stamp, raw in observations:
        day = date.fromisoformat(stamp)
        if stamp in result or not start <= day <= today: raise ValueError('Invalid FRED observation date.')
        if not isinstance(raw, str): raise ValueError('Invalid FRED source value.')
        value = None if raw.strip() in ('', '.', 'NA') else float(raw)
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError('Invalid market price.')
        result[stamp] = value
    if sum(value is not None for value in result.values()) < 60:
        raise ValueError('Insufficient source price history.')
    return result


def weekly_correlation(prices, benchmark, inverse, today, weeks):
    # Use the same actual observation day for each FX/equity endpoint.
    # A holiday Friday uses the last shared day in that week. Missing weeks
    # remain missing; they are never bridged into a multi-week return.
    endpoints = {}
    for stamp in sorted(prices.keys() & benchmark.keys()):
        day = date.fromisoformat(stamp)
        friday = day + timedelta(days=4-day.weekday())
        if day.weekday() > 4 or friday >= today or prices[stamp] is None or benchmark[stamp] is None: continue
        endpoints[friday] = (stamp, prices[stamp], benchmark[stamp])
    last_week = today - timedelta(days=(today.weekday()-4) % 7)
    if last_week >= today: last_week -= timedelta(days=7)
    first_week = last_week - timedelta(weeks=weeks-1)
    returns = []
    for week in sorted(endpoints):
        previous = endpoints.get(week-timedelta(days=7))
        if previous is None or not first_week <= week <= last_week: continue
        current = endpoints[week]
        returns.append((current[0], math.log(current[1]/previous[1])*(-1 if inverse else 1), math.log(current[2]/previous[2])))
    value = None
    if len(returns) >= 20:
        try: value = correlation([row[1] for row in returns], [row[2] for row in returns])
        except (ValueError, ZeroDivisionError): pass
    return {'value': value, 'observations': len(returns), 'start': returns[0][0] if returns else None,
            'end': returns[-1][0] if returns else None}


def points(market, quotes, today, weeks, now):
    rows=[]
    for currency, _, pair, identifier, inverted in PAIRS:
        risk=weekly_correlation(market.get(identifier,{}),market.get('SP500',{}),inverted,today,weeks)
        quote=quotes.get(pair)
        row={'currency':currency,'pair':pair,'risk_correlation':risk['value'],
             'weekly_observations':risk['observations'],'correlation_start':risk['start'],'correlation_end':risk['end'],
             **{key:None for key in ('usd_carry_pct','pair_carry_pct','implied_volatility_pct','spot','forward_12m','spot_date','forward_date','quote_time','iv_time','iv_tenor','source')},'stale':False}
        if quote:
            row.update(quote)
            dates=[quote.get(key) for key in ('quote_time','iv_time','spot_time') if quote.get(key)]
            row['stale']=any((now-datetime.fromisoformat(value)).total_seconds()>300 for value in dates)
            if quote.get('spot_time_precision')=='day':row['stale']=True
        rows.append(row)
    return rows


class FXCarryRiskService:
    def __init__(self,path,enabled=True,transport=None,clock=None,api_key=None,quote_loader=None,iv_tenor=None,forward_source=None,volatility_source=None):
        self.path=Path(path);self.enabled=enabled;self.transport=transport or fetch
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.api_key=api_key if api_key is not None else os.environ.get('FRED_API_KEY') or os.environ.get('BONDS_FRED_API_KEY')
        self.iv_tenor=iv_tenor or os.environ.get('FX_CARRY_IV_TENOR','12M')
        if self.iv_tenor not in ('1W','1M','3M','6M','12M'):raise ValueError('Unsupported FX implied-volatility tenor.')
        self.quote_loader=quote_loader or (configured_feed() if enabled else None)
        self.forward_source=forward_source
        if self.forward_source is None and enabled and not self.quote_loader and os.environ.get('FX_INVESTING_FORWARDS_ENABLED','true').lower()=='true':
            self.forward_source=InvestingForwardSource(self.path.with_name('investing-fx-forwards.json'))
        self.volatility_source=volatility_source
        if self.volatility_source is None and enabled and not self.quote_loader:
            self.volatility_source=FXVolatilitySource(self.path.with_name('saxo-fx-volatility.json'),tenor=self.iv_tenor)
        self.lock=RLock();self.last_attempt=None;self.market={};self.quotes={};self.checks={};self.errors={}
        try:
            saved=json.loads(self.path.read_text(encoding='utf-8'))
            if saved['schema']!=2:raise ValueError('Invalid exact-metrics snapshot.')
            today=self.clock().date();market=saved['market']
            if market and set(market)!=set(IDS):raise ValueError('Missing saved history.')
            for values in market.values():
                if not isinstance(values,dict) or len(values)<60:raise ValueError('Incomplete saved history.')
                for stamp,value in values.items():
                    if date.fromisoformat(stamp)>today or value is not None and(type(value) not in (int,float) or not math.isfinite(value) or value<=0):raise ValueError('Invalid saved market price.')
            checks=saved['checks']
            for key,raw in checks.items():
                at=datetime.fromisoformat(raw)
                if key not in ('market','quotes') or at.tzinfo is None or at>self.clock():raise ValueError('Invalid source check time.')
            # Revalidate raw source quotes and recompute their carry on load.
            quotes=validate_feed(saved['feed'],self.clock(),self.iv_tenor) if saved.get('feed') else {}
            if market and not checks.get('market') or quotes and not checks.get('quotes'):raise ValueError('Undated saved data.')
            self.market=market;self.quotes=quotes;self.checks=checks;self.feed=saved.get('feed')
        except (OSError,ValueError,KeyError,TypeError,AttributeError):self.feed=None

    def load_market(self,today):
        start=today-timedelta(days=900)
        def one(identifier):
            parameters={'series_id':identifier,'api_key':self.api_key,'file_type':'json',
                        'observation_start':start.isoformat(),'observation_end':today.isoformat(),
                        'units':'lin','sort_order':'asc','limit':100000,'offset':0}
            if self.api_key:url='https://api.stlouisfed.org/fred/series/observations?'+urlencode(parameters)
            else:url='https://fred.stlouisfed.org/graph/fredgraph.csv?'+urlencode({'id':identifier,'cosd':start.isoformat(),'coed':today.isoformat()})
            return identifier,market_series(self.transport(url),identifier,start,today,bool(self.api_key))
        with ThreadPoolExecutor(max_workers=5) as pool:result=dict(pool.map(one,IDS))
        for identifier,values in result.items():
            before={stamp for stamp,value in self.market.get(identifier,{}).items() if value is not None and stamp>=start.isoformat()}
            after={stamp for stamp,value in values.items() if value is not None}
            if not before.issubset(after):raise ValueError('Regressed source history.')
        return result

    def load_quotes(self,now):
        body=self.quote_loader()
        document=json.loads(body) if isinstance(body,str) else body
        result=validate_feed(document,now,self.iv_tenor)
        if not self.quotes.keys()<=result.keys():raise ValueError('Feed lost previously covered currency pairs.')
        for pair,quote in self.quotes.items():
            if any(result[pair][key]<quote[key] for key in ('quote_time','iv_time')):raise ValueError('Regressed market quote time.')
        fields=('pair','spot','forward_12m','spot_date','forward_date','quote_time','iv_time','iv_tenor','iv_delta','implied_volatility_pct')
        saved={'provider':document['provider'],'as_of':document['as_of'],'volatility_unit':'percent','forward_format':'outright',
               'quotes':[{key:quote[key] for key in fields} for quote in result.values()]}
        return result,saved

    def view(self,weeks=52,refresh=False,saved_only=False):
        if weeks not in (26,52,104):raise ValueError('Unsupported correlation window.')
        with self.lock:
            now=self.clock()
            if self.enabled and not saved_only and(refresh or self.last_attempt is None or(now-self.last_attempt).total_seconds()>=POLL):
                self.last_attempt=now
                with ThreadPoolExecutor(max_workers=3) as pool:
                    jobs={}
                    history_checked=datetime.fromisoformat(self.checks['market']) if self.checks.get('market') else None
                    if refresh or not self.market or history_checked is None or(now-history_checked).total_seconds()>=HISTORY_POLL:
                        jobs['market']=pool.submit(self.load_market,now.date())
                    if self.quote_loader:jobs['quotes']=pool.submit(self.load_quotes,now)
                    elif self.forward_source:jobs['forwards']=pool.submit(self.forward_source.refresh,now)
                    if self.volatility_source and not self.quote_loader:
                        jobs['volatility']=pool.submit(self.volatility_source.refresh,now,force=refresh)
                    for key,job in jobs.items():
                        try:
                            value=job.result()
                            if key=='market':self.market=value
                            elif key=='quotes':self.quotes,self.feed=value
                            if key not in ('forwards','volatility'):self.checks[key]=self.clock().isoformat()
                            self.errors.pop(key,None)
                        except Exception:
                            # Request URLs may contain credentials. Never expose
                            # raw transport exceptions or authenticated URLs.
                            self.errors[key]=('FRED FX/S&P 500 history' if key=='market' else 'Forward/options feed')+' check failed. Saved values retain their original source timestamps.'
                if self.market or self.quotes:
                    try:
                        self.path.parent.mkdir(parents=True,exist_ok=True)
                        temporary=self.path.with_suffix('.tmp')
                        temporary.write_text(json.dumps({'schema':2,'market':self.market,'feed':self.feed,'checks':self.checks}),encoding='utf-8')
                        temporary.replace(self.path);self.errors.pop('cache',None)
                    except OSError:self.errors['cache']='The latest observations could not be saved to disk.'
            forward_snapshot=self.forward_source.snapshot(now) if self.forward_source else {'quotes':{},'checks':{},'errors':{}}
            volatility=self.volatility_source.snapshot(now) if self.volatility_source and not self.quote_loader else None
            rows=points(self.market,{**forward_snapshot['quotes'],**self.quotes},now.date(),weeks,now)
            for row in rows:
                if volatility and row['pair'] in volatility['quotes']:
                    row.update(volatility['quotes'][row['pair']])
                iv_max_age=7*86400 if row.get('iv_delivery')=='daily_report' else 300
                row['iv_stale']=bool(row.get('iv_time') and (now-datetime.fromisoformat(row['iv_time'])).total_seconds()>iv_max_age)
                if row.get('carry_basis')=='quoted_1Y_tenor':
                    row['source_check_at']=forward_snapshot['checks'].get(row['pair'])
                    row['source_check_failed']=row['pair'] in forward_snapshot['errors']
                    row['stale']=row['stale'] or row['source_check_failed']
            if not self.enabled:status='disabled'
            elif not self.quote_loader and not self.forward_source:status='awaiting_feed'
            elif self.forward_source and not self.quote_loader:
                status='unavailable' if not forward_snapshot['quotes'] else 'stale' if self.errors or forward_snapshot['errors'] or any(row['stale'] for row in rows) else 'partial'
            elif self.errors:status='stale' if self.quotes else 'unavailable'
            elif any(row['stale'] for row in rows):status='stale'
            elif len(self.quotes)<len(PAIRS) or not self.market:status='partial'
            else:status='ready'
            return deepcopy({'status':status,'basis':'observed_forwards_and_implied_volatility','feed_configured':bool(self.quote_loader),
                             'forward_feed_configured':bool(self.forward_source),'forward_source_checks':forward_snapshot['checks'],
                             'volatility_status':{k:v for k,v in volatility.items() if k!='quotes'} if volatility else None,
                             'volatility_poll_interval_seconds':60,
                             'forward_source_errors':forward_snapshot['errors'],
                             'quote_delivery':'Public web quotes; source delay is not guaranteed. Automatic checks do not imply streaming data.' if self.forward_source else 'Configured forward/options feed',
                             'iv_tenor':self.iv_tenor,'poll_interval_seconds':POLL,'history_poll_interval_seconds':HISTORY_POLL,
                             'weeks':weeks,'last_attempt_at':self.last_attempt.isoformat() if self.last_attempt else None,
                             'source_checks':self.checks,'errors':list(self.errors.values())+list(forward_snapshot['errors'].values())+(volatility['errors'] if volatility else []),'rows':rows,
                             'sources':[{'name':'FRED H.10 FX reference rates','url':'https://fred.stlouisfed.org/release?rid=15'},
                                        {'name':'S&P 500 via FRED','url':'https://fred.stlouisfed.org/series/SP500'}]
                                        +([{'name':'Investing.com 1Y forward points','url':'https://uk.investing.com/currencies/eur-usd-forward-rates'}] if self.forward_source else [])
                                        +([{'name':'Saxo public ATM volatility report','url':'https://fxowebtools.saxobank.com/otc.html'}] if volatility else []),
                             'methodology':('Investing.com mode uses observed spot midpoints plus 1Y forward-point midpoints (0.01 per JPY pip, 0.0001 otherwise). The quoted 1Y tenor is treated as one year: long-base carry = (spot/forward - 1) x 100. Exact settlement dates are not published, so no dates or day-count adjustment are invented. Spot and forward quote times may differ; both are disclosed. Public-source delay is not guaranteed. No implied volatility is supplied by this source. ' if self.forward_source else 'Configured feed carry uses observed spot and 12M outright quotes: long-base annualized carry = (spot/forward - 1) x 365/(forward value date - spot value date) x 100. ')
                             +'Long-quote carry uses (forward/spot - 1), not a sign flip. The first chart is long USD versus each foreign currency; the second is long each displayed pair base currency. '
                             +('Implied volatility uses only the selected ATM tenor from Saxo\'s public OTC options report. The server checks the report every minute while running, including when the dashboard is hidden. The visible chart reads updated observations every minute and catches up after focus or reconnection. Report observations retain their original GMT publication time; checking the report does not make it streaming. Reports more than seven days old are flagged. Carry and IV can have different observation times and feeds. Missing currencies remain unavailable. ' if volatility else 'Implied volatility requires quoted ATM option volatility for the selected tenor, in percentage points. ')
                             +'No policy-rate carry estimates or realized-volatility substitutes are used. Correlation uses completed calendar weeks of same-date FRED FX and S&P 500 log returns (at least 20 returns); missing weeks are not bridged. H.10 daily FX observations are released weekly. Old quotes and date-only spot observations are flagged. DXY is omitted because it has no single spot/12M-forward funding pair.'})
