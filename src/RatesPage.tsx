import {useCallback,useEffect,useMemo,useRef,useState} from 'react';
import {Bar,CartesianGrid,ComposedChart,Line,ReferenceLine,ResponsiveContainer,Tooltip,XAxis,YAxis} from 'recharts';
import {Download,RefreshCw} from 'lucide-react';
import {api,download} from './api';
import {Badge,PageTitle,Panel,number,stamp} from './components';
import {watchBondRefresh} from './bondsData';
import './rates.css';

export type RateObservation={date:string;value:number|null};
export type RateSeries={key:string;label:string;series_id:string;transformation:string;unit:string;axis:'left'|'right';style:'line'|'bar';frequency:string;source:string;seasonal_adjustment:string;source_url:string|null;observations:RateObservation[];last_value:number|null;last_observation_date:string|null;last_checked_at:string|null;status:string;error:string|null;delivery?:string;last_observation_at?:string|null;last_disseminated_at?:string|null;trade_count?:number|null;history_start?:string|null;pending_live_files?:number;pending_history_files?:number;observation_details?:Record<string,{trade_count:number;last_trade_at:string}>};
export type RateChart={key:string;title:string;subtitle:string;left_unit:string;right_unit?:string;notes:string;series:RateSeries[]};
export type RatesMarket={country:'USA';status:string;poll_interval_seconds:number;history_start:string;last_attempt_at:string|null;cache_warning:string|null;charts:RateChart[];notice:string};
const COLORS=['#388675','#527dcc','#ba8653'];
const dateLabel=(value:string,monthly=false)=>new Date(value+'T00:00:00Z').toLocaleDateString('en-GB',{timeZone:'UTC',month:'short',year:'numeric',...(monthly?{}:{day:'2-digit' as const})});
const valueLabel=(value:number|null,unit:string)=>value===null?'Unavailable':`${number(value,unit==='%'?2:1)}${unit==='%'?'%':` ${unit}`}`;

export function periodStart(period:string,end:string){
 if(period==='All')return '1990-01-01';
 const at=new Date(end+'T00:00:00Z'),years=Number(period.replace('Y',''));
 const month=at.getUTCMonth(),day=at.getUTCDate(),year=at.getUTCFullYear()-years;
 const last=new Date(Date.UTC(year,month+1,0)).getUTCDate();
 return new Date(Date.UTC(year,month,Math.min(day,last))).toISOString().slice(0,10);
}

export function ratesChartData(chart:RateChart,period:string){
 const last=chart.series.flatMap(series=>series.last_observation_date?[series.last_observation_date]:[]).sort().at(-1);
 const start=last?periodStart(period,last):'1990-01-01';
 const rows=new Map<string,Record<string,string|number|null>>();
 const series=chart.series.map(item=>({definition:item,points:item.observations.filter(point=>point.date>=start&&(!last||point.date<=last)&&(item.frequency!=='Daily'||point.value!==null)).map(point=>{
  const time=Date.parse(point.date+'T00:00:00Z');
  const row=rows.get(point.date)??{date:point.date,time};row[item.key]=point.value;rows.set(point.date,row);
  return {date:point.date,time,[item.key]:point.value};
 })}));
 return {series,rows:[...rows.values()].sort((a,b)=>Number(a.time)-Number(b.time))};
}

function MacroTooltip({active,label,chart,rows}:{active?:boolean;label?:number|string;chart:RateChart;rows:Record<string,string|number|null>[]}){
 if(!active)return null;
 const row=rows.find(value=>value.time===Number(label));
 if(!row)return null;
 return <div className="rates-tooltip"><strong>{dateLabel(String(row.date))}</strong>{chart.series.map((series,index)=>{
  const value=row[series.key];
  const detail=series.observation_details?.[String(row.date)];
  return typeof value==='number'?<div key={series.key}><i style={{background:COLORS[index]}}/><span>{series.label}{detail&&<small>{detail.trade_count} trades · last trade {stamp(detail.last_trade_at)} SGT</small>}</span><strong>{valueLabel(value,series.unit)}</strong></div>:null;
 })}<small>Observation date / reference month</small></div>;
}

export function RatesChartPanel({chart,period}:{chart:RateChart;period:string}){
 const plotted=useMemo(()=>ratesChartData(chart,period),[chart,period]);
 const present=plotted.series.some(series=>series.points.some(point=>typeof point[series.definition.key]==='number'));
 const dual=Boolean(chart.right_unit);
 return <Panel title={chart.title} subtitle={chart.subtitle} className={`rates-panel rates-${chart.key}`}>
  <div className="rates-readings">{chart.series.map((series,index)=><div key={series.key} className={series.status==='awaiting_source'?'rates-reading unavailable':'rates-reading'}>
   <span><i style={{background:COLORS[index]}}/>{series.label}</span><strong>{valueLabel(series.last_value,series.unit)}</strong>
   <small>{series.last_observation_date?dateLabel(series.last_observation_date,series.frequency==='Monthly'):series.status==='awaiting_source'?'Source needed':'Awaiting published observations'}</small>
   {series.last_observation_at&&<small>Last trade {stamp(series.last_observation_at)} SGT</small>}
  </div>)}</div>
  <div className="rates-chart" role="img" aria-label={chart.title}>{present?<ResponsiveContainer width="100%" height="100%"><ComposedChart data={plotted.rows} margin={{top:22,right:dual?20:22,bottom:18,left:8}}>
   <CartesianGrid vertical={false} stroke="#e5ebf0" strokeDasharray="3 4"/>
   <XAxis type="number" scale="time" dataKey="time" domain={['dataMin','dataMax']} tickFormatter={value=>new Date(Number(value)).toLocaleDateString('en-GB',{timeZone:'UTC',month:'short',year:'2-digit'})} minTickGap={48} axisLine={false} tickLine={false}/>
   <YAxis yAxisId="left" width={59} domain={['auto','auto']} tickFormatter={value=>number(Number(value),chart.left_unit.includes('%')?1:0)} axisLine={false} tickLine={false} label={{value:chart.left_unit,angle:-90,position:'insideLeft',offset:0,style:{textAnchor:'middle',fill:'#82909d',fontSize:10}}}/>
   {dual&&<YAxis yAxisId="right" orientation="right" width={65} domain={['auto','auto']} tickFormatter={value=>number(Number(value),chart.right_unit?.includes('millions')?1:0)} axisLine={false} tickLine={false} label={{value:chart.right_unit,angle:90,position:'insideRight',style:{textAnchor:'middle',fill:'#82909d',fontSize:10}}}/>}
   <Tooltip content={<MacroTooltip chart={chart} rows={plotted.rows}/>} cursor={{stroke:'#a9b9c3',strokeDasharray:'3 3'}}/>
   {chart.key==='employment'&&<ReferenceLine yAxisId="right" y={0} stroke="#bac7cf" strokeDasharray="4 4"/>}
   {plotted.series.map(({definition:series,points},index)=>series.style==='bar'?<Bar key={series.key} dataKey={series.key} name={series.label} yAxisId={series.axis} fill={COLORS[index]} maxBarSize={12} isAnimationActive={false}/>:<Line key={series.key} data={points} type="linear" dataKey={series.key} name={series.label} yAxisId={series.axis} stroke={COLORS[index]} strokeWidth={2} dot={false} activeDot={{r:3}} connectNulls={false} isAnimationActive={false}/>)}
  </ComposedChart></ResponsiveContainer>:<div className="rates-empty">Published observations are not available for this chart yet.</div>}</div>
  <div className="rates-notes">{chart.notes}</div>
  {chart.series.filter(series=>series.delivery==='reported_transactions').map(series=><div key={series.key} className="rates-source-message" role="status">
   <strong>DTCC reported trades · automatic 60-second checks.</strong>{' '}
   {series.last_observation_at?`${series.trade_count??0} eligible trades in the latest daily median. `:'Waiting for eligible reported trades. '}
   Rates change when new eligible trades or corrections are reported; reporting delays apply.
   {Boolean(series.pending_live_files)&&` Catching up: ${series.pending_live_files} intraday files remain; the current median is provisional.`}
   {series.history_start&&` Swap history from ${dateLabel(series.history_start)}.`}
   {Boolean(series.pending_history_files)&&` Older history is loading automatically (${series.pending_history_files} daily files remaining).`}
  </div>)}
  {chart.series.filter(series=>series.error).map(series=><div key={series.key} className="rates-source-message" role="status"><strong>{series.label}: </strong>{series.error}</div>)}
  <details className="rates-provenance"><summary>Sources &amp; observation dates</summary>{chart.series.map(series=><div key={series.key}>
   <span>{series.label} · {series.source}</span><small>{series.frequency} · {series.seasonal_adjustment} · {series.series_id}</small>
   {series.source_url&&<a href={series.source_url} target="_blank" rel="noopener noreferrer">View source</a>}
   {series.last_checked_at&&<small>Checked {stamp(series.last_checked_at)} SGT</small>}
   {series.last_disseminated_at&&<small>Latest processed file {stamp(series.last_disseminated_at)} SGT</small>}
  </div>)}</details>
 </Panel>;
}

export default function RatesPage(){
 const [market,setMarket]=useState<RatesMarket|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[period,setPeriod]=useState('5Y');
 const request=useRef<AbortController|null>(null),lastAttempt=useRef(0),failed=useRef(false);
 const load=useCallback(async(force=false)=>{
  if(request.current)return;
  const controller=new AbortController();request.current=controller;lastAttempt.current=Date.now();setBusy(true);
  const timeout=window.setTimeout(()=>controller.abort(),90_000);
  try{
   if(!force){
    try{const saved=await api<RatesMarket>('/rates/us?saved_only=true',{signal:controller.signal,cache:'no-store'});if(request.current===controller&&saved.country==='USA'&&saved.charts?.some(chart=>chart.series.some(series=>series.last_value!==null)))setMarket(saved);}
    catch(reason){if(controller.signal.aborted)throw reason;}
   }
   const data=await api<RatesMarket>('/rates/us'+(force?'?refresh=true':''),{signal:controller.signal,cache:'no-store'});
   if(data.country!=='USA'||!Array.isArray(data.charts)||data.charts.length!==7)throw Error('The U.S. Rates response is incomplete.');
   if(request.current===controller){setMarket(data);setError('');failed.current=false;}
  }catch(reason){if(request.current===controller){failed.current=true;setError(controller.signal.aborted?'The source check timed out. Saved observations retain their dates.':reason instanceof Error?reason.message:'U.S. rates data could not be loaded.');}}
  finally{window.clearTimeout(timeout);if(request.current===controller){request.current=null;lastAttempt.current=Date.now();setBusy(false);}}
 },[]);
 useEffect(()=>{
  void load();
  const stop=watchBondRefresh(()=>void load(),()=>!request.current,()=>lastAttempt.current,()=>failed.current,()=>60_000);
  const manual=()=>void load(true);window.addEventListener('meridian:refresh-rates',manual);
  return()=>{stop();window.removeEventListener('meridian:refresh-rates',manual);const pending=request.current;request.current=null;pending?.abort();};
 },[load]);
 const series=market?.charts.flatMap(chart=>chart.series)??[];
 const ready=series.filter(value=>value.last_value!==null).length;
 return <>
  <PageTitle eyebrow="MARKETS · UNITED STATES" title="Rates" description="Inflation, labour markets, expectations and housing." actions={<><button className="button secondary" disabled={!ready} onClick={()=>download('us-rates-macro.json',market)}><Download size={15}/>Export data</button><button className="button primary" disabled={busy} onClick={()=>void load(true)}><RefreshCw size={15} className={busy?'spin':''}/>{busy?'Checking sources…':'Refresh U.S. data'}</button></>}/>
  <div className="rates-status"><Badge tone={ready?'green':'neutral'}>United States · USD</Badge><span>{ready} series loaded</span><span>FRED releases · Indeed job postings · DTCC inflation swap trades</span>{market?.last_attempt_at&&<span>Sources checked {stamp(market.last_attempt_at)} SGT</span>}</div>
  <p className="rates-frequency">Charts check every minute while visible and catch up when you return. DTCC inflation swap trades are checked every minute in the background while the app runs. New observations and corrections update automatically; source publication and reporting delays apply. Monthly dates identify reference months.</p>
  {(error||market?.cache_warning)&&<div className="rates-error" role="alert">{error||market?.cache_warning}</div>}
  <div className="rates-range"><span>Chart history</span><div className="segmented" aria-label="U.S. macro chart date range">{['1Y','3Y','5Y','10Y','All'].map(value=><button key={value} type="button" className={period===value?'active':''} aria-pressed={period===value} onClick={()=>setPeriod(value)}>{value}</button>)}</div></div>
  <div className="rates-grid">{market?.charts.map(chart=><RatesChartPanel key={chart.key} chart={chart} period={period}/>)}{!market&&<div className="rates-loading" role="status">{busy?'Loading published U.S. data…':'No saved U.S. rates data is available yet.'}</div>}</div>
 </>;
}
