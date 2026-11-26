import {useCallback,useEffect,useRef,useState} from 'react';
import {Area,AreaChart,CartesianGrid,ResponsiveContainer,Tooltip,XAxis,YAxis} from 'recharts';
import {Download,RefreshCw,Search,X} from 'lucide-react';
import {api,download} from './api';
import {readWorkspacePreference,writeWorkspacePreference} from './workspaceStorage';
import {Badge,Change,Metric,PageTitle,Panel,number,signed,stamp} from './components';
import type {PageProps} from './pages';
import BopSection from './BopSection';
import FxCarryRisk from './FxCarryRisk';
import FxOptionsHeatmap from './FxOptionsHeatmap';

type Quote={symbol:string;base:string;quote:string;name:string;price:number|null;change_pct:number|null;previous_close?:number|null;high?:number|null;low?:number|null;quote_time:string|null;timestamp_kind?:string;market_open:boolean|null;stale:boolean;error:string|null;fetched_at?:string;source?:string;provider?:string;fallback?:boolean;fallback_reason?:string|null;market_state_basis?:string;stale_after_seconds?:number};
type Quota={plan:string;minute_limit:number;minute_used:number;minute_remaining:number;daily_limit:number|null;daily_used_observed:number;daily_remaining_observed:number|null;retry_after_seconds:number;note:string;initialized?:boolean};
type Provider={id:string;name:string;role:string;state:string;last_error:string|null;retry_after_seconds:number};
type Market={source:string;rows:Quote[];checked_at:string;quota:Quota;coverage:string;portfolio_note:string;primary_provider?:string;providers?:Provider[]};
type Candle={date:string;time:string;open:number|null;high:number|null;low:number|null;close:number;complete:boolean;ohlc_available?:boolean};
type History={source?:string;provider?:string;fallback?:boolean;fallback_reason?:string|null;symbol:string;interval:string;bars:Candle[];fetched_at:string;analytics_as_of:string|null;methodology:string;quota:Quota;providers?:Provider[];analytics:{completed_observations:number;return_1m_pct:number|null;volatility_20d_pct:number|null;mean_50d:number|null;distance_50d_pct:number|null}|null};
type Pair={symbol:string;base:string;quote:string;name:string;group:string};
type Results={results:Pair[];total:number;universe_count:number;offset:number;limit:number};
// EUR covers France, Germany and Italy; USD, GBP, JPY and CAD complete the G7.
const DEFAULTS=['EUR/USD','GBP/USD','USD/JPY','USD/CAD','AUD/USD','NZD/USD','USD/SGD','USD/CHF'];
const DEFAULTS_VERSION='g7-v1';
const decimals=(pair:string)=>pair.endsWith('/JPY')?3:5;
const fmt=(v:number|null|undefined,digits=2,suffix='')=>v==null?'Unavailable':number(v,digits)+suffix;

export default function FxPage({notify}:PageProps){
 const [pairs,setPairs]=useState<string[]>(()=>{try{const saved=JSON.parse(readWorkspacePreference('fx-pairs')||'null');if(!Array.isArray(saved)||!saved.length||saved.length>12||!saved.every(p=>typeof p==='string'&&/^[A-Z]{3}\/[A-Z]{3}$/.test(p)))return DEFAULTS;return readWorkspacePreference('fx-defaults-version')===DEFAULTS_VERSION?saved:[...new Set([...DEFAULTS,...saved])].slice(0,12);}catch{return DEFAULTS;}});
 useEffect(()=>{try{writeWorkspacePreference('fx-pairs',JSON.stringify(pairs));writeWorkspacePreference('fx-defaults-version',DEFAULTS_VERSION);}catch{/* The live comparison still works when browser storage is unavailable. */}},[pairs]);
 const [selected,setSelected]=useState(pairs[0]);const [market,setMarket]=useState<Market|null>(null);
 const [busy,setBusy]=useState(false);const [error,setError]=useState('');const sequence=useRef(0);
 const [clock,setClock]=useState(Date.now());
 useEffect(()=>{const timer=setInterval(()=>setClock(Date.now()),30000);return()=>clearInterval(timer);},[]);
 const isStale=(row:Quote)=>row.stale||!row.quote_time||(row.market_open!==false&&clock-Date.parse(row.quote_time)>(row.stale_after_seconds??600)*1000);
 const [period,setPeriod]=useState('3M');const interval=period==='1D'?'5min':period==='1W'?'1h':'1day';
 const [daily,setDaily]=useState<History|null>(null);const [intraday,setIntraday]=useState<History|null>(null);const [historyError,setHistoryError]=useState('');
 const [intradayError,setIntradayError]=useState('');const [dailyBusy,setDailyBusy]=useState(false);const [intradayBusy,setIntradayBusy]=useState(false);const [refresh,setRefresh]=useState(0);
 const [query,setQuery]=useState('');const [searchOpen,setSearchOpen]=useState(false);const [results,setResults]=useState<Results|null>(null);const [searchBusy,setSearchBusy]=useState(false);const [searchError,setSearchError]=useState('');const [offset,setOffset]=useState(0);const searchRef=useRef<HTMLDivElement>(null);
 const load=useCallback(async()=>{const id=++sequence.current;setBusy(true);try{const result=await api<Market>(`/fx/market?pairs=${encodeURIComponent(pairs.join(','))}`);if(id!==sequence.current)return;setMarket(result);setError('');}catch(e){if(id===sequence.current)setError(e instanceof Error?e.message:'FX connection failed');}finally{if(id===sequence.current)setBusy(false);}},[pairs]);
 useEffect(()=>{void load();return()=>{sequence.current++;};},[load]);
 useEffect(()=>{function reload(){void load();setRefresh(v=>v+1);}window.addEventListener('jr-trading-workbench:refresh-fx',reload);const timer=setInterval(()=>{if(!document.hidden)reload();},300000);return()=>{clearInterval(timer);window.removeEventListener('jr-trading-workbench:refresh-fx',reload);};},[load]);
 useEffect(()=>{
  let current=true;const controller=new AbortController();setDaily(null);setDailyBusy(true);setHistoryError('');
  api<History>(`/fx/history?pair=${encodeURIComponent(selected)}&interval=1day`,{signal:controller.signal}).then(r=>{if(current){setDaily(r);setMarket(m=>m&&r.providers?{...m,providers:r.providers}:m);}}).catch(e=>{if(current)setHistoryError(e.message);}).finally(()=>{if(current)setDailyBusy(false);});
  return()=>{current=false;controller.abort();};
 },[selected,refresh]);
 useEffect(()=>{
  if(interval==='1day')return;
  let current=true;const controller=new AbortController();setIntraday(null);setIntradayBusy(true);setIntradayError('');
  api<History>(`/fx/history?pair=${encodeURIComponent(selected)}&interval=${interval}`,{signal:controller.signal}).then(r=>{if(current){setIntraday(r);setMarket(m=>m&&r.providers?{...m,providers:r.providers}:m);}}).catch(e=>{if(current)setIntradayError(e.message);}).finally(()=>{if(current)setIntradayBusy(false);});
  return()=>{current=false;controller.abort();};
 },[selected,interval,refresh]);
 useEffect(()=>{function outside(e:PointerEvent){if(searchRef.current&&!searchRef.current.contains(e.target as Node))setSearchOpen(false);}window.addEventListener('pointerdown',outside);return()=>window.removeEventListener('pointerdown',outside);},[]);
 useEffect(()=>{
  if(!searchOpen)return;
  let current=true;const controller=new AbortController();setSearchBusy(true);setResults(null);setSearchError('');
  const timer=setTimeout(()=>{api<Results>(`/fx/search?q=${encodeURIComponent(query)}&offset=${offset}`,{signal:controller.signal}).then(r=>{if(current)setResults(r);}).catch(e=>{if(current)setSearchError(e.message);}).finally(()=>{if(current)setSearchBusy(false);});},250);
  return()=>{current=false;clearTimeout(timer);controller.abort();};
 },[query,offset,searchOpen]);
 function selectPair(pair:Pair){if(!pairs.includes(pair.symbol)){const next=pairs.length<12?[...pairs,pair.symbol]:pairs.map(p=>p===selected?pair.symbol:p);setPairs(next);writeWorkspacePreference('fx-pairs',JSON.stringify(next));if(pairs.length>=12)notify(`${pair.symbol} replaces ${selected} in the comparison.`);}setSelected(pair.symbol);setQuery('');setOffset(0);setSearchOpen(false);}
 function removePair(pair:string){if(pairs.length<=1)return;const next=pairs.filter(p=>p!==pair);setPairs(next);writeWorkspacePreference('fx-pairs',JSON.stringify(next));if(pair===selected)setSelected(next[0]);}
 function reload(){void load();setRefresh(v=>v+1);window.dispatchEvent(new Event('jr-trading-workbench:refresh-bop'));}
 const quote=market?.rows.find(r=>r.symbol===selected);const history=interval==='1day'?daily:intraday;const chartBusy=interval==='1day'?dailyBusy:intradayBusy;
 const chartError=interval==='1day'?historyError:intradayError;
 const bars=history?.symbol===selected?history.bars.slice(period==='1M'?-22:period==='3M'?-66:period==='1Y'?-252:0):[];
 const unavailableRanges=bars.filter(bar=>bar.ohlc_available===false).length;
 const analytics=daily?.symbol===selected?daily.analytics:null;const digits=decimals(selected);const base=selected.slice(0,3),counter=selected.slice(4);
 return <>
  <PageTitle eyebrow="MARKETS" title="FX" description="Live currency rates, relative moves and historical context." actions={<><a className="button secondary" href="#fx-bop">Balance of payments</a><button className="button secondary" disabled={!market} onClick={()=>download('fx-market-snapshot.json',{market,daily,chart:history})}><Download size={15}/>Export snapshot</button><button className="button primary" disabled={busy} onClick={reload}><RefreshCw size={15} className={busy?'spin':''}/>{busy?'Updating...':'Refresh FX'}</button></>}/>
  {error&&<div className="equity-error" role="alert"><strong>FX update failed</strong><p>{error}</p>{market&&<p>The last successful response remains visible with its original quote timestamps.</p>}<button className="button secondary tiny" onClick={reload}>Retry FX</button></div>}
  <div className="metrics-grid"><Metric accent label={`${selected} · reference rate`} value={fmt(quote?.price,digits)} foot={`${counter} per 1 ${base}${quote&&isStale(quote)?" / stale timestamp":""}`}/><Metric label="Provider daily change" value={quote?.change_pct==null?'Unavailable':`${signed(quote.change_pct)}%`} foot="Versus provider previous close"/><Metric label="One-month price return" value={fmt(analytics?.return_1m_pct,2,'%')} foot={daily?.analytics_as_of?`Completed closes through ${daily.analytics_as_of}`:"Completed daily history required"}/><Metric label="Annualized volatility" value={fmt(analytics?.volatility_20d_pct,2,'%')} foot={daily?.analytics_as_of?`20 returns through ${daily.analytics_as_of}`:"20 completed daily returns required"}/></div>
  <Panel title={`${selected} · exchange-rate history`} subtitle={`Quoted as ${counter} per ${base} · ${interval==='1day'?'provider daily sessions':'UTC intraday candles'}`} actions={<div className="segmented">{['1D','1W','1M','3M','1Y'].map(p=><button key={p} className={period===p?'active':''} onClick={()=>setPeriod(p)}>{p}</button>)}</div>}>
   {chartError?<div className="padded fx-history-error" role="alert"><strong>History unavailable</strong><p>{chartError}</p><button className="button secondary tiny" onClick={()=>setRefresh(v=>v+1)}>Retry history</button></div>:chartBusy?<p className="padded muted" role="status">Loading FX source history...</p>:bars.length?<div className="chart-wrap"><ResponsiveContainer width="100%" height="100%"><AreaChart data={bars} margin={{top:12,right:18,left:0,bottom:0}}><CartesianGrid vertical={false} stroke="#e9edf2" strokeDasharray="3 4"/><XAxis dataKey="date" minTickGap={65} tickFormatter={v=>interval==='1day'?v.slice(5):v.slice(5,16)} axisLine={false} tickLine={false}/><YAxis domain={['auto','auto']} tickFormatter={v=>number(v,digits)} width={80} axisLine={false} tickLine={false}/><Tooltip formatter={v=>[number(Number(v),digits),`${counter} per ${base}`]} contentStyle={{borderRadius:8}}/><Area type="linear" dataKey="close" stroke="#388675" fill="#38867515" strokeWidth={2}/></AreaChart></ResponsiveContainer></div>:<p className="padded muted">No source history has been loaded.</p>}
   <div className="chart-footer"><span>{bars.length} closing prices · latest session may still be developing{unavailableRanges>0?` · ${unavailableRanges} source OHLC ranges unavailable`:''}</span><span>{history?`${history.source??'FX source'} / retrieved ${stamp(history.fetched_at)} SGT`:'FX history'}</span></div>
  </Panel>
  <Panel className="equity-screener-panel" title="FX universe" subtitle="Search Twelve Data currency pairs and build a live comparison" actions={<Badge>{pairs.length} / 12 compared</Badge>}>
   <div className="equity-screener-controls"><div className="equity-universe-search" ref={searchRef} onKeyDown={e=>{if(e.key==='Escape')setSearchOpen(false);}}><form className="table-search" role="search" onSubmit={e=>{e.preventDefault();if(!searchBusy&&results?.results[0])selectPair(results.results[0]);}}><Search size={15}/><input aria-label="Search Twelve Data FX pairs" placeholder="EUR/SGD, yen, Singapore dollar..." value={query} maxLength={100} onFocus={()=>setSearchOpen(true)} onChange={e=>{setQuery(e.target.value);setOffset(0);setSearchOpen(true);setResults(null);}} aria-expanded={searchOpen} aria-controls="fx-pair-results"/></form>
    {searchOpen&&<div id="fx-pair-results" className="equity-universe-results"><div className="equity-search-heading"><strong>Twelve Data currency directory</strong><button className="icon-button" aria-label="Close FX search" onClick={()=>setSearchOpen(false)}><X size={14}/></button></div>{searchBusy&&<p className="equity-search-status" role="status">Searching currency pairs...</p>}{searchError&&<p className="equity-search-status" role="alert">{searchError}</p>}{results&&!searchBusy&&<><p className="equity-search-status" role="status">{number(results.total,0)} matches across {number(results.universe_count,0)} pairs</p>{!results.total&&<p className="equity-search-status">No currency pairs match this search.</p>}<div className="equity-search-list">{results.results.map(pair=><button key={pair.symbol} className="equity-search-result" onClick={()=>selectPair(pair)} aria-label={`Load ${pair.symbol} ${pair.name}`}><span className="instrument-avatar fx">{pair.base}</span><span><strong>{pair.symbol}</strong><small>{pair.name}</small><small>{pair.group}</small></span><span className="equity-search-action">{pairs.includes(pair.symbol)?'View':pairs.length>=12?'Replace selected':'Add & view'}</span></button>)}</div><div className="equity-search-pagination"><button className="button secondary tiny" disabled={!offset} onClick={()=>setOffset(Math.max(0,offset-25))}>Previous</button><span>{results.total?`${offset+1}-${Math.min(offset+25,results.total)}`:'0'} of {results.total}</span><button className="button secondary tiny" disabled={offset+25>=results.total} onClick={()=>setOffset(offset+25)}>Next</button></div></>}</div>}
   </div></div>
   <div className="table-scroll"><table><thead><tr><th>CURRENCY PAIR</th><th className="num">RATE</th><th className="num">DAY CHANGE</th><th className="num">SESSION HIGH / LOW</th><th>QUOTE TIME · SGT</th><th><span className="sr-only">Remove pair</span></th></tr></thead><tbody>{(market?.rows??[]).filter(row=>pairs.includes(row.symbol)).map(row=><tr key={row.symbol} className={selected===row.symbol?'equity-selected':''}><td><button className="instrument-name" onClick={()=>setSelected(row.symbol)}><span className="instrument-avatar fx">{row.base}</span><span><strong>{row.symbol}</strong><small>{row.name}</small></span></button></td><td className="num">{fmt(row.price,decimals(row.symbol))}<small className="unit">{row.quote} per {row.base}</small></td><td className="num">{row.change_pct==null?'Unavailable':<Change value={row.change_pct}/>}</td><td className="num">{fmt(row.high,decimals(row.symbol))} / {fmt(row.low,decimals(row.symbol))}</td><td>{row.quote_time?stamp(row.quote_time):'Unavailable'}</td><td><button className="icon-button" disabled={pairs.length<=1} aria-label={`Remove ${row.symbol} from FX comparison`} onClick={()=>removePair(row.symbol)}><X size={14}/></button></td></tr>)}</tbody></table>{!market&&<p className="padded muted">{busy?'Retrieving currency quotes...':'No live quotes loaded.'}</p>}</div>
  </Panel>
  <FxCarryRisk refresh={refresh}/>
  <BopSection/>
  <FxOptionsHeatmap refresh={refresh}/>
 </>;
}
