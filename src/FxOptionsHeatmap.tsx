import {useCallback,useEffect,useMemo,useRef,useState} from 'react';
import {Download,RefreshCw} from 'lucide-react';
import {api,download} from './api';
import {Badge,Metric,Panel,number,stamp} from './components';
import {watchBondRefresh} from './bondsData';
import {METRICS,compactUsd,heatColor,heatmapValues,validateHeatmap} from './fxOptionsData';
import type {GreekMetric,HeatmapData} from './fxOptionsData';
import './fxOptions.css';

const PAIRS=['EUR/USD','USD/JPY','GBP/USD','AUD/USD','USD/CAD','USD/CHF','NZD/USD','USD/SEK','USD/NOK'];

export function FxOptionsGrid({data,metric,calls,puts}:{data:HeatmapData|null;metric:GreekMetric;calls:boolean;puts:boolean}){
 const values=useMemo(()=>heatmapValues(data,metric,calls,puts),[data,metric,calls,puts]);
 const enabled=calls||puts;
 return <>
  <div className="fx-options-summary">
   <Metric label={`Total ${METRICS[metric].toLowerCase()}`} value={enabled?compactUsd(values.total):'—'} foot="Gross sensitivity · USD"/>
   <Metric label={data?.basis==='open_interest'?'Included contracts':'Included trades'} value={data?.metrics.notional.available&&enabled?number(values.count,0):'—'} foot={data?.basis==='open_interest'?'Published open interest':'Reported in the selected window'}/>
   <Metric label="Put / call ratio" value={values.putCallRatio===null?'—':number(values.putCallRatio,2)} foot={`All sides · ${METRICS[metric].toLowerCase()}`}/>
   <Metric label="Reference spot" value={data?.spot==null?'—':number(data.spot,data.pair.endsWith('/JPY')?3:5)} foot={data?.spot_at?`${stamp(data.spot_at)} SGT`:data?.spot_date?`${data.spot_date} · time not published`:'Source timestamp required'}/>
  </div>
  {!enabled&&<p className="fx-options-notice" role="status">Select Calls, Puts, or both to display the heatmap.</p>}
  {data&&!values.available&&<p className="fx-options-notice" role="status">{data.metrics[metric]?.coverage_note??`${METRICS[metric]} is not available from the connected source.`}</p>}
  {data?.metrics[metric]?.coverage_note&&values.available&&<p className="fx-options-coverage">{data.metrics[metric].coverage_note}</p>}
  <div className="fx-options-table-scroll" tabIndex={0} role="region" aria-label={`${METRICS[metric]} by strike moneyness and expiry; scroll horizontally`}>
   <table className="fx-options-table">
    <caption>{data?.pair??'FX options'} · {METRICS[metric]} · strike / reference spot by time to expiry</caption>
    <thead><tr><th scope="col">Strike / spot</th><th scope="col">Total</th>{data?.columns.map(label=><th scope="col" key={label}>{label}</th>)}</tr></thead>
    <tbody>
     <tr className="fx-options-totals"><th scope="row">Total</th><td>{enabled?compactUsd(values.total):'—'}</td>{values.columnTotals.map((value,index)=><td key={index}><strong>{enabled?compactUsd(value):'—'}</strong><span className="fx-options-bar" style={{width:values.total&&value!==null?`${Math.max(0,value/values.total)*100}%`:'0%'}}/></td>)}</tr>
     {data?.rows.map((label,row)=><tr key={label}><th scope="row">{label}</th><td className="fx-options-row-total">{enabled?compactUsd(values.rowTotals[row]):'—'}</td>{data.columns.map((expiry,column)=>{
      const value=enabled?values.grid[row][column]:null;
      const strong=value!==null&&values.max>0&&value/values.max>.24;
      const title=`${label} strike / spot · ${expiry} · ${METRICS[metric]}: ${value===null?'unavailable':new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2}).format(value)}`;
      return <td key={expiry} className="fx-options-cell" style={{background:heatColor(value,values.max),color:strong?'white':undefined}} title={title} aria-label={title}>{compactUsd(value)}</td>;
     })}</tr>)}
    </tbody>
   </table>
  </div>
  <div className="fx-options-legend"><span>Lower concentration</span><i/><span>Higher concentration</span><small>Each view scales to its largest cell. “—” means unavailable.</small></div>
 </>;
}

export default function FxOptionsHeatmap({refresh=0}:{refresh?:number}){
 const [pair,setPair]=useState('EUR/USD'),[metric,setMetric]=useState<GreekMetric>('gamma'),[calls,setCalls]=useState(true),[puts,setPuts]=useState(true),[windowDays,setWindowDays]=useState(30);
 const [data,setData]=useState<HeatmapData|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
 const request=useRef<AbortController|null>(null),sequence=useRef(0),lastAttempt=useRef(0),failed=useRef(false);
 const load=useCallback(async(force=false)=>{
  if(request.current)return;
  const controller=new AbortController(),id=++sequence.current;request.current=controller;setBusy(true);
  const timeout=window.setTimeout(()=>controller.abort(),90_000);
  const query=`pair=${encodeURIComponent(pair)}&window_days=${windowDays}`;
  try{
   if(!force){try{const saved=validateHeatmap(await api<HeatmapData>(`/fx/options/heatmap?${query}&saved_only=true`,{signal:controller.signal,cache:'no-store'}),pair);if(id===sequence.current)setData(saved);}catch(reason){if(controller.signal.aborted)throw reason;}}
   const result=validateHeatmap(await api<HeatmapData>(`/fx/options/heatmap?${query}${force?'&refresh=true':''}`,{signal:controller.signal,cache:'no-store'}),pair);
   if(id===sequence.current){setData(result);setError('');failed.current=false;}
  }catch(reason){if(id===sequence.current){failed.current=true;setError(controller.signal.aborted?'The options source check timed out. Saved data retains its source timestamps.':reason instanceof Error?reason.message:'FX options data could not be loaded.');}}
  finally{window.clearTimeout(timeout);if(id===sequence.current){request.current=null;lastAttempt.current=Date.now();setBusy(false);}}
 },[pair,windowDays]);
 useEffect(()=>{
  setData(null);setError('');void load();
  const stop=watchBondRefresh(()=>void load(),()=>!request.current,()=>lastAttempt.current,()=>failed.current,()=>60_000);
  const manual=()=>void load(true);window.addEventListener('meridian:refresh-fx',manual);
  return()=>{stop();window.removeEventListener('meridian:refresh-fx',manual);sequence.current++;const previous=request.current;request.current=null;previous?.abort();};
 },[load,refresh]);
 return <section id="fx-options" className="fx-options-section" aria-label="FX options expiration heatmap">
  <div className="fx-options-heading"><div><span className="eyebrow">FX OPTIONS</span><h2>Expiration heatmap<span>.</span></h2><p>Explore option concentration across moneyness and expiry.</p></div><Badge tone={data?.status==='ready'?'green':'neutral'}>{data?.basis==='open_interest'?'Open interest':data?.basis==='reported_trades'?'Reported trades':'Checking source'}</Badge></div>
  <Panel title={`${pair} · option concentration`} subtitle={data?.basis==='reported_trades'?'Reported trade activity · ESTIMATED Greeks · gross sensitivities':'Strike moneyness × time to expiry'} actions={<div className="fx-options-actions"><button className="button secondary tiny" disabled={!data?.cells.length} onClick={()=>download(`fx-options-${pair.replace('/','')}-${metric}.json`,{...data,selected_metric:metric,calls,puts})}><Download size={14}/>Export</button><button className="button secondary tiny" disabled={busy} onClick={()=>void load(true)}><RefreshCw size={14} className={busy?'spin':''}/>{busy?'Checking…':'Refresh'}</button></div>}>
   <div className="fx-options-controls">
    <label>Currency pair<select value={pair} onChange={event=>setPair(event.target.value)} aria-label="FX options currency pair">{PAIRS.map(value=><option key={value}>{value}</option>)}</select></label>
    <fieldset><legend>Units</legend><div className="segmented" aria-label="FX option notional measure">{(Object.keys(METRICS) as GreekMetric[]).map(value=><button key={value} type="button" aria-pressed={metric===value} className={metric===value?'active':''} onClick={()=>setMetric(value)}>{METRICS[value]}</button>)}</div></fieldset>
    <fieldset className="fx-options-sides"><legend>Option type</legend><label><input type="checkbox" checked={calls} onChange={event=>setCalls(event.target.checked)}/>Calls</label><label><input type="checkbox" checked={puts} onChange={event=>setPuts(event.target.checked)}/>Puts</label></fieldset>
    {data?.basis!=='open_interest'&&<label>Trade window<select value={windowDays} onChange={event=>setWindowDays(Number(event.target.value))} aria-label="FX option trade lookback"><option value={1}>Past 24 hours</option><option value={7}>Past 7 days</option><option value={30}>Past 30 days</option></select></label>}
   </div>
   <div className="fx-options-status" aria-live="polite"><span>{data?.source??'Connecting options source…'}</span>{data?.as_of&&<span>Model valued {stamp(data.as_of)} SGT</span>}{data?.last_trade_at&&<span>Last trade {stamp(data.last_trade_at)} SGT</span>}{data?.checked_at&&<span>DTCC checked {stamp(data.checked_at)} SGT</span>}<span>Automatic 60-second checks</span></div>
   {(error||data?.error)&&<p className="fx-options-error" role="alert">{error||data?.error}</p>}
   {!data?<div className="fx-options-loading" role="status">{busy?'Loading FX option source data…':'FX option source data is unavailable.'}</div>:<FxOptionsGrid data={data} metric={metric} calls={calls} puts={puts}/>}
   {data&&<div className="fx-options-notes"><p><strong>{METRICS[metric]}:</strong> {data.metrics[metric]?.definition}</p>{data.notices.map((notice,index)=><p key={index}>{notice}</p>)}{data.source_url&&<a href={data.source_url} target="_blank" rel="noopener noreferrer">View source</a>}</div>}
  </Panel>
 </section>;
}
