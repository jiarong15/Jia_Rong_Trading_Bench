"""BOP deployment checks using the same public adapters and chart contracts."""
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
import importlib
import math
from pathlib import Path
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from .bop import BOPError

ADAPTERS={
    'USA':('bop','BOPService'), 'CHN':('bop_china','ChinaBOPService'),
    'JPN':('bop_japan','JapanBOPService'), 'GBR':('bop_uk','UKBOPService'),
    'CAN':('bop_canada','CanadaBOPService'), 'AUS':('bop_australia','AustraliaBOPService'),
    'NZL':('bop_new_zealand','NewZealandBOPService'), 'CHE':('bop_switzerland','SwitzerlandBOPService'),
    'SGP':('bop_singapore','SingaporeBOPService'),
}


def failure_code(exc):
    if isinstance(exc,HTTPError):return 'http_'+str(exc.code)
    cause=exc.reason if isinstance(exc,URLError) else exc
    if isinstance(cause,ssl.SSLCertVerificationError):return 'certificate_error'
    if isinstance(cause,ssl.SSLError):return 'tls_error'
    if isinstance(cause,(TimeoutError,socket.timeout)):return 'timeout'
    if isinstance(cause,socket.gaierror):return 'dns_error'
    if isinstance(cause,(ConnectionError,OSError)):return 'network_error'
    if isinstance(cause,HTTPException):return 'http_protocol_error'
    return 'configuration_or_source_validation_error'


def sources_for(view,code):
    sources=list(view.get('sources',[]))
    if code=='USA':
        sources=[{**item,'group':('accounts','customs')[index] if index<2 else f'unexpected_{index}'} for index,item in enumerate(sources)]
        sources.extend(view.get('macro',{}).get('sources',[]))
    return sources


def expected_sources(code):
    if code=='USA':return {'accounts','customs','quarterly','reserve_stock','monthly','daily','debt'}
    return set(importlib.import_module('backend.'+ADAPTERS[code][0]).GROUP_NAMES)


def chart_coverage(view,code,frequency):
    if view.get('country',{}).get('code')!=code or view.get('frequency')!=frequency or view.get('status')!='ready':
        raise BOPError('The BOP view returned the wrong country, frequency or readiness state.')
    if view.get('validation',{}).get('status')!='passed':raise BOPError('The BOP accounting checks did not pass.')
    macro=view.get('macro',{})
    if code!='USA' and macro.get('country_code')!=code:raise BOPError('Companion data returned a different country.')
    if macro.get('frequency')!=frequency or macro.get('status')!='ready':raise BOPError('BOP companion charts are incomplete.')
    investments=['net_direct','net_portfolio','net_other'] if macro.get('portfolio_split')=='combined' else ['net_direct','net_equity','net_debt','net_other']
    charts=[
        ('current_account',view.get('series',[]),['goods','services','primary_income','secondary_income','current_account']),
        ('customs_vs_bop',view.get('series',[]),['customs_goods','bop_goods_nsa' if code in ('GBR','AUS') else 'goods']),
        ('current_account_gdp_currency',macro.get('quarterly',[]),['current_account_gdp_pct','broad_dollar' if code=='USA' else 'currency_index']),
        ('investment',macro.get('quarterly',[]),investments),
        ('reserves',macro.get('quarterly',[]),['reserve_transactions','reserves_debt_pct']),
        ('trade',macro.get('trade',[]),['exports','imports','exports_ma3','imports_ma3','trade_balance']),
        ('trade_growth',macro.get('trade',[]),['exports_yoy','imports_yoy']),
    ]
    result=[]
    for name,rows,fields in charts:
        dates=[row['date'] for row in rows]
        if not dates or dates!=sorted(set(dates)):raise BOPError('A BOP chart has missing, duplicated or unordered dates.')
        counts={field:sum(isinstance(row.get(field),(int,float)) and not isinstance(row.get(field),bool) and math.isfinite(row[field]) for row in rows) for field in fields}
        if not all(counts.values()):raise BOPError('A plotted BOP series has no usable observations.')
        if any(row.get(field) is not None and (isinstance(row[field],bool) or not isinstance(row[field],(int,float)) or not math.isfinite(row[field])) for row in rows for field in fields):
            raise BOPError('A BOP chart contains an invalid numeric observation.')
        result.append({'chart':name,'plotted_observations':counts,'latest_date':dates[-1]})
    return result


def inspect_views(code,views,started=None,finished=None):
    if set(views)!={'quarterly','trailing4q'}:raise BOPError('Both BOP frequencies must be checked.')
    coverage={frequency:chart_coverage(view,code,frequency) for frequency,view in views.items()}
    view=views['quarterly'];expected=expected_sources(code)
    sources=sources_for(view,code);seen=[source.get('group') for source in sources]
    complete=set(seen)==expected and len(seen)==len(expected)
    checked=[]
    for source in sources:
        stamp=source.get('checked_at') or source.get('retrieved_at');fresh=False
        if started is not None:
            try:
                at=datetime.fromisoformat(stamp.replace('Z','+00:00'))
                fresh=at.tzinfo is not None and started-timedelta(seconds=2)<=at<=(finished or datetime.now(timezone.utc))+timedelta(seconds=2)
            except (AttributeError,TypeError,ValueError):pass
        checked.append({'group':source.get('group'),'host':urlsplit(source.get('url','')).hostname,'checked_at':stamp,
                        'fresh_download':fresh,'latest_observation':source.get('latest_observation')})
    warnings=[warning for warning in view.get('macro',{}).get('warnings',[]) if not warning.startswith("Treasury's table linked for ")]
    notes=[warning for warning in view.get('macro',{}).get('warnings',[]) if warning.startswith("Treasury's table linked for ")]
    live=started is not None
    passed=complete and (not live or (all(source['fresh_download'] for source in checked) and not view.get('refresh_error') and not warnings and view.get('refresh_available') is True))
    return {'country':code,'status':'passed' if passed else 'failed','check':'live_sources_and_charts' if live else 'saved_charts_only',
            'charts':coverage,'expected_source_count':len(expected),'sources':checked,'refresh_enabled':view.get('refresh_available'),
            'warnings':warnings,'data_notes':notes,'error_code':None if passed else 'incomplete_or_saved_sources'}


class CountryCheck:
    def __init__(self,code,cache_root,enabled=True):
        self.code=code;self.network_errors=[];cache_root=Path(cache_root)
        module_name,class_name=ADAPTERS[code];module=importlib.import_module('backend.'+module_name)
        self.service=getattr(module,class_name)(cache_root/(code.lower()+'.json'),enabled=enabled)
        self._observe(self.service)
        self.macro=None
        if code=='USA':
            from .bop_macro import BOPMacroService
            self.macro=BOPMacroService(cache_root/'usa-macro.json',enabled=enabled);self._observe(self.macro)

    def _observe(self,service):
        native=service.transport
        def download(url,*args):
            try:return native(url,*args)
            except (OSError,HTTPException,ValueError,BOPError) as exc:
                # Error bodies and query strings can contain FRED credentials.
                self.network_errors.append({'host':urlsplit(url).hostname,'error_code':failure_code(exc)})
                raise
        service.transport=download

    def view(self,frequency,refresh=False):
        view=self.service.view(frequency=frequency,refresh=refresh)
        if self.macro:view['macro']=self.macro.view(self.service.snapshot['series'],refresh=refresh,frequency=frequency)
        return view

    def run(self,live=True):
        started=datetime.now(timezone.utc)
        try:
            views={'quarterly':self.view('quarterly',refresh=live),'trailing4q':self.view('trailing4q')}
            report=inspect_views(self.code,views,started if live else None)
        except (OSError,HTTPException,ValueError,TypeError,KeyError,BOPError):
            report={'country':self.code,'status':'failed','check':'live_sources_and_charts' if live else 'saved_charts_only','error_code':'chart_or_source_validation_failed'}
        return {**report,'network_errors':self.network_errors}
