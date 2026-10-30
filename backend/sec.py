"""Read-only EDGAR client and conservative, accession-bound annual XBRL mapping.

Filing text is treated as untrusted data; it is downloaded, never executed or
interpreted as application instructions. Missing disclosures are never zeroes.
"""
from copy import deepcopy
from datetime import date, datetime, timezone
import json
from math import isfinite
import os
from pathlib import Path
import re
from threading import RLock
import time
import urllib.error
import urllib.parse
import urllib.request
from .equity_analysis import FIELDS, validate_financials

class SECError(ValueError):pass

DIRECT={
 'net_income':(['NetIncomeLoss'],'duration'),
 'cfo':(['NetCashProvidedByUsedInOperatingActivities'],'duration'),
 'assets':(['Assets'],'instant'),
 'current_assets':(['AssetsCurrent'],'instant'),
 'current_liabilities':(['LiabilitiesCurrent'],'instant'),
 'revenue':(['RevenueFromContractWithCustomerExcludingAssessedTax','Revenues','SalesRevenueNet'],'duration'),
 'gross_profit':(['GrossProfit'],'duration'),
 'shares':(['CommonStockSharesOutstanding'],'instant'),
 'book_equity':(['StockholdersEquity'],'instant'),
 'ebit':(['OperatingIncomeLoss'],'duration'),
 'cash':(['CashAndCashEquivalentsAtCarryingValue'],'instant'),
 'long_term_debt':(['LongTermDebt'],'instant'),
}

def normalize_symbol(symbol):return symbol.upper().replace('.','-')

def filing_rows(columns):
    result=[]
    for i,form in enumerate(columns.get('form',[])):
        if form not in ['10-K','10-K/A','20-F','40-F']:continue
        try:
            row={k:columns[k][i] for k in ['form','accessionNumber','filingDate','reportDate','primaryDocument']}
            date.fromisoformat(row['filingDate']);date.fromisoformat(row['reportDate'])
        except (KeyError,IndexError,ValueError):continue
        if row['filingDate']<=date.today().isoformat():result.append(row)
    return result

def filing_url(cik,filing):
    accession=filing['accessionNumber'];filename=filing['primaryDocument']
    if not re.fullmatch(r'\d{10}-\d{2}-\d{6}',accession) or not re.fullmatch(r'[A-Za-z0-9_.-]+\.(?:htm|html|txt)',filename,re.I):
        raise SECError('SEC returned an invalid filing document reference.')
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-','')}/{urllib.parse.quote(filename)}"

def map_financials(facts,filings,latest,cik,symbol):
    """Select annual periods by end/start dates, never by the misleading XBRL fy label."""
    gaap=facts.get('facts',{}).get('us-gaap',{});end=latest['reportDate']
    prior_filings=[f for f in filings if f['form']=='10-K' and 330<=(date.fromisoformat(end)-date.fromisoformat(f['reportDate'])).days<=380]
    prior_filing=max(prior_filings,key=lambda f:f['filingDate']) if prior_filings else None
    if prior_filing:prior_end=prior_filing['reportDate']
    else:
        candidates=[]
        for tag in ['NetIncomeLoss','Revenues','RevenueFromContractWithCustomerExcludingAssessedTax']:
            for v in gaap.get(tag,{}).get('units',{}).get('USD',[]):
                if v.get('accn')==latest['accessionNumber'] and v.get('start'):
                    try:
                        if 330<=(date.fromisoformat(end)-date.fromisoformat(v['end'])).days<=380 and 330<=(date.fromisoformat(v['end'])-date.fromisoformat(v['start'])).days<=380:candidates.append(v['end'])
                    except (KeyError,ValueError):continue
        prior_end=max(candidates) if candidates else None
    if not prior_end:return None,{},['A comparable prior annual period was not available in standard US-GAAP facts.']
    # Use this report's restated comparatives first. Older 10-Ks fill coverage
    # gaps only; their accession remains attached to every imported number.
    allowed=[latest]+sorted([f for f in filings if f['form']=='10-K' and f['filingDate']<=latest['filingDate'] and f['accessionNumber']!=latest['accessionNumber']],key=lambda f:f['filingDate'],reverse=True)
    provenance={};warnings=[]
    def fact(tags,period,kind='instant',unit='USD'):
        for filing in allowed:
            for tag in tags:
                values=[]
                for v in gaap.get(tag,{}).get('units',{}).get(unit,[]):
                    if v.get('accn')!=filing['accessionNumber'] or v.get('form') not in ['10-K','10-K/A'] or v.get('end')!=period or v.get('filed','')>latest['filingDate']:continue
                    try:
                        duration=(date.fromisoformat(v['end'])-date.fromisoformat(v['start'])).days if v.get('start') else None
                    except (ValueError,KeyError):continue
                    if kind=='duration' and (duration is None or not 330<=duration<=380):continue
                    if kind=='instant' and duration is not None:continue
                    number=v.get('val')
                    if isinstance(number,bool) or not isinstance(number,(int,float)) or not isfinite(number):continue
                    values.append(v)
                if not values:continue
                if len({v['val'] for v in values})!=1:
                    warnings.append(f'Ambiguous {tag} values for {period}; left for review.');return None
                v=values[0]
                return {'value':v['val']/1e6,'tag':f'us-gaap:{tag}','unit':unit,'end':period,'start':v.get('start'),'accession':v['accn'],'filed_at':v['filed'],'url':filing_url(cik,filing)}
        return None
    def put(row,period_name,key,item):
        if item is None:return
        value=item['value']
        if key in ['assets','beginning_assets','shares','invested_capital'] and value<=0:return
        if key in ['current_assets','current_liabilities','long_term_debt','revenue','cash','total_debt'] and value<0:return
        row[key]=value;provenance[f'{period_name}.{key}']=item
    def combined(items,formula):
        if any(item is None for item in items):return None
        return {'value':sum(i['value'] for i in items),'formula':formula,'components':items}
    current={k:None for k in FIELDS};prior={k:None for k in FIELDS}
    for period_name,row,period in [('current',current,end),('prior',prior,prior_end)]:
        for field,(tags,kind) in DIRECT.items():put(row,period_name,field,fact(tags,period,kind,'shares' if field=='shares' else 'USD'))
        if row['gross_profit'] is None and row['revenue'] is not None:
            cost=fact(['CostOfRevenue','CostOfGoodsAndServicesSold'],period,'duration')
            if cost:
                revenue=provenance[f'{period_name}.revenue']
                put(row,period_name,'gross_profit',{'value':revenue['value']-cost['value'],'formula':'Revenue minus reported cost of revenue / goods and services','components':[revenue,cost]})
        if row['long_term_debt'] is None:
            put(row,period_name,'long_term_debt',combined([fact(['LongTermDebtCurrent'],period),fact(['LongTermDebtNoncurrent'],period)],'Current plus noncurrent long-term debt'))
        noncurrent=fact(['LongTermDebtNoncurrent'],period);debt_current=fact(['DebtCurrent','ShortTermBorrowingsAndCurrentPortionOfLongTermDebt'],period)
        total=combined([noncurrent,debt_current],'Noncurrent long-term debt plus total current debt')
        if total is None and row['long_term_debt'] is not None:
            total=combined([provenance[f'{period_name}.long_term_debt'],fact(['ShortTermBorrowings'],period)],'Total long-term debt plus reported short-term borrowings')
        put(row,period_name,'total_debt',total)
        if all(row[k] is not None for k in ['book_equity','total_debt','cash']):
            put(row,period_name,'invested_capital',{'value':row['book_equity']+row['total_debt']-row['cash'],'formula':'Book equity + interest-bearing debt - cash (simplified financing approach)','components':[provenance[f'{period_name}.{k}'] for k in ['book_equity','total_debt','cash']]})
        # Positive cash issuance proves issuance; an absent or zero cash-flow
        # tag cannot rule out non-cash issuance and is never treated as "No".
        issued=fact(['ProceedsFromIssuanceOfCommonStock'],period,'duration')
        row['equity_issued']=True if issued and issued['value']>0 else None
        if row['equity_issued'] is not None:provenance[f'{period_name}.equity_issued']=issued
    put(current,'current','beginning_assets',fact(['Assets'],prior_end))
    # Find an actual disclosed opening balance; do not manufacture leap-year
    # or 52/53-week fiscal dates with a fixed 365-day subtraction.
    opening_dates=set()
    for v in gaap.get('Assets',{}).get('units',{}).get('USD',[]):
        try:
            if 330<=(date.fromisoformat(prior_end)-date.fromisoformat(v['end'])).days<=380:opening_dates.add(v['end'])
        except (KeyError,ValueError):pass
    for opening in sorted(opening_dates,reverse=True):
        value=fact(['Assets'],opening)
        if value:put(prior,'prior','beginning_assets',value);break
    document={'source':f"SEC EDGAR 10-K: {filing_url(cik,latest)}",'period_end':end,'filed_at':latest['filingDate'],
        'prior_period_end':prior_end,'prior_filed_at':latest['filingDate'],'current':current,'prior':prior}
    document=validate_financials(document,date.today())
    warnings.extend(['Annual shares are historical, not a live diluted share count. Verify subsequent splits and share-class economics before using current-price valuations.',
        'Latest 10-K comparatives are preferred. Older filings may supply opening balances or missing fields; verify restatements and discontinued operations.',
        'Missing custom tags, debt components and issuance evidence remain unknown. USD millions and millions of shares; no missing debt component is assumed zero.'])
    return document,provenance,list(dict.fromkeys(warnings))

class SECService:
    def __init__(self,cache_dir,enabled=True,transport=None):
        self.cache_dir=Path(cache_dir);self.enabled=enabled;self.transport=transport;self.lock=RLock();self.cache={};self.last_request=0.
    def request(self,url,html=False):
        if not self.enabled and self.transport is None:raise SECError('SEC requests are disabled in this isolated test instance.')
        if self.transport:return self.transport(url,html)
        # Identify the application honestly; operators can provide their own
        # contact in SEC_USER_AGENT without sharing any Alpaca credentials.
        user_agent=os.environ.get('SEC_USER_AGENT','MeridianInvestmentWorkbench/0.1 (single-user investment research)')
        with self.lock:
            delay=.25-(time.monotonic()-self.last_request)
            if delay>0:time.sleep(delay)
            self.last_request=time.monotonic()
            req=urllib.request.Request(url,headers={'User-Agent':user_agent,'Accept':'text/html' if html else 'application/json'})
            try:
                with urllib.request.urlopen(req,timeout=30) as response:
                    raw=response.read(25*1024*1024+1)
                if len(raw)>25*1024*1024:raise SECError('SEC response exceeded the 25 MB download limit.')
                return raw if html else json.loads(raw)
            except urllib.error.HTTPError as exc:
                reason={403:'SEC denied access. Check the declared SEC_USER_AGENT/contact and try again later.',404:'The requested SEC record was not found.',429:'SEC rate limit reached. Wait before retrying.'}.get(exc.code,'The upstream SEC request failed.')
                raise SECError(f'SEC HTTP {exc.code}. {reason}') from None
            except (OSError,ValueError) as exc:
                if isinstance(exc,SECError):raise
                raise SECError('SEC could not be reached or returned invalid data. Existing saved inputs were preserved.') from None
    def cached(self,key,ttl,loader):
        with self.lock:
            old=self.cache.get(key)
            if old and time.monotonic()-old[0]<ttl:return deepcopy(old[1])
            value=loader()
            # Company facts can be large; keep only a bounded recent set.
            if len(self.cache)>=24:self.cache.pop(next(iter(self.cache)))
            self.cache[key]=(time.monotonic(),value);return deepcopy(value)
    def report(self,symbol):
        return self.cached(('report',symbol),3600,lambda:self._report(symbol))
    def _report(self,symbol):
        tickers=self.cached('tickers',86400,lambda:self.request('https://www.sec.gov/files/company_tickers.json'))
        match=[t for t in tickers.values() if normalize_symbol(t['ticker'])==normalize_symbol(symbol)]
        if not match:return {'status':'unavailable','message':f'No SEC company CIK mapping was found for {symbol}. Funds, foreign listings and some securities do not have a company 10-K.', 'document':None}
        cik=str(match[0]['cik_str']).zfill(10)
        return self._report_for_cik(symbol,cik)
    def report_for_cik(self,symbol,cik):
        """Recover directory gaps using a validated external filing reference.

        EDGAR submissions must independently confirm the exact ticker. The
        resulting document still comes from SEC, with Nasdaq discovery credited.
        """
        if not re.fullmatch(r'\d{1,10}',str(cik)):raise SECError('Invalid company CIK reference.')
        report=self._report_for_cik(symbol,str(cik).zfill(10))
        report['discovery_source']='Nasdaq Data Link NDAQ/RD; company identity verified by SEC submissions'
        with self.lock:self.cache[('report',symbol)]=(time.monotonic(),deepcopy(report))
        return report
    def _report_for_cik(self,symbol,cik):
        submission=self.request(f'https://data.sec.gov/submissions/CIK{cik}.json')
        if normalize_symbol(symbol) not in [normalize_symbol(t) for t in submission.get('tickers',[])]:
            raise SECError('SEC ticker mapping did not match the company submission record; no company was guessed.')
        filings=filing_rows(submission.get('filings',{}).get('recent',{}))
        for older in submission.get('filings',{}).get('files',[])[:8]:
            if len({f['reportDate'] for f in filings if f['form']=='10-K'})>=3:break
            filename=older.get('name','')
            if not re.fullmatch(r'CIK\d{10}-submissions-\d+\.json',filename):continue
            filings.extend(filing_rows(self.request('https://data.sec.gov/submissions/'+filename)))
        annuals=[f for f in filings if f['form']=='10-K']
        if not annuals:
            forms=sorted({f['form'] for f in filings})
            return {'status':'unavailable','cik':cik,'company':submission.get('name'), 'document':None,
                'message':f'No original Form 10-K was found for {symbol} in the inspected submission history. Other annual forms: {", ".join(forms) or "none"}. 20-F/40-F and fund reports need a separate mapping.'}
        latest=max(annuals,key=lambda f:(f['reportDate'],f['filingDate']))
        url=filing_url(cik,latest)
        # A missing XBRL endpoint does not mean the 10-K itself is missing.
        try: facts=self.request(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json')
        except SECError as exc:
            facts=None;document=None;provenance={};warnings=[f'Annual XBRL facts unavailable: {exc}']
        if facts is not None:
            if str(facts.get('cik','')).lstrip('0')!=cik.lstrip('0'):raise SECError('SEC financial-fact identity did not match the requested company.')
            document,provenance,warnings=map_financials(facts,filings,latest,cik,symbol)
        amendments=[f for f in filings if f['form']=='10-K/A' and f['reportDate']==latest['reportDate']]
        if amendments:warnings.append('A 10-K amendment exists. Imported figures are from the original 10-K; review the linked amendment before relying on the worksheet.')
        path=self.cache_dir/f"{cik}-{latest['accessionNumber']}.html";download_error=None;size=0
        try:
            if not path.exists():
                content=self.request(url,html=True)
                if not isinstance(content,bytes) or not content:raise SECError('SEC returned an empty filing document.')
                if b'Your Request Originates from an Undeclared Automated Tool' in content[:20000]:raise SECError('SEC blocked the filing download as an unidentified automated request.')
                self.cache_dir.mkdir(parents=True,exist_ok=True);temp=path.with_suffix('.tmp');temp.write_bytes(content);temp.replace(path)
            size=path.stat().st_size
        except (SECError,OSError) as exc:download_error=str(exc) if isinstance(exc,SECError) else 'The local SEC filing cache could not be written.'
        return {'status':'loaded' if document else 'partial','symbol':symbol,'cik':cik,'company':submission.get('name'),
            'filing':{'form':'10-K','accession':latest['accessionNumber'],'filed_at':latest['filingDate'],'period_end':latest['reportDate'],'url':url},
            'amendments':[{'filed_at':f['filingDate'],'url':filing_url(cik,f)} for f in amendments],
            'fetched_at':datetime.now(timezone.utc).isoformat(),'document':document,'provenance':provenance,'warnings':warnings,
            'download_available':bool(size),'download_bytes':size,'download_error':download_error,
            'message':'SEC 10-K retrieved. Standard annual facts are imported where available; review remaining gaps and assumptions.'}
    def document_path(self,symbol):
        report=self.report(symbol)
        if not report.get('download_available'):raise SECError(report.get('download_error') or 'No downloaded SEC 10-K is available for this symbol.')
        return self.cache_dir/f"{report['cik']}-{report['filing']['accession']}.html"

def effective_document(state,symbol):
    manual=state.get('equity_financials',{}).get(symbol)
    return manual or state.get('equity_sec',{}).get(symbol,{}).get('document')
