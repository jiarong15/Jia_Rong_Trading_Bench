"""Published U.S. policy rates, Treasury yields, inflation, employment and housing data for the Rates page.

FRED levels are retained as released. Calendar-month transformations never
bridge missing months. Series refresh independently and keep dated saved data
during source failures. A breakeven is never relabelled as an inflation swap.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timezone
import csv
import io
import json
import math
import os
from pathlib import Path
from threading import RLock
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPSHandler, Request, build_opener

from .fx_exact_feed import NoRedirect, timestamp
from .fx_volatility import tls_context
from .dtcc_inflation import DTCCInflationSource

START = '1990-01-01'
POLL = 60
SERIES = {
    'DFF': ('Daily','Percent','Not seasonally adjusted','Federal Reserve H.15',False),
    'DGS2': ('Daily','Percent','Not seasonally adjusted','Federal Reserve H.15 / U.S. Treasury',False),
    'DGS5': ('Daily','Percent','Not seasonally adjusted','Federal Reserve H.15 / U.S. Treasury',False),
    'DGS10': ('Daily','Percent','Not seasonally adjusted','Federal Reserve H.15 / U.S. Treasury',False),
    'DGS30': ('Daily','Percent','Not seasonally adjusted','Federal Reserve H.15 / U.S. Treasury',False),
    'CUSR0000SAC': ('Monthly','Index 1982-1984=100','Seasonally adjusted','BLS',True),
    'CUSR0000SAS': ('Monthly','Index 1982-1984=100','Seasonally adjusted','BLS',True),
    'PCEPI': ('Monthly','Index 2017=100','Seasonally adjusted','BEA',True),
    'PCEPILFE': ('Monthly','Index 2017=100','Seasonally adjusted','BEA',True),
    'UNRATE': ('Monthly','Percent','Seasonally adjusted','BLS',False),
    'PAYEMS': ('Monthly','Thousands of persons','Seasonally adjusted','BLS',True),
    'FRBATLWGT3MMAUMHWGO': ('Monthly','Percent change from year ago','Not seasonally adjusted','Atlanta Fed',False),
    'CES0500000003': ('Monthly','Dollars per hour','Seasonally adjusted','BLS',True),
    'T5YIE': ('Daily','Percent','Not seasonally adjusted','St. Louis Fed / U.S. Treasury',False),
    'EXPINF5YR': ('Monthly','Percent','Not seasonally adjusted','Cleveland Fed',False),
    'JTSJOL': ('Monthly','Thousands of job openings','Seasonally adjusted','BLS JOLTS',True),
    'IHLIDXUS': ('Daily','Index 1 Feb 2020=100','Seasonally adjusted','Indeed Hiring Lab',True),
    'UNDCONTSA': ('Monthly','Thousands of units','Seasonally adjusted','U.S. Census Bureau / HUD',True),
}


def spec(key, label, identifier, transform='level', unit='%', axis='left', style='line'):
    return {'key':key,'label':label,'series_id':identifier,'transformation':transform,'unit':unit,'axis':axis,'style':style}


CHARTS = [
    {'key':f'policy_{years}y','title':f'U.S. policy rate vs {years}Y Treasury yield',
     'subtitle':'Effective federal funds rate and Treasury constant-maturity yield',
     'left_unit':'Rate %','series':[spec('policy_rate','Effective federal funds rate','DFF'),
         {**spec(f'treasury_{years}y',f'{years}Y Treasury yield',f'DGS{years}'),'preserve_gaps':True}],
     'notes':'Policy rate uses the effective federal funds rate (DFF), the observed overnight rate. Treasury yields are constant-maturity benchmarks. Both lines use published daily percent levels and retain their own observation dates; missing Treasury observations stay gaps.'}
    for years in (2,5,10,30)
] + [
    {'key':'cpi','title':'CPI goods & services','subtitle':'Year-over-year inflation · seasonally adjusted indexes',
     'left_unit':'% YoY','series':[spec('cpi_goods','CPI goods','CUSR0000SAC','yoy'),spec('cpi_services','CPI services','CUSR0000SAS','yoy')],
     'notes':'Goods uses the BLS commodities component, including food and energy goods. Services includes energy services. Growth is calculated from the same calendar month one year earlier.'},
    {'key':'pce','title':'PCE inflation','subtitle':'Headline and core · year-over-year change',
     'left_unit':'% YoY','series':[spec('pce','Headline PCE','PCEPI','yoy'),spec('core_pce','Core PCE','PCEPILFE','yoy')],
     'notes':'BEA chain-type price indexes. Core PCE excludes food and energy. These are inflation rates, not the dollar level of consumer spending.'},
    {'key':'employment','title':'Unemployment & nonfarm payrolls','subtitle':'Unemployment rate and monthly payroll change',
     'left_unit':'Unemployment %','right_unit':'Jobs, thousands',
     'series':[spec('unemployment','Unemployment rate','UNRATE'),spec('payroll_change','Nonfarm payroll change','PAYEMS','monthly_change','thousand jobs','right','bar')],
     'notes':'Unemployment is the household-survey rate. Payrolls are the month-over-month change in total nonfarm establishment employment, in thousands. Both are seasonally adjusted and can be revised.'},
    {'key':'wages','title':'Wage growth & hourly earnings growth','subtitle':'Atlanta Fed wage tracker and BLS average hourly earnings',
     'left_unit':'% YoY','series':[spec('wage_growth','Atlanta Fed wage growth','FRBATLWGT3MMAUMHWGO'),spec('earnings_growth','Average hourly earnings growth','CES0500000003','yoy')],
     'notes':'Atlanta Fed: 3-month moving average of unweighted median 12-month hourly wage growth for matched individuals (not seasonally adjusted). BLS: year-over-year growth in seasonally adjusted average hourly earnings of all private employees. The populations and methods differ.'},
    {'key':'inflation_expectations','title':'Inflation swaps, breakevens & expectations','subtitle':'United States · matching 5-year horizons',
     'left_unit':'Annualized %','series':[spec('inflation_swap','5Y zero-coupon inflation swap','USD_ZCIS_5Y'),spec('breakeven','5Y Treasury breakeven','T5YIE'),spec('expected_inflation','5Y expected inflation','EXPINF5YR')],
     'notes':'The DTCC swap line is the daily median of eligible executed 5Y USD CPI-U zero-coupon swaps, recalculated from reported trades during the day. Trade dates use UTC. It is not a continuous dealer quote. Packages, upfront payments and annual-coupon swaps are excluded; corrections/errors revise the history. Breakevens are Treasury inflation compensation, including risk/liquidity premia. Cleveland Fed expectations are monthly model estimates. Each line retains its native dates.'},
    {'key':'jobs','title':'Job openings & job postings','subtitle':'JOLTS openings and the Indeed U.S. postings index',
     'left_unit':'Openings, millions','right_unit':'Postings index',
     'series':[spec('job_openings','JOLTS job openings','JTSJOL','millions','million openings'),spec('job_postings','Indeed job postings','IHLIDXUS','level','index','right')],
     'notes':'JOLTS is a monthly stock of job openings. Indeed Hiring Lab is a seasonally adjusted postings index (1 February 2020=100), calculated using a trailing 7-day average. The index is not a count of vacancies. Credit: Indeed Hiring Lab via FRED; Indeed owns the postings data.'},
    {'key':'housing','title':'Total housing under construction','subtitle':'New privately owned housing units · seasonally adjusted',
     'left_unit':'Units, thousands','series':[spec('housing_under_construction','Housing under construction','UNDCONTSA','level','thousand units')],
     'notes':'Census/HUD total new privately owned units under construction at the end of the reference period. This is the stock under construction, not housing starts or completions.'},
]


def fetch(url, token=None):
    headers = {'User-Agent':'JRTradingWorkbench/1.0','Accept':'application/json,text/csv','Cache-Control':'no-cache'}
    if token: headers['Authorization']='Bearer '+token
    opener = build_opener(NoRedirect(),HTTPSHandler(context=tls_context()))
    with opener.open(Request(url,headers=headers),timeout=18) as response: body=response.read(8_000_001)
    if len(body)>8_000_000: raise ValueError('Rates response exceeds size limit.')
    return body.decode('utf-8-sig')


def validate_values(values, identifier, today):
    if not isinstance(values,dict) or not 2<=len(values)<=30000: raise ValueError('Incomplete history.')
    result={}
    for stamp,value in values.items():
        at=date.fromisoformat(stamp)
        if at.isoformat()!=stamp or stamp<START or at>today: raise ValueError('Invalid observation date.')
        if identifier in SERIES and SERIES[identifier][0]=='Monthly' and at.day!=1: raise ValueError('Expected monthly reference dates.')
        if value is not None:
            if type(value) not in (int,float) or not math.isfinite(value): raise ValueError('Non-finite source value.')
            if identifier in SERIES and SERIES[identifier][4] and value<=0: raise ValueError('Invalid source level.')
        result[stamp]=value
    if sum(v is not None for v in result.values())<2: raise ValueError('Insufficient published values.')
    return dict(sorted(result.items()))


def parse_fred(body, identifier, today, authenticated):
    if authenticated:
        document=json.loads(body); records=document['observations']
        if not isinstance(records,list) or document.get('count')!=len(records) or document.get('offset')!=0 or document.get('units')!='lin':
            raise ValueError('Incomplete or transformed FRED response.')
        pairs=[(item['date'],item['value']) for item in records]
    else:
        reader=csv.DictReader(io.StringIO(body))
        if reader.fieldnames!=['observation_date',identifier]: raise ValueError('Unexpected FRED CSV columns.')
        pairs=[(item['observation_date'],item[identifier]) for item in reader]
    values={}
    for stamp,raw in pairs:
        if stamp in values or not isinstance(raw,str): raise ValueError('Duplicate or invalid observation.')
        values[stamp]=None if raw.strip() in ('','.','NA') else float(raw)
    return validate_values(values,identifier,today)


def shift_month(stamp, months):
    at=date.fromisoformat(stamp); total=at.year*12+at.month-1+months
    return date(total//12,total%12+1,1).isoformat()


def transformed(values, kind):
    result=[]
    for stamp,value in sorted(values.items()):
        output=value
        if value is not None and kind in ('yoy','monthly_change'):
            previous=values.get(shift_month(stamp,-12 if kind=='yoy' else -1))
            output=None if previous is None or kind=='yoy' and previous<=0 else (value/previous-1)*100 if kind=='yoy' else value-previous
        elif value is not None and kind=='millions': output=value/1000
        result.append({'date':stamp,'value':round(output,6) if output is not None else None})
    return result


def parse_swap(document, now):
    if isinstance(document,str): document=json.loads(document)
    if (document.get('country')!='USA' or document.get('instrument')!='zero_coupon_inflation_swap'
            or document.get('index')!='CPI-U' or document.get('tenor_years')!=5 or document.get('unit')!='percent'
            or not isinstance(document.get('provider'),str) or not 1<=len(document['provider'])<=120):
        raise ValueError('Expected a genuine U.S. CPI 5Y zero-coupon inflation swap feed.')
    as_of=timestamp(document['as_of'],now)
    observations=document['observations']
    if not isinstance(observations,list) or not 2<=len(observations)<=30000: raise ValueError('Invalid swap history.')
    values={}
    for item in observations:
        if item['date'] in values: raise ValueError('Duplicate swap observation.')
        if date.fromisoformat(item['date'])>as_of.date(): raise ValueError('Swap observation exceeds feed date.')
        values[item['date']]=item['value']
    return validate_values(values,'USD_ZCIS_5Y',now.date()),document['provider']


class USRatesService:
    def __init__(self,path,enabled=True,transport=None,clock=None,api_key=None,swap_source=None):
        self.path=Path(path); self.enabled=enabled; self.transport=transport or fetch
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.api_key=api_key if api_key is not None else os.environ.get('FRED_API_KEY') or os.environ.get('BONDS_FRED_API_KEY')
        self.swap_url=os.environ.get('RATES_INFLATION_SWAP_URL','').strip()
        self.swap_token=os.environ.get('RATES_INFLATION_SWAP_TOKEN','').strip()
        if self.swap_url:
            parts=urlsplit(self.swap_url)
            if (parts.scheme!='https' and not(parts.scheme=='http' and parts.hostname in ('localhost','127.0.0.1','::1'))) or not parts.hostname or parts.username or parts.password:
                raise ValueError('Use an HTTPS inflation swap source or a local HTTP bridge.')
        self.swap_source=swap_source if swap_source is not None else None if self.swap_url else DTCCInflationSource(
            self.path.with_name('dtcc-inflation.json'), enabled=enabled and os.environ.get('RATES_DTCC_ENABLED','true').lower()=='true', clock=self.clock)
        self.lock=RLock(); self.values={}; self.checks={}; self.errors={}; self.last_attempt=None; self.cache_error=None; self.swap_provider=None
        try:
            saved=json.loads(self.path.read_text(encoding='utf-8'))
            if saved['schema']!=1: raise ValueError('Invalid rates snapshot.')
            for identifier,values in saved['values'].items():
                if identifier not in SERIES and identifier!='USD_ZCIS_5Y': raise ValueError('Unexpected rates series.')
                valid=validate_values(values,identifier,self.clock().date())
                checked=timestamp(saved['checks'][identifier],self.clock()).isoformat()
                if identifier!='USD_ZCIS_5Y' or self.swap_url:
                    self.values[identifier]=valid; self.checks[identifier]=checked
            self.swap_provider=saved.get('swap_provider')
        except (OSError,ValueError,KeyError,TypeError,AttributeError):
            self.values={}; self.checks={}; self.swap_provider=None

    def load_one(self,identifier,now):
        if identifier=='USD_ZCIS_5Y': return parse_swap(self.transport(self.swap_url,token=self.swap_token or None),now)
        if self.api_key:
            params={'series_id':identifier,'api_key':self.api_key,'file_type':'json','observation_start':START,
                    'observation_end':now.date().isoformat(),'units':'lin','sort_order':'asc','limit':100000,'offset':0}
            url='https://api.stlouisfed.org/fred/series/observations?'+urlencode(params)
        else:
            url='https://fred.stlouisfed.org/graph/fredgraph.csv?'+urlencode({'id':identifier,'cosd':START,'coed':now.date().isoformat()})
        return parse_fred(self.transport(url),identifier,now.date(),bool(self.api_key)),None

    def view(self,refresh=False,saved_only=False):
        if self.swap_source and self.enabled and not saved_only:
            self.swap_source.refresh(self.clock(),force=refresh)
        swap_snapshot=self.swap_source.snapshot() if self.swap_source else None
        with self.lock:
            now=self.clock()
            if self.enabled and not saved_only and (refresh or self.last_attempt is None or (now-self.last_attempt).total_seconds()>=POLL):
                self.last_attempt=now
                identifiers=list(SERIES)+(['USD_ZCIS_5Y'] if self.swap_url else [])
                with ThreadPoolExecutor(max_workers=5) as pool:
                    jobs={key:pool.submit(self.load_one,key,now) for key in identifiers}
                    for identifier,job in jobs.items():
                        try:
                            values,provider=job.result(); previous=self.values.get(identifier,{})
                            old_dates=[d for d,v in previous.items() if v is not None]; new_dates=[d for d,v in values.items() if v is not None]
                            if old_dates and (max(new_dates)<max(old_dates) or min(new_dates)>min(old_dates)):
                                raise ValueError('Truncated or regressed history.')
                            self.values[identifier]=values; self.checks[identifier]=self.clock().isoformat(); self.errors.pop(identifier,None)
                            if provider: self.swap_provider=provider
                        except Exception:
                            self.errors[identifier]='Source check failed. Saved values retain their original observation dates.'
                if self.values:
                    try:
                        self.path.parent.mkdir(parents=True,exist_ok=True); temporary=self.path.with_suffix('.tmp')
                        temporary.write_text(json.dumps({'schema':1,'values':self.values,'checks':self.checks,'swap_provider':self.swap_provider}),encoding='utf-8')
                        temporary.replace(self.path); self.cache_error=None
                    except OSError: self.cache_error='The latest rates observations could not be saved to disk.'
            charts=[]
            for config in CHARTS:
                chart={k:v for k,v in config.items() if k!='series'}; chart['series']=[]
                if config['key']=='inflation_expectations' and self.swap_url:
                    chart['notes']='The configured feed supplies genuine 5Y USD CPI-U zero-coupon inflation swap rates. Treasury breakevens include risk/liquidity premia; Cleveland Fed expected inflation is a monthly model estimate. Each line retains its native observation dates.'
                for definition in config['series']:
                    identifier=definition['series_id']; values=self.values.get(identifier,{})
                    dtcc=swap_snapshot if identifier=='USD_ZCIS_5Y' and swap_snapshot else None
                    if dtcc: values=dtcc['values']
                    observations=transformed(values,definition['transformation'])
                    available=[item for item in observations if item['value'] is not None]; last=available[-1] if available else None
                    info=SERIES.get(identifier,('Daily','Percent','Not seasonally adjusted',dtcc['provider'] if dtcc else self.swap_provider or 'No swap source connected',False))
                    unavailable=identifier=='USD_ZCIS_5Y' and not self.swap_url and not dtcc
                    status=dtcc['status'] if dtcc else 'awaiting_source' if unavailable else 'stale' if identifier in self.errors and values else 'ready' if values else 'unavailable'
                    chart['series'].append({**definition,'label':'5Y zero-coupon inflation swap · trade median' if dtcc else definition['label'],
                        'frequency':info[0],'source':info[3],'seasonal_adjustment':info[2],
                        'source_url':'https://fred.stlouisfed.org/series/'+identifier if identifier in SERIES else dtcc['source_url'] if dtcc else None,
                        'observations':observations,'last_value':last['value'] if last else None,'last_observation_date':last['date'] if last else None,
                        'last_checked_at':dtcc['checked_at'] if dtcc else self.checks.get(identifier),'status':status,
                        'error':dtcc['error'] if dtcc else 'No inflation swap source is enabled. No swap proxy is substituted.' if unavailable else self.errors.get(identifier),
                        **({'delivery':dtcc['delivery'],'last_observation_at':dtcc['last_trade_at'],
                            'last_disseminated_at':dtcc['published_at'],'trade_count':dtcc['observation_details'].get(last['date'],{}).get('trade_count') if last else None,
                            'observation_details':dtcc['observation_details'],'history_start':dtcc['history_start'],
                            'pending_live_files':dtcc['pending_live_files'],'pending_history_files':dtcc['pending_history_files']} if dtcc else {})})
                charts.append(chart)
            all_ready=all(s['status']=='ready' and s['last_value'] is not None for c in charts for s in c['series'])
            return deepcopy({'country':'USA','status':'disabled' if not self.enabled else 'ready' if all_ready else 'partial',
                'poll_interval_seconds':POLL,'history_start':START,'last_attempt_at':self.last_attempt.isoformat() if self.last_attempt else None,
                'cache_warning':self.cache_error,'charts':charts,
                'notice':'FRED releases are checked every minute while visible; DTCC swap trades are checked every minute in the background while the server runs. Source publication and reporting delays apply. Monthly dates identify reference months, not release dates. Corrections and revisions update history; missing observations are never filled.'})
