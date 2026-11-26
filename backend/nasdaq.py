"""Read-only Nasdaq Data Link fallback. Filing references are not filing text.

NDAQ metadata was verified against the live API. These tables have no filing
date/accession columns, so raw annual rows are displayed for research and never
silently converted into accession-bound SEC inputs or point-in-time backtests.
"""
from copy import deepcopy
from datetime import date, datetime, timezone
import json
import math
import re
from threading import RLock
import time
import urllib.error
import urllib.parse
import urllib.request
from .credentials import nasdaq_key

BASE='https://data.nasdaq.com/api/v3/datatables/'
TABLES=('NDAQ/RD','NDAQ/BS','NDAQ/IS','NDAQ/CF','NDAQ/FD')

class NasdaqError(ValueError):
    def __init__(self,message,status='error',code=None):
        super().__init__(message);self.status=status;self.code=code

def filing_reference(value):
    """Only permit SEC HTTPS index links with a real CIK; never fetch provider URLs."""
    if not isinstance(value,str):return None,None
    try:
        parsed=urllib.parse.urlsplit(value)
        if parsed.scheme!='https' or parsed.netloc.lower() not in ('www.sec.gov','sec.gov') or parsed.fragment:return None,None
        if parsed.path not in ('/cgi-bin/browse-edgar','/edgar/browse/'):return None,None
        cik=urllib.parse.parse_qs(parsed.query).get('CIK',[''])[0]
        if not re.fullmatch(r'\d{1,10}',cik) or not int(cik):return None,None
        cik=cik.zfill(10)
        return f'https://www.sec.gov/edgar/browse/?CIK={int(cik)}&owner=exclude',cik
    except ValueError:return None,None

class NasdaqService:
    def __init__(self,enabled=True,transport=None):
        self.enabled=enabled;self.transport=transport;self.lock=RLock();self.cache={};self.blocked=None
    def request(self,table,params):
        if table not in TABLES:raise NasdaqError('Unsupported Nasdaq table.')
        if self.transport:return self.transport(table,params)
        try:key=nasdaq_key()
        except ValueError:raise NasdaqError('Nasdaq Data Link credentials are not configured on the backend.','not_configured') from None
        url=BASE+table+'.json?'+urllib.parse.urlencode({**params,'api_key':key})
        req=urllib.request.Request(url,headers={'User-Agent':'JRTradingWorkbench/0.1','Accept':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=20) as response:raw=response.read(5*1024*1024+1)
            if len(raw)>5*1024*1024:raise NasdaqError('Nasdaq response exceeded the 5 MB limit.')
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            # Never propagate authenticated URLs or arbitrary response bodies.
            try:code=json.loads(exc.read(8192)).get('quandl_error',{}).get('code')
            except (ValueError,AttributeError):code=None
            code=code if isinstance(code,str) and re.fullmatch(r'[A-Za-z0-9]{1,20}',code) else None
            if code=='QEPx04':raise NasdaqError(f'Nasdaq {table}: subscription required (HTTP {exc.code}, QEPx04). This key does not have access to the NDAQ database.','subscription_required',code) from None
            if exc.code==401:raise NasdaqError('Nasdaq authentication failed. Check the backend API key.','authentication_failed',code) from None
            if exc.code==429:raise NasdaqError('Nasdaq rate limit reached. Try again later.','rate_limited',code) from None
            raise NasdaqError(f'Nasdaq {table} request failed (HTTP {exc.code}).','error',code) from None
        except (OSError,ValueError) as exc:
            if isinstance(exc,NasdaqError):raise
            raise NasdaqError('Nasdaq could not be reached or returned invalid JSON.') from None
    def rows(self,table,params):
        result=[];cursor=None
        for _ in range(3):
            payload=self.request(table,{**params,'qopts.per_page':1000,**({'qopts.cursor_id':cursor} if cursor else {})})
            try:
                data=payload['datatable'];names=[c['name'] for c in data['columns']]
                if len(set(names))!=len(names) or not all(isinstance(n,str) for n in names):raise ValueError()
                for values in data['data']:
                    if len(values)!=len(names):raise ValueError()
                    if any(not isinstance(v,(str,int,float,bool,type(None))) or isinstance(v,float) and not math.isfinite(v) for v in values):raise ValueError()
                    result.append(dict(zip(names,values)))
                cursor=payload.get('meta',{}).get('next_cursor_id')
            except (KeyError,TypeError,ValueError):raise NasdaqError('Nasdaq returned an unexpected table schema; no records imported.') from None
            if not cursor:return result
        raise NasdaqError('Nasdaq result exceeds the bounded pagination limit; no truncated annual records imported.')
    def report(self,symbol,refresh=False):
        with self.lock:
            if refresh:self.cache.pop(symbol,None);self.blocked=None
            old=self.cache.get(symbol)
            if old and time.monotonic()-old[0]<900:return deepcopy(old[1])
            report={'symbol':symbol,'source':'Nasdaq Data Link','status':'unavailable','document':None,
                'full_document_available':False,'fetched_at':datetime.now(timezone.utc).isoformat(),'annual_tables':[],
                'message':'No matching Nasdaq company record was found.','warnings':[]}
            if not self.enabled and not self.transport:
                report.update(status='disabled',message='Nasdaq Data Link is disabled on this backend.');return report
            try:
                if self.blocked and time.monotonic()-self.blocked[0]<900:raise self.blocked[1]
                records=self.rows('NDAQ/RD',{'symbol':symbol})
                matches=[r for r in records if r.get('symbol','').upper().replace('.','-')==symbol.upper().replace('.','-')]
                if len(matches)>1:raise NasdaqError('Nasdaq returned ambiguous company identities; no company was guessed.')
                if matches:
                    ref=matches[0];url,cik=filing_reference(ref.get('secfilings'))
                    report.update(company=ref.get('name'),filings_url=url,cik=cik,status='reference_only',
                        message='Nasdaq company reference retrieved. The filing index is a link to EDGAR, not a downloaded 10-K.')
                    if ref.get('secfilings') and not url:report['warnings'].append('The provider filing reference could not be verified as a SEC company index URL.')
                    for table in TABLES[1:]:
                        try:
                            rows=self.rows(table,{'symbol':symbol,'dimension':'ARY','reportperiod.gte':f'{date.today().year-5}-01-01'})
                            annual=[]
                            for row in rows:
                                if row.get('symbol','').upper().replace('.','-')!=symbol.upper().replace('.','-'):raise NasdaqError('Nasdaq financial-row ticker did not match the requested company.')
                                if ref.get('figi') and row.get('figi')!=ref['figi']:raise NasdaqError('Nasdaq financial-row FIGI did not match the company reference.')
                                try:period=date.fromisoformat(row['reportperiod'])
                                except (KeyError,ValueError,TypeError):raise NasdaqError('Nasdaq returned an invalid annual reporting period.') from None
                                if row.get('dimension')!='ARY' or period>date.today():raise NasdaqError('Nasdaq returned a nonannual or future reporting period.')
                                annual.append(row)
                            if annual:report['annual_tables'].append({'table':table,'url':f'https://data.nasdaq.com/databases/NDAQ/documentation','rows':sorted(annual,key=lambda r:(r['reportperiod'],str(r.get('calendardate',''))),reverse=True)})
                        except NasdaqError as exc:
                            report['warnings'].append(str(exc))
                            if exc.status in ('subscription_required','authentication_failed','rate_limited'):
                                self.blocked=(time.monotonic(),exc);report['code']=exc.code
                                if not report['annual_tables']:report['status']=exc.status;report['message']=str(exc)
                                break
                    if report['annual_tables']:
                        report.update(status='annual_data',message='Nasdaq annual financial rows retrieved. These are structured data, not the full 10-K document.')
                    report['warnings'].append('Raw provider values retain their column names, units and currency. No filing dates are supplied by these tables; figures are not automatically inserted into the SEC worksheet or used in scores/backtests. Verify units, periods and disclosures against the filing first.')
            except NasdaqError as exc:
                report.update(status=exc.status,code=exc.code,message=str(exc))
                if exc.status in ('subscription_required','authentication_failed','rate_limited'):self.blocked=(time.monotonic(),exc)
            if len(self.cache)>=32:self.cache.pop(next(iter(self.cache)))
            self.cache[symbol]=(time.monotonic(),deepcopy(report))
            return report
