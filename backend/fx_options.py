"""Auto-updating DTCC FX trade heatmap with disclosed estimated sensitivities."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
from threading import Event, RLock, Thread
from time import monotonic

from .dtcc_inflation import BASE, public_bytes
from .fx_exact_feed import PAIRS, timestamp
from .fx_option_models import COLUMNS, DEFINITIONS, ROWS, build_cells, parse_fx_archive, resolve_trades, saxo_surface

POLL=60
BASIS='DTCC_FX_VANILLA_TRADE_FLOW_V1'
PATTERN=re.compile(r'CFTC_(CUMULATIVE|SLICE)_FOREX_(\d{4})_(\d{2})_(\d{2})(?:_(\d+))?\.zip')


def identity(name):
    match=PATTERN.fullmatch(name)
    if not match:
        raise ValueError('Unexpected DTCC FX file.')
    kind,y,m,d,sequence=match.groups()
    if (kind=='SLICE')!=(sequence is not None):
        raise ValueError('Invalid FX file kind.')
    return kind,date(int(y),int(m),int(d)).isoformat()


def index_files(body,kind,now):
    rows=json.loads(body)['CFTC_FX']
    if not isinstance(rows,list) or not 1<=len(rows)<=30000:
        raise ValueError('Invalid DTCC FX index.')
    files={}
    for item in rows:
        name=item['fileName']; actual,day=identity(name)
        if actual!=kind or day>now.date().isoformat() or name in files or type(item['rowCount']) is not int or not 0<=item['rowCount']<=500000:
            raise ValueError('Invalid FX index entry.')
        at=timestamp(item['dissemDTM'],now)
        if item.get('endTs'):
            at=max(at,timestamp(item['endTs'],now))
        files[name]={'day':day,'published_at':at.isoformat(),'rows':item['rowCount']}
    return files


class FXOptionTape:
    def __init__(self,path,transport=None,clock=None):
        self.path=Path(path); self.transport=transport or public_bytes
        self.clock=clock or (lambda:datetime.now(timezone.utc)); self.lock=RLock()
        self.bucket=None; self.files={}; self.trades=[]; self.checked=None; self.published=None
        self.error=None; self.pending_history=0; self.pending_live=0
        try:
            saved=json.loads(self.path.read_text(encoding='utf-8')); now=self.clock()
            if saved['basis']!=BASIS or saved['schema']!=1:
                raise ValueError('Invalid saved FX basis.')
            for name,file in saved['files'].items():
                _,day=identity(name)
                if day>now.date().isoformat() or not isinstance(file['events'],list):
                    raise ValueError('Invalid saved FX file.')
                timestamp(file['published_at'],now)
                for event in file['events']:
                    if event['action'] not in ('NEWT','CORR','EROR') or not event['id'].isdigit() or event['original'] and not event['original'].isdigit():
                        raise ValueError('Invalid saved lifecycle.')
                    timestamp(event['at'],now); trade=event['trade']
                    if trade is not None:
                        executed=timestamp(trade['executed_at'],now)
                        if (trade['pair'] not in PAIRS or trade['side'] not in ('call','put') or trade['date']!=executed.date().isoformat()
                                or date.fromisoformat(trade['expiry'])<executed.date()
                                or any(type(trade[key]) not in (int,float) or not math.isfinite(trade[key]) or trade[key]<=0 for key in ('strike','notional'))):
                            raise ValueError('Invalid saved FX trade.')
            self.files=saved['files']; self.trades=resolve_trades(self.files)
            self.checked=timestamp(saved['checked_at'],now).isoformat() if saved.get('checked_at') else None
            self.published=max((file['published_at'] for file in self.files.values()),default=None)
            self.pending_history=saved.get('pending_history',0); self.pending_live=saved.get('pending_live',0)
        except (OSError,ValueError,KeyError,TypeError,AttributeError):
            self.files={}; self.trades=[]; self.checked=None; self.published=None

    def download(self,name,metadata,now):
        kind,_=identity(name); route='cumulative' if kind=='CUMULATIVE' else 'intraday'
        body=self.transport(BASE+f'api/report/{route}/cftc/'+name)
        return {'published_at':metadata['published_at'],'events':parse_fx_archive(body,now,metadata['rows'])}

    def refresh(self,now):
        try:
            if not self.bucket:
                bucket=self.transport(BASE+'api/general/bucketname').decode().strip()
                if not re.fullmatch(r'kgc[0-9a-z-]{3,60}',bucket):
                    raise ValueError('Unexpected DTCC bucket.')
                self.bucket=bucket
            root=f'https://{self.bucket}.s3.amazonaws.com/dashboard/'
            with ThreadPoolExecutor(max_workers=2) as pool:
                daily_job=pool.submit(self.transport,root+'Cumulative.json'); slices_job=pool.submit(self.transport,root+'Slice.json')
                daily=index_files(daily_job.result(),'CUMULATIVE',now); slices=index_files(slices_job.result(),'SLICE',now)
            cutoff=(now.date()-timedelta(days=40)).isoformat()
            with self.lock:
                files=deepcopy(self.files)
            latest=max(daily,key=lambda name:daily[name]['day'])
            loaded=[identity(name)[1] for name in files if identity(name)[0]=='CUMULATIVE']
            if loaded and max(loaded)>daily[latest]['day']:
                raise ValueError('FX index regressed.')
            failed=[]
            def batch(names,index):
                with ThreadPoolExecutor(max_workers=6) as pool:
                    jobs={pool.submit(self.download,name,index[name],now):name for name in names}
                    for job in as_completed(jobs):
                        name=jobs[job]
                        try: files[name]=job.result()
                        except Exception: failed.append(name)
            # Process the latest complete day and intraday tape before older
            # history. Live observations are visible during background backfill.
            if latest not in files:
                batch([latest],daily)
            baseline=max((identity(name)[1] for name in files if identity(name)[0]=='CUMULATIVE'),default='1990-01-01')
            needed=[name for name in slices if slices[name]['day']>baseline and name not in files]
            needed.sort(key=lambda name:slices[name]['published_at'],reverse=True)
            batch(needed[:64],slices)
            self.publish(files,daily,slices,cutoff,baseline,failed,save=False)
            missing=[name for name in daily if daily[name]['day']>=cutoff and name not in files]
            missing.sort(key=lambda name:daily[name]['day'],reverse=True)
            batch(missing[:8],daily)
            self.publish(files,daily,slices,cutoff,baseline,failed,save=True)
        except Exception:
            with self.lock:
                self.error='DTCC FX source check failed. Saved trades retain their execution times; retrying automatically.'

    def publish(self,files,daily,slices,cutoff,baseline,failed,save):
        complete={identity(name)[1] for name in files if identity(name)[0]=='CUMULATIVE'}
        pruned={name:item for name,item in files.items() if identity(name)[1]>=cutoff and not(identity(name)[0]=='SLICE' and identity(name)[1] in complete)}
        trades=resolve_trades(pruned)
        with self.lock:
            self.files=pruned; self.trades=trades
            self.pending_history=sum(name not in pruned for name in daily if daily[name]['day']>=cutoff)
            self.pending_live=sum(name not in pruned for name in slices if slices[name]['day']>baseline)
            self.published=max((file['published_at'] for file in pruned.values()),default=None)
            if not failed:
                self.checked=self.clock().isoformat()
            self.error='Some DTCC FX files could not be downloaded; retrying automatically. Loaded trade activity may be incomplete.' if failed else None
            if save:
                try:
                    self.path.parent.mkdir(parents=True,exist_ok=True); temporary=self.path.with_suffix('.tmp')
                    temporary.write_text(json.dumps({'schema':1,'basis':BASIS,'files':pruned,'checked_at':self.checked,
                        'pending_history':self.pending_history,'pending_live':self.pending_live}),encoding='utf-8')
                    temporary.replace(self.path)
                except OSError:
                    self.error='FX trade data could not be saved locally. Current loaded observations remain visible.'

    def snapshot(self):
        with self.lock:
            return deepcopy({'trades':self.trades,'checked_at':self.checked,'published_at':self.published,'error':self.error,
                'pending_history_files':self.pending_history,'pending_live_files':self.pending_live})


class FXOptionsService:
    def __init__(self,path,carry_service,enabled=True,transport=None,clock=None,tape=None):
        self.clock=clock or (lambda:datetime.now(timezone.utc)); self.enabled=enabled; self.carry=carry_service
        self.tape=tape or FXOptionTape(path,transport=transport,clock=self.clock)
        self.lock=RLock(); self.stop=Event(); self.thread=None; self.inflight=False; self.attempt=None; self.input_error=None

    def refresh(self,force=False):
        now=self.clock()
        with self.lock:
            if not self.enabled or self.inflight or not force and self.attempt and (now-self.attempt).total_seconds()<POLL:
                return
            self.attempt=now; self.inflight=True
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                tape_job=pool.submit(self.tape.refresh,now)
                inputs_job=pool.submit(self.carry.view)
                tape_job.result()
                try: inputs_job.result(); self.input_error=None
                except Exception: self.input_error='Market-input refresh failed; source-dated saved inputs are used when available.'
        finally:
            with self.lock: self.inflight=False

    def inputs(self,now,pair):
        with self.carry.lock:
            forward=self.carry.forward_source.snapshot(now) if self.carry.forward_source else {'quotes':{},'errors':{}}
            source=self.carry.volatility_source
            if source:
                source.snapshot(now)
                with source.report.lock:
                    body=source.report.raw; report_error=source.report.error
            else:
                body=None; report_error=None
        surface,published=saxo_surface(body,now)
        quote=forward['quotes'].get(pair); warnings=[]
        if published and (now-timestamp(published,now)).total_seconds()>7*86400:
            surface={}; warnings.append('The Saxo volatility report is over seven days old; Greek estimates are unavailable.')
        if quote:
            spot_day=date.fromisoformat(quote['spot_observation_date'])
            if (now.date()-spot_day).days>7 or (now-timestamp(quote['quote_time'],now)).total_seconds()>7*86400:
                warnings.append('Spot/forward inputs are over seven days old; estimates use an outdated reference.'); surface={}
        if pair in forward['errors']:
            warnings.append('Spot/forward source check failed; saved input dates are retained.')
        if report_error:
            warnings.append('Saxo report check failed; estimates use the dated saved report.')
        return quote,surface.get(pair,[]),published,warnings

    def view(self,pair='EUR/USD',window_days=30,refresh=False,saved_only=False):
        if pair not in PAIRS or window_days not in (1,7,30):
            raise ValueError('Unsupported FX option pair or trade window.')
        if self.enabled and not saved_only:
            self.refresh(force=refresh)
        now=self.clock(); tape=self.tape.snapshot()
        quote,nodes,published,warnings=self.inputs(now,pair)
        cells,selected,modelled,last_trade=build_cells(tape['trades'] if self.enabled else [],pair,now,window_days,quote,nodes)
        has_source=self.enabled and tape['checked_at'] is not None and quote is not None
        metrics={key:{'available':bool(has_source and (key=='notional' or modelled>0 or selected==0 and nodes)),
                     'definition':definition,'coverage_note':f'{modelled} of {selected} loaded trades have Greek estimates. Missing cells require a supported Saxo ATM tenor (up to one year) and current spot/forward inputs.' if key!='notional' else f'{selected} unexpired trades loaded in this window. Notionals use the displayed spot reference.' if has_source else 'A dated spot reference and loaded DTCC trade files are required.'}
                 for key,definition in DEFINITIONS.items()}
        notices=[
            'DTCC-reported European vanilla trades executed within the selected window and expiring after today. This is gross trade activity, not open interest, net positioning or dealer exposure. Calls/puts refer to the displayed pair’s base currency.',
            'ESTIMATED GREEKS: undiscounted Black forward sensitivities. A flat carry rate is inferred from the connected spot and 1Y forward; Saxo ATM total variance is interpolated across 1W, 1M, 3M, 6M and 12M tenors. The 1W volatility is held flat below one week. Smile/skew and discount factors are not modelled.',
            'Missing volatility, tenors beyond one year and missing market inputs leave Greek cells unavailable. A missing contribution makes that side/bucket total unavailable. Same-day expiries are excluded because the cut time is not reported.',
            'Only standard, non-package, European vanilla trades are included. Prints with inconsistent contract dates or currency amounts are excluded. Corrections/errors revise original prints; modifications/terminations are not counted as new trades or used to infer remaining positions. Public notionals may be rounded/capped, and reporting delays apply.',
        ]
        if quote:
            notices.append(f"Spot reference date: {quote['spot_observation_date']} ("+('time '+quote['spot_time'] if quote.get('spot_time') else 'time not published')+f"). 1Y forward quote: {quote['quote_time']}. Source: Investing.com, also used by the carry charts.")
        if published:
            notices.append('Saxo ATM volatility report published '+published+'. It is a periodically published report, not streaming volatility.')
        if not nodes:
            notices.append('No supported current Saxo ATM volatility curve is available for this pair. Reported notional remains available when spot data is loaded.')
        if tape['pending_history_files'] or tape['pending_live_files']:
            notices.insert(0,f"PROVISIONAL COVERAGE: {tape['pending_history_files']} older daily files and {tape['pending_live_files']} intraday files remain. Values reflect loaded trades only; history fills automatically.")
        notices.extend(warnings)
        error=tape['error'] or self.input_error
        status='disabled' if not self.enabled else 'stale' if error or warnings else 'catching_up' if tape['pending_live_files'] or tape['pending_history_files'] else 'ready' if has_source and metrics['gamma']['available'] else 'partial' if has_source else 'unavailable'
        return {'pair':pair,'status':status,'basis':'reported_trades','source':'DTCC public FX trades · estimated Greeks','source_url':BASE,
                'checked_at':tape['checked_at'],'as_of':now.isoformat(),'last_trade_at':last_trade,'published_at':tape['published_at'],
                'spot':quote['spot'] if quote else None,'spot_at':quote.get('spot_time') if quote else None,
                'spot_date':quote.get('spot_observation_date') if quote else None,'volatility_published_at':published,
                'window_start':(now-timedelta(days=window_days)).isoformat(),'window_end':now.isoformat(),
                'rows':ROWS,'columns':COLUMNS,'cells':cells,'metrics':metrics,'notices':notices,'error':error,
                'included_trades':selected,'modelled_trades':modelled,'pending_history_files':tape['pending_history_files'],
                'pending_live_files':tape['pending_live_files'],'poll_interval_seconds':POLL}

    def _run(self):
        while not self.stop.is_set():
            start=monotonic()
            self.refresh(force=True)
            if self.stop.wait(max(.1,POLL-(monotonic()-start))):
                break

    def start(self):
        if not self.enabled:
            return
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop.clear(); self.thread=Thread(target=self._run,name='fx-options-trades',daemon=True); self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=4)
