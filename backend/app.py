from pathlib import Path
from contextlib import asynccontextmanager
from importlib.util import find_spec
from typing import Literal
from uuid import uuid4
import os
from fastapi import FastAPI, HTTPException, Request, Query, Response
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .data import DemoProvider, AS_OF, ASSET_CLASSES, curves
from .engine import summarize, research, check_order, backtest
from .store import Store, now
from .risk import portfolio_risk
from .rebalance import preview as rebalance_preview, recommendation, validate_targets, fingerprint, settle
from .alpaca import AlpacaService, DEFAULT_SYMBOLS, symbols_from
from .equity_analysis import score_financials, validate_financials, valuation, implementation, PAPERS
from .sec import SECService, SECError, effective_document
from .nasdaq import NasdaqService
from .twelvedata import TwelveDataService, TwelveDataError, DEFAULT_PAIRS, pairs_from, pair_from
from .yahoo_fx import YahooFXService
from .fx_sources import FXSources
from .fx_carry_risk import FXCarryRiskService
from .us_rates import USRatesService
from .fx_options import FXOptionsService
from .config import load_backend_env, project_id
from .credentials import twelvedata_status
from .bop_portability import inspect_views
from .bop import BOPService, BOPError
from .bop_macro import BOPMacroService
from .bop_japan import JapanBOPService
from .bop_china import ChinaBOPService
from .bop_uk import UKBOPService
from .bop_singapore import SingaporeBOPService
from .bop_australia import AustraliaBOPService
from .bop_new_zealand import NewZealandBOPService
from .bop_switzerland import SwitzerlandBOPService
from .bop_canada import CanadaBOPService
from .telegram_news import TelegramNewsService
from .tradays_focus import TradaysFocusService
from .treasury_yields import TreasuryYieldService
from .sovereign_yields import SovereignYields
from datetime import datetime, timezone
from copy import deepcopy

class ResearchRequest(BaseModel):
    asset_class: Literal['bonds','rates','fx','equities','commodities']
    question: str = Field(min_length=5,max_length=2000)
    horizon: Literal['1M','3M']='1M'
class ProposalRequest(BaseModel):
    run_id: str
    instrument_id: str
class HaltRequest(BaseModel): halted: bool
class LimitsRequest(BaseModel):
    max_position: float=Field(ge=.05,le=.5)
    max_gross: float=Field(ge=.1,le=1)
    order_budget: float=Field(ge=.005,le=.1)
class BacktestRequest(BaseModel): instrument_id: str
class WatchRequest(BaseModel): instrument_id: str
class RebalanceRequest(BaseModel):
    targets: dict[str,float]
    strength: float=Field(default=1,ge=0,le=1)
    confidence: Literal[.95,.99]=.95
    lookback: Literal[63,126,252]=252
    benchmark: Literal['balanced','SPX','IG']='balanced'
    baseline_fingerprint: str|None=Field(default=None,max_length=100)
    request_id: str|None=Field(default=None,max_length=80)
class RebalancePolicyRequest(BaseModel):
    targets: dict[str,float]
    band: float=Field(default=.02,ge=.001,le=.1)
    pace: float=Field(default=.5,ge=.1,le=1)
    max_turnover: float=Field(default=.1,ge=.01,le=1)
    min_trade_weight: float=Field(default=.0005,ge=0,le=.01)
class FinancialRequest(BaseModel): document: dict
class ValuationRequest(BaseModel):
    wacc: float=Field(default=.09,ge=.01,le=.4)
    tax: float=Field(default=.21,ge=0,le=.6)
    forward_roic: float=Field(default=.15,ge=-.5,le=1)
    growth: float=Field(default=.03,ge=-.2,le=.4)
    cap: float=Field(default=10,ge=0,le=50)
class ImplementationRequest(BaseModel):
    portfolio_value: float=Field(default=1000000,gt=0,le=1e12)
    current_shares: int=Field(default=0,ge=0,le=1000000000)
    target_weight: float=Field(default=.05,ge=0,le=1)
    buffer: float=Field(default=.01,ge=0,le=.2)
    fee_bps: float=Field(default=1,ge=0,le=1000)
    impact_bps: float=Field(default=5,ge=0,le=1000)
    cost_basis: float|None=Field(default=None,ge=0)
    tax_rate: float|None=Field(default=None,ge=0,le=1)

def create_app(db_path=None):
    if db_path is None:load_backend_env()
    @asynccontextmanager
    async def lifespan(application):
        source=application.state.fx_carry_risk.volatility_source
        inflation=application.state.us_rates.swap_source
        options=application.state.fx_options
        if source: source.start()
        if inflation: inflation.start()
        options.start()
        try: yield
        finally:
            if source: source.close()
            if inflation: inflation.close()
            options.close()
    app=FastAPI(title='Meridian Investment Workbench',version='0.1.0',lifespan=lifespan)
    provider=DemoProvider();store=Store(db_path or os.environ.get('WORKBENCH_DB',str(Path(__file__).parent.parent/'data'/'workbench.sqlite3')))
    app.state.store=store;app.state.provider=provider
    equities=AlpacaService(enabled=db_path is None and os.environ.get('ALPACA_ENABLED','true').lower()!='false')
    app.state.equities=equities
    sec=SECService(Path(store.path).parent/'sec-cache',enabled=db_path is None and os.environ.get('SEC_ENABLED','true').lower()!='false')
    app.state.sec=sec
    nasdaq=NasdaqService(enabled=db_path is None and os.environ.get('NASDAQ_DATA_LINK_ENABLED','true').lower()!='false')
    app.state.nasdaq=nasdaq
    fx=TwelveDataService(enabled=db_path is None and os.environ.get('TWELVEDATA_ENABLED','true').lower()!='false')
    app.state.fx=fx
    yahoo=YahooFXService(Path(store.path).parent/'yahoo-cache',enabled=db_path is None and os.environ.get('YAHOO_FX_ENABLED','true').lower()!='false')
    app.state.yahoo_fx=yahoo
    fx_sources=FXSources(yahoo,fx,primary=os.environ.get('FX_PRIMARY_PROVIDER','yahoo'))
    app.state.fx_sources=fx_sources
    fx_carry_risk=FXCarryRiskService(Path(store.path).parent/'fx-carry-risk.json',enabled=db_path is None)
    app.state.fx_carry_risk=fx_carry_risk
    fx_options=FXOptionsService(Path(store.path).parent/'fx-option-trades.json',fx_carry_risk,
                               enabled=db_path is None and os.environ.get('FX_OPTIONS_ENABLED','true').lower()=='true')
    app.state.fx_options=fx_options
    us_rates=USRatesService(Path(store.path).parent/'us-rates.json',enabled=db_path is None and os.environ.get('RATES_ENABLED','true').lower()=='true')
    app.state.us_rates=us_rates
    bop=BOPService(Path(store.path).parent/'bop-cache'/'usa.json',enabled=db_path is None and os.environ.get('BOP_ENABLED','true').lower()!='false')
    app.state.bop=bop
    bop_macro=BOPMacroService(Path(store.path).parent/'bop-cache'/'usa-macro.json',enabled=db_path is None and os.environ.get('BOP_ENABLED','true').lower()!='false')
    app.state.bop_macro=bop_macro
    bop_japan=JapanBOPService(Path(store.path).parent/'bop-cache'/'japan.json',enabled=db_path is None and os.environ.get('BOP_ENABLED','true').lower()!='false')
    app.state.bop_japan=bop_japan
    bop_china=ChinaBOPService(Path(store.path).parent/'bop-cache'/'china.json',enabled=db_path is None and os.environ.get('BOP_ENABLED','true').lower()!='false')
    app.state.bop_china=bop_china
    bop_uk=UKBOPService(Path(store.path).parent/'bop-cache'/'uk.json',enabled=db_path is None and os.environ.get('BOP_ENABLED','true').lower()!='false')
    app.state.bop_uk=bop_uk
    bop_singapore=SingaporeBOPService(Path(store.path).parent/'bop-cache'/'singapore.json',enabled=db_path is None and os.environ.get('BOP_ENABLED','true').lower()!='false')
    app.state.bop_singapore=bop_singapore
    bop_australia=AustraliaBOPService(Path(store.path).parent/'bop-cache'/'australia.json',enabled=db_path is None and os.environ.get('BOP_ENABLED','true').lower()!='false')
    app.state.bop_australia=bop_australia
    bop_new_zealand=NewZealandBOPService(Path(store.path).parent/'bop-cache'/'new-zealand.json',enabled=db_path is None and os.environ.get('BOP_ENABLED','true').lower()!='false')
    app.state.bop_new_zealand=bop_new_zealand
    bop_switzerland=SwitzerlandBOPService(Path(store.path).parent/'bop-cache'/'switzerland.json',enabled=db_path is None and os.environ.get('BOP_ENABLED','true').lower()!='false')
    app.state.bop_switzerland=bop_switzerland
    bop_canada=CanadaBOPService(Path(store.path).parent/'bop-cache'/'canada.json',enabled=db_path is None and os.environ.get('BOP_ENABLED','true').lower()!='false')
    app.state.bop_canada=bop_canada
    bop_services={'USA':bop,'CHN':bop_china,'JPN':bop_japan,'GBR':bop_uk,'CAN':bop_canada,
                  'AUS':bop_australia,'NZL':bop_new_zealand,'CHE':bop_switzerland,'SGP':bop_singapore}
    def bop_view(country,frequency,refresh):
        result=bop_services[country].view(frequency=frequency,refresh=refresh)
        if country=='USA' and result['status']=='ready':result['macro']=bop_macro.view(bop.snapshot['series'],refresh,frequency=frequency)
        return result
    news=TelegramNewsService(Path(store.path).parent/'telegram-news.json',enabled=db_path is None and os.environ.get('TELEGRAM_NEWS_ENABLED','true').lower()!='false')
    app.state.telegram_news=news
    calendar_focus=TradaysFocusService(enabled=db_path is None)
    app.state.tradays_focus=calendar_focus
    treasury_yields=TreasuryYieldService(Path(store.path).parent/'treasury-yields.json',enabled=db_path is None and os.environ.get('BONDS_ENABLED','true').lower()!='false')
    app.state.treasury_yields=treasury_yields
    sovereign_yields=SovereignYields(treasury_yields,Path(store.path).parent/'bond-countries',enabled=db_path is None and os.environ.get('BONDS_ENABLED','true').lower()!='false')
    app.state.sovereign_yields=sovereign_yields
    @app.exception_handler(TwelveDataError)
    async def fx_error(_request,exc):
        return JSONResponse(status_code=exc.code,content={'detail':str(exc),'retry_after_seconds':exc.retry_after},headers={'Retry-After':str(exc.retry_after)} if exc.retry_after else None)
    @app.exception_handler(ValueError)
    async def value_error(_request,exc): return JSONResponse(status_code=409,content={'detail':str(exc)})
    @app.middleware('http')
    async def local_mutation_guard(request:Request,call_next):
        origin=request.headers.get('origin')
        if request.method not in ('GET','HEAD','OPTIONS') and origin and origin not in (f'http://{request.headers.get("host")}', 'http://localhost:5173','http://127.0.0.1:5173'):
            return JSONResponse(status_code=403,content={'detail':'Cross-origin writes are not permitted.'})
        response=await call_next(request);response.headers['X-Content-Type-Options']='nosniff';return response
    @app.get('/api/health')
    def health(): return {'status':'ok','mode':'synthetic-paper','snapshot':AS_OF,'process_id':os.getpid(),'project_id':project_id()}
    @app.get('/api/fx/status')
    def fx_status(response:Response):
        response.headers['Cache-Control']='no-store'
        return {'primary_provider':fx_sources.primary,'yahoo':{'enabled':yahoo.enabled,'package_present':find_spec('yfinance') is not None,'api_key_required':False},
                'twelvedata':{'enabled':fx.enabled,**twelvedata_status()},'providers':fx_sources.providers()}
    @app.get('/api/news/telegram')
    def telegram_news(response:Response):
        response.headers['Cache-Control']='no-store'
        return news.view()
    @app.get('/api/news/telegram/history')
    def telegram_news_history(response:Response,before:int=Query(gt=0,lt=1_000_000_000_000)):
        response.headers['Cache-Control']='no-store'
        return news.load_history(before)
    @app.get('/api/calendar/tradays/focus')
    def tradays_focus(response:Response):
        response.headers['Cache-Control']='no-store'
        return calendar_focus.view()
    @app.get('/api/bonds/treasury')
    def treasury_market(response:Response,refresh:bool=False,country:Literal['USA','CHN','JPN','GBR','CAN','SGP']='USA',saved_only:bool=False):
        response.headers['Cache-Control']='no-store'
        return sovereign_yields.view(country,refresh=refresh,saved_only=saved_only)
    @app.get('/api/fx/market')
    def fx_market(pairs:str=','.join(DEFAULT_PAIRS)):return fx_sources.market(pairs_from(pairs))
    @app.get('/api/rates/us')
    def us_rates_market(response:Response,refresh:bool=False,saved_only:bool=False):
        response.headers['Cache-Control']='no-store'
        return us_rates.view(refresh=refresh,saved_only=saved_only)
    @app.get('/api/fx/options/heatmap')
    def fx_options_heatmap(response:Response,pair:str='EUR/USD',window_days:int=30,refresh:bool=False,saved_only:bool=False):
        response.headers['Cache-Control']='no-store'
        return fx_options.view(pair,window_days,refresh=refresh,saved_only=saved_only)
    @app.get('/api/fx/carry-risk')
    def fx_carry_risk_view(response:Response,weeks:int=Query(default=52,ge=26,le=104),refresh:bool=False,saved_only:bool=False):
        if weeks not in (26,52,104):raise HTTPException(422,'Choose a 26, 52 or 104 week correlation window.')
        response.headers['Cache-Control']='no-store'
        return fx_carry_risk.view(weeks=weeks,refresh=refresh,saved_only=saved_only)
    @app.get('/api/fx/search')
    def fx_search(q:str=Query(default='',max_length=100),offset:int=Query(default=0,ge=0),limit:int=Query(default=25,ge=1,le=100)):
        return fx_sources.search(q,offset,limit)
    @app.get('/api/fx/history')
    def fx_history(pair:str='EUR/USD',interval:Literal['1day','1h','5min']='1day'):
        return fx_sources.history(pair_from(pair),interval)
    @app.get('/api/fx/bop')
    def fx_bop(response:Response,country:str=Query(default='USA',max_length=3),frequency:Literal['quarterly','trailing4q']='trailing4q',refresh:bool=True):
        response.headers['Cache-Control']='no-store'
        try:
            return bop_view(country.upper(),frequency,refresh)
        except KeyError:raise HTTPException(404,'Unknown BOP country.')
        except BOPError as exc:raise HTTPException(503,str(exc))
    @app.get('/api/fx/bop/status')
    def bop_status(response:Response):
        response.headers['Cache-Control']='no-store'
        countries=[]
        for code,service in bop_services.items():
            try:
                report=inspect_views(code,{frequency:bop_view(code,frequency,False) for frequency in ('quarterly','trailing4q')})
                countries.append({'code':code,'saved_charts_ready':report['status']=='passed','refresh_enabled':service.enabled,
                                  'chart_count':7,'source_count':len(report['sources'])})
            except (OSError,ValueError,TypeError,KeyError,BOPError):
                countries.append({'code':code,'saved_charts_ready':False,'refresh_enabled':service.enabled,'chart_count':0})
        return {'public_sources_require_key':False,'auto_check_seconds':300,'check':'local_chart_readiness_only',
                'countries':countries,'live_refresh_enabled':all(service.enabled for service in (*bop_services.values(),bop_macro))}
    @app.get('/api/equities/market')
    def equity_market(symbols: str=','.join(DEFAULT_SYMBOLS)):
        market=deepcopy(equities.market(symbols_from(symbols)))
        state=store.read()
        for row in market['rows']:
            document=effective_document(state,row['symbol'])
            row['financials']=score_financials(document,row['price'])
            row['financial_source']=document.get('source') if document else None
            row['filed_at']=document.get('filed_at') if document else None
            row['financial_origin']='manual' if state.get('equity_financials',{}).get(row['symbol']) else 'sec' if document else None
            row['value_momentum_percentile']=None
        complete=[r for r in market['rows'] if r['kind']=='Stock' and r['momentum_12_1'] is not None and (r['financials']['book_to_market'] or 0)>0]
        if len(complete)>=3:
            def rank(value,values):return 100*(sum(v<value for v in values)+.5*sum(v==value for v in values))/len(values)
            for row in complete:
                row['value_momentum_percentile']=.5*rank(row['financials']['book_to_market'],[r['financials']['book_to_market'] for r in complete])+.5*rank(row['momentum_12_1'],[r['momentum_12_1'] for r in complete])
        market['papers']=PAPERS
        market['blend_note']='Equal-weight value/momentum percentile blend within the complete-input stock subset; at least three positive-book-equity stocks are required. Not a forecast or paper replication.'
        return market
    @app.get('/api/equities/search')
    def search_equities(q:str=Query(default='',max_length=100),offset:int=Query(default=0,ge=0),limit:int=Query(default=25,ge=1,le=100)):
        return equities.search(q,offset,limit)
    @app.get('/api/equities/financials/{symbol}')
    def equity_financials(symbol:str):
        symbol=symbols_from(symbol)[0]
        state=store.read()
        return {'symbol':symbol,'document':effective_document(state,symbol),'sec_report':state.get('equity_sec',{}).get(symbol),
            'nasdaq_report':state.get('equity_nasdaq',{}).get(symbol),'sec_error':state.get('equity_sec_errors',{}).get(symbol),
            'origin':'manual' if state.get('equity_financials',{}).get(symbol) else 'sec' if effective_document(state,symbol) else None,'papers':PAPERS}
    @app.post('/api/equities/reports/{symbol}/sync')
    def sync_annual_reports(symbol:str,refresh:bool=False):
        symbol=symbols_from(symbol)[0]
        if refresh:
            with sec.lock:sec.cache.pop(('report',symbol),None)
        sec_error=None;fallback=None
        try:report=sec.report(symbol)
        except SECError as exc:
            sec_error=str(exc);report={'status':'error','message':sec_error,'document':None}
        if not report.get('document') or not report.get('download_available'):
            fallback=nasdaq.report(symbol,refresh=refresh)
            # A second, exact company identifier can recover SEC directory gaps.
            # It cannot bypass a denied SEC request or replace filing text.
            if fallback.get('cik') and not report.get('filing'):
                try:
                    recovered=sec.report_for_cik(symbol,fallback['cik'])
                    if recovered.get('filing'):report=recovered;sec_error=None
                except SECError as exc:fallback.setdefault('warnings',[]).append(f'SEC retrieval using the Nasdaq reference failed: {exc}')
        with store.transaction() as (db,state):
            # Retain the last usable SEC import during source outages. The
            # attempt error is separate so retained data cannot look like success.
            retained=False
            old=state.get('equity_sec',{}).get(symbol)
            if (sec_error or not report.get('document')) and old and old.get('document'):
                retained=True;sec_error=sec_error or report.get('message','No new annual financial import available.');report=old
            changed=old!=report or state.get('equity_nasdaq',{}).get(symbol)!=fallback or state.get('equity_sec_errors',{}).get(symbol)!=sec_error
            state.setdefault('equity_sec',{})[symbol]=report
            state.setdefault('equity_nasdaq',{})[symbol]=fallback
            state.setdefault('equity_sec_errors',{})[symbol]=sec_error
            if changed:store.event(db,'equity.annual_reports_synced',{'symbol':symbol,'sec_status':report['status'],
                'sec_error':sec_error,'retained_sec_report':retained,'nasdaq_status':fallback.get('status') if fallback else 'not_needed',
                'accession':report.get('filing',{}).get('accession')})
            document=effective_document(state,symbol)
            origin='manual' if state.get('equity_financials',{}).get(symbol) else 'sec' if document else None
        return {'symbol':symbol,'sec_report':report,'sec_error':sec_error,'nasdaq_report':fallback,
            'retained_sec_report':retained,'document':document,'origin':origin}
    @app.post('/api/equities/sec/{symbol}/sync')
    def sync_sec_financials(symbol:str,refresh:bool=False):
        symbol=symbols_from(symbol)[0]
        if refresh:
            with sec.lock:sec.cache.pop(('report',symbol),None)
        report=sec.report(symbol)
        with store.transaction() as (db,state):
            if state.get('equity_sec',{}).get(symbol)!=report:
                state.setdefault('equity_sec',{})[symbol]=report
                store.event(db,'equity.sec_synced',{'symbol':symbol,'cik':report.get('cik'),'accession':report.get('filing',{}).get('accession'),
                    'status':report['status'],'input_type':'sec_xbrl','download_available':report.get('download_available',False)})
            document=effective_document(state,symbol)
            origin='manual' if state.get('equity_financials',{}).get(symbol) else 'sec' if document else None
        return {'symbol':symbol,'sec_report':report,'document':document,'origin':origin}
    @app.get('/api/equities/sec/{symbol}/document')
    def download_sec_filing(symbol:str):
        symbol=symbols_from(symbol)[0];path=sec.document_path(symbol)
        return FileResponse(path,filename=f'{symbol}-{path.name}',media_type='application/octet-stream',
            headers={'Content-Security-Policy':"sandbox; default-src 'none'",'X-Content-Type-Options':'nosniff'})
    @app.put('/api/equities/financials/{symbol}')
    def save_equity_financials(symbol:str,body:FinancialRequest):
        symbol=symbols_from(symbol)[0]
        document=validate_financials(body.document,datetime.now(timezone.utc).date())
        with store.transaction() as (db,state):
            state.setdefault('equity_financials',{})[symbol]=document
            store.event(db,'equity.financials_saved',{'symbol':symbol,'source':document['source'],'filed_at':document['filed_at'],'input_type':'user_supplied'})
        return {'symbol':symbol,'document':document,'financials':score_financials(document)}
    @app.post('/api/equities/{symbol}/valuation')
    def equity_valuation(symbol:str,body:ValuationRequest):
        symbol=symbols_from(symbol)[0];row=equities.market([symbol])['rows'][0]
        if not row['price']:raise ValueError('No sourced market price is available.')
        document=effective_document(store.read(),symbol)
        return {**valuation(document,row['price'],**body.model_dump()),'market_price':row['price'],'price_time':row['trade_time'],'calculated_at':now()}
    @app.post('/api/equities/{symbol}/implementation')
    def equity_implementation(symbol:str,body:ImplementationRequest):
        symbol=symbols_from(symbol)[0];row=equities.market([symbol])['rows'][0]
        return {**implementation(row,**body.model_dump()),'market_price':row['price'],'price_time':row['trade_time'],'calculated_at':now()}
    @app.get('/api/portfolio/risk')
    def risk_metrics(confidence: Literal['0.95','0.99']='0.95', lookback: int=Query(default=252,ge=63,le=252), benchmark: Literal['balanced','SPX','IG']='balanced'):
        state=store.read()
        store.reconcile(state,provider)
        return portfolio_risk(state,provider,float(confidence),lookback,benchmark)
    @app.get('/api/rebalance/policy')
    def rebalance_policy():
        state=store.read();store.reconcile(state,provider)
        return recommendation(state,provider)
    @app.put('/api/rebalance/policy')
    def save_rebalance_policy(body:RebalancePolicyRequest):
        with store.transaction() as (db,state):
            validate_targets(body.targets,state,provider)
            state['rebalance_policy']={**body.model_dump(),'saved':True}
            store.event(db,'rebalance.policy_updated',state['rebalance_policy'])
        return recommendation(state,provider)
    @app.post('/api/rebalance/preview')
    def preview_rebalance(body:RebalanceRequest):
        state=store.read();store.reconcile(state,provider)
        return rebalance_preview(state,provider,body.targets,body.strength,body.confidence,body.lookback,body.benchmark)
    @app.post('/api/rebalance/execute')
    def execute_rebalance(body:RebalanceRequest):
        if not body.request_id or not body.baseline_fingerprint: raise ValueError('A reviewed preview and an idempotency key are required.')
        with store.transaction() as (db,state):
            previous=next((p for p in state.get('rebalances',[]) if p['request_id']==body.request_id),None)
            if previous:
                if previous.get('request')!=body.model_dump(): raise ValueError('This idempotency key belongs to a different rebalance request.')
                return previous
            store.reconcile(state,provider)
            if fingerprint(state,provider)!=body.baseline_fingerprint: raise ValueError('Portfolio, prices, policy or limits changed. Refresh and review a new preview before approval.')
            plan=rebalance_preview(state,provider,body.targets,body.strength,body.confidence,body.lookback,body.benchmark)
            if not plan['can_execute']:raise ValueError('; '.join(plan['violations']) or 'This scenario has no executable trades after lot rounding.')
            batch_id=str(uuid4());fills=[]
            for item in plan['orders']:
                i=provider.instrument(item['instrument_id'])
                risk=check_order(state,provider,i,item['side'],item['quantity'],item['fill_price'])
                settle(state,i,item['side'],item['quantity'],item['fill_price'],item['fee'])
                order={**item,'id':str(uuid4()),'run_id':f'rebalance:{batch_id}','status':'filled','snapshot':AS_OF,
                    'created_at':now(),'executed_at':now(),'risk':risk,'reason':'User-approved paper rebalance basket.'}
                state['orders'].insert(0,order);fills.append(order)
                store.event(db,'order.approved',{'order_id':order['id'],'batch_id':batch_id,'mode':'paper'})
                store.event(db,'order.filled',order)
            result={'id':batch_id,'request_id':body.request_id,'request':body.model_dump(),'status':'filled','fills':fills,'cost':plan['estimated_cost'],'created_at':now()}
            state.setdefault('rebalances',[]).insert(0,result)
            store.event(db,'rebalance.completed',result)
        return result
    @app.get('/api/workspace')
    def workspace():
        state=store.read();events=store.audit()
        return {**state,'portfolio':summarize(state,provider),'instruments':provider.instruments(),'curve':curves(),
          'events':list(reversed(events))[:100],'ledger_verified':True,'snapshot':AS_OF,'mode':'synthetic-paper',
          'sources':[{'id':'demo-market','name':'Meridian fixture provider','status':'Demo','coverage':ASSET_CLASSES,'observations':len(provider.records)*260,'vintage':'demo-v1','as_of':AS_OF,'quality':'Synthetic','capabilities':['quotes','daily bars','reference terms','illustrative curves']}],
          'capabilities':{'llm':'Not connected; deterministic specialists','live_execution':False,'paper_execution':'USD cash bonds and selected equities','storage':'SQLite WAL + hash-linked audit journal'}}
    @app.get('/api/instruments/{key}/history')
    def history(key:str):
        if not provider.instrument(key): raise HTTPException(404,'Instrument not found')
        return {'instrument_id':key,'source_id':'demo-market','vintage':'demo-v1','series':provider.series(key)}
    @app.post('/api/research')
    def run_research(body:ResearchRequest):
        signals=research(provider,body.asset_class,body.question,body.horizon)
        run={'id':str(uuid4()),'created_at':now(),'asset_class':body.asset_class,'question':body.question,'horizon':body.horizon,
             'status':'completed','engine':'deterministic-demo','as_of':AS_OF,'signals':signals,
             'stages':[{'name':'Director','detail':f'Selected {len(signals)} {body.asset_class} instruments and a {body.horizon} observation window.'},
               {'name':'Quant specialist','detail':'Computed trailing price change and annualized realized volatility from versioned fixture bars.'},
               {'name':'Risk challenger','detail':'Scores are uncalibrated directional measures. Tradability, concentration and cash are checked separately before paper execution.'}]}
        with store.transaction() as (db,state):
            state['runs'].insert(0,run);store.event(db,'research.completed',{'run_id':run['id'],'model':'momentum-risk-v1','snapshot':AS_OF,'signal_count':len(signals)})
        return run
    @app.post('/api/orders')
    def propose(body:ProposalRequest):
        store.audit()
        with store.transaction() as (db,state):
            store.reconcile(state,provider)
            run=next((r for r in state['runs'] if r['id']==body.run_id),None)
            if not run: raise HTTPException(404,'Research run not found')
            signal=next((s for s in run['signals'] if s['instrument_id']==body.instrument_id),None)
            if not signal: raise HTTPException(404,'Signal not found in research run')
            duplicate=next((o for o in state['orders'] if o['run_id']==body.run_id and o['instrument_id']==body.instrument_id and o['status']=='proposed'),None)
            if duplicate: return duplicate
            if signal['direction']=='neutral': raise ValueError('A neutral signal does not propose a trade.')
            i=provider.instrument(body.instrument_id);side='buy' if signal['direction']=='bullish' else 'sell'
            portfolio=summarize(state,provider);step=i['quantity_step']
            budget=portfolio['nav']*state['limits']['order_budget']
            qty=int(budget/(i['price']*i['multiplier'])//step)*step
            if side=='sell': qty=min(qty,state['positions'].get(i['id'],0))
            risk=check_order(state,provider,i,side,qty,i['price'])
            order={'id':str(uuid4()),'run_id':run['id'],'instrument_id':i['id'],'symbol':i['symbol'],'side':side,'quantity':qty,
              'reference_price':i['price'],'status':'proposed','created_at':now(),'snapshot':AS_OF,'risk':risk,'reason':signal['reasoning']}
            state['orders'].insert(0,order);store.event(db,'order.proposed',order)
            return order
    @app.post('/api/orders/{order_id}/execute')
    def execute(order_id:str):
        store.audit()
        with store.transaction() as (db,state):
            store.reconcile(state,provider)
            o=next((o for o in state['orders'] if o['id']==order_id),None)
            if not o: raise HTTPException(404,'Order not found')
            if o['status']=='filled': return o # idempotent retry
            if o['status']!='proposed': raise ValueError('Only proposed orders can be approved.')
            if o['snapshot']!=AS_OF: raise ValueError('The proposal snapshot has expired; create fresh research.')
            i=provider.instrument(o['instrument_id']);fill=round(i['price']*(1.0005 if o['side']=='buy' else .9995),6)
            risk=check_order(state,provider,i,o['side'],o['quantity'],fill)
            old_qty=state['positions'].get(i['id'],0);old_cost=state['costs'].get(i['id'],fill)
            if o['side']=='buy': state['costs'][i['id']]=(old_qty*old_cost+o['quantity']*fill)/(old_qty+o['quantity'])
            else: state['realized_pnl']+=(fill-old_cost)*o['quantity']*i['multiplier']
            state['realized_pnl']-=risk['fee'];state['positions'][i['id']]=risk['quantity_after'];state['cash']=risk['cash_after']
            o.update(status='filled',fill_price=fill,fee=risk['fee'],executed_at=now(),risk=risk)
            store.event(db,'order.approved',{'order_id':o['id'],'mode':'paper'})
            store.event(db,'order.filled',o)
            return o
    @app.post('/api/orders/{order_id}/cancel')
    def cancel(order_id:str):
        with store.transaction() as (db,state):
            o=next((o for o in state['orders'] if o['id']==order_id),None)
            if not o: raise HTTPException(404,'Order not found')
            if o['status']!='proposed': raise ValueError('Only a proposed order can be cancelled.')
            o['status']='cancelled';store.event(db,'order.cancelled',{'order_id':order_id});return o
    @app.post('/api/portfolio/halt')
    def halt(body:HaltRequest):
        with store.transaction() as (db,state):
            state['halted']=body.halted;store.event(db,'portfolio.halted' if body.halted else 'portfolio.resumed',{'halted':body.halted})
        return {'halted':body.halted}
    @app.put('/api/limits')
    def limits(body:LimitsRequest):
        with store.transaction() as (db,state):
            state['limits']=body.model_dump();store.event(db,'mandate.updated',state['limits'])
        return body
    @app.post('/api/backtests')
    def replay(body:BacktestRequest):
        result=backtest(provider,body.instrument_id);result.update(id=str(uuid4()),created_at=now())
        with store.transaction() as (db,state):
            state['backtests'].insert(0,result);store.event(db,'backtest.completed',{'id':result['id'],'instrument_id':body.instrument_id,'snapshot':AS_OF})
        return result
    @app.post('/api/watchlist')
    def watch(body:WatchRequest):
        if not provider.instrument(body.instrument_id): raise HTTPException(404,'Instrument not found')
        with store.transaction() as (db,state):
            if body.instrument_id in state['watchlist']: state['watchlist'].remove(body.instrument_id)
            else: state['watchlist'].append(body.instrument_id)
        return {'ok':True}
    @app.post('/api/data/validate')
    def validate_data():
        records=provider.instruments()
        if len({i['id'] for i in records})!=len(records): raise ValueError('Duplicate canonical identifiers.')
        for i in records:
            if i['available_at']>AS_OF or not i['unit'] or not i['source_id']: raise ValueError('Observation metadata is invalid.')
            bars=provider.series(i['id'])
            if any(b['close']<=0 or b['open']<=0 for b in bars): raise ValueError('A bar contains an invalid mark.')
            if [b['date'] for b in bars]!=sorted({b['date'] for b in bars}): raise ValueError('Duplicate or out-of-order bars.')
        store.audit()
        checks=[f'{len(records)} unique identifiers','Availability timestamps valid','Positive ordered bars','Audit chain verified']
        with store.transaction() as (db,state): store.event(db,'data.validated',{'checks':checks,'vintage':'demo-v1'})
        return {'checks':checks}
    dist=Path(__file__).parent.parent/'dist'
    app.mount('/assets',StaticFiles(directory=dist/'assets',check_dir=False),name='assets')
    @app.get('/{path:path}')
    def frontend(path:str):
        if path.startswith('api/'): raise HTTPException(404,'API route not found')
        if not (dist/'index.html').exists(): raise HTTPException(404,'Frontend not built. Run npm run build or use the Vite dev server.')
        return FileResponse(dist/'index.html')
    return app

app=create_app()
