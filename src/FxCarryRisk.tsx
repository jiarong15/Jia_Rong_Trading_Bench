import {useEffect,useRef,useState} from 'react';
import {CartesianGrid,LabelList,ReferenceLine,ResponsiveContainer,Scatter,ScatterChart,Tooltip,XAxis,YAxis} from 'recharts';
import {RefreshCw} from 'lucide-react';
import {api} from './api';
import {Badge,Panel,number,signed,stamp} from './components';
import {watchBondRefresh} from './bondsData';
import './fxCarryRisk.css';

type CarryPoint={currency:string;pair:string;usd_carry_pct:number|null;pair_carry_pct:number|null;risk_correlation:number|null;weekly_observations:number;correlation_start:string|null;correlation_end:string|null;implied_volatility_pct:number|null;iv_tenor:string|null;iv_time:string|null;iv_source?:string;iv_source_url?:string;iv_delivery?:string;iv_basis?:string;iv_stale?:boolean;spot:number|null;forward_12m:number|null;spot_date:string|null;forward_date:string|null;quote_time:string|null;source:string|null;stale:boolean;carry_basis?:string;spot_time?:string|null;spot_observation_date?:string;spot_time_precision?:string;forward_bid_points?:number;forward_ask_points?:number;source_url?:string;source_check_at?:string;source_check_failed?:boolean};
type CarryRisk={status:string;basis:'observed_forwards_and_implied_volatility';feed_configured:boolean;forward_feed_configured?:boolean;forward_source_checks?:Record<string,string>;volatility_status?:{provider:string;enabled:boolean;status:string;report_checked_at:string|null;report_published_at:string|null;poll_interval_seconds:number;errors:string[]}|null;quote_delivery?:string;iv_tenor:string;poll_interval_seconds:number;weeks:number;last_attempt_at:string|null;source_checks:Record<string,string>;errors:string[];rows:CarryPoint[];methodology:string;sources:{name:string;url:string}[]};
type PlotPoint=CarryPoint&{x:number;y:number;label:string};
const pct=(value:number|null)=>value===null?'Unavailable':`${signed(value,2)}%`;

function RiskTooltip({active,payload,kind}:{active?:boolean;payload?:{payload:PlotPoint}[];kind:'correlation'|'volatility'}){
 const point=payload?.[0]?.payload;
 if(!active||!point)return null;
 return <div className="fx-carry-tooltip">
  <strong>{kind==='correlation'?`USD versus ${point.currency}`:point.pair}</strong>
  <span>Forward carry: {pct(point.y)}</span>
  {kind==='correlation'?<><span>S&amp;P 500 correlation: {number(point.x,3)}</span><span>{point.weekly_observations} weekly returns through {point.correlation_end}</span></>:<><span>Implied volatility: {number(point.x,2)}%</span><span>{point.iv_tenor} ATM: {point.iv_time?stamp(point.iv_time)+' SGT':'timestamp unavailable'}</span></>}
  {kind==='volatility'&&<><small>{point.iv_source??point.source} · {point.iv_delivery==='daily_report'?'Published report snapshot':'Options feed'}</small><small>{point.iv_basis}</small>{point.iv_stale&&<small>{point.iv_delivery==='daily_report'?'Report is more than seven days old.':'Volatility observation is over five minutes old.'}</small>}</>}
  <span>Spot mid: {point.spot===null?'Unavailable':number(point.spot,5)}</span>
  <span>12M forward mid: {point.forward_12m===null?'Unavailable':number(point.forward_12m,5)}</span>
  {point.carry_basis==='quoted_1Y_tenor'?<>
   <small>1Y forward points: {number(point.forward_bid_points??0,2)} / {number(point.forward_ask_points??0,2)}</small>
   <small>Spot observed: {point.spot_time?stamp(point.spot_time)+' SGT':point.spot_observation_date+' (date only)'}</small>
   <small>Quoted 1Y tenor; settlement dates are not published.</small>
  </>:<small>Value dates: {point.spot_date} to {point.forward_date}</small>}
  <small>{point.source}: forward quoted {point.quote_time?stamp(point.quote_time)+' SGT':'timestamp unavailable'}</small>
  {point.source_check_failed&&<small>Latest source check failed; retaining the previous quote.</small>}
  {point.stale&&<small>Quote is over five minutes old, or the spot time is available only as a date.</small>}
 </div>;
}

function CarryScatter({rows,kind,busy}:{rows:CarryPoint[];kind:'correlation'|'volatility';busy:boolean}){
 const data:PlotPoint[]=rows.flatMap(row=>{
  const x=kind==='correlation'?row.risk_correlation:row.implied_volatility_pct;
  const y=kind==='correlation'?row.usd_carry_pct:row.pair_carry_pct;
  return x===null||y===null?[]:[{...row,x,y,label:kind==='correlation'?row.currency:row.pair.replace('/','')}];
 });
 const minimum=Math.min(0,...data.map(row=>row.y))-1;
 const maximum=Math.max(0,...data.map(row=>row.y))+1;
 return <div className="fx-carry-chart" role="img" aria-label={kind==='correlation'?'Scatter chart of 12-month long-USD forward carry versus weekly S&P 500 correlation':'Scatter chart of annualized forward carry versus implied currency volatility'}>{data.length?<ResponsiveContainer width="100%" height="100%"><ScatterChart margin={{top:34,right:48,left:24,bottom:42}}><CartesianGrid stroke="#e3e9ee" strokeDasharray="3 4"/><XAxis type="number" dataKey="x" name={kind==='correlation'?'Correlation':'Implied volatility'} domain={kind==='correlation'?[-1,1]:[0,'auto']} tickFormatter={value=>number(value,kind==='correlation'?1:0)} tickLine={false} axisLine={false} label={{value:kind==='correlation'?'Weekly spot return correlation to S&P 500':'Implied volatility (%)',position:'bottom',offset:16}}/><YAxis type="number" dataKey="y" name="Forward carry" unit="%" domain={[minimum,maximum]} tickFormatter={value=>number(value,1)} tickLine={false} axisLine={false} width={52} label={{value:'Annualized carry (%)',angle:-90,position:'insideLeft',offset:-12}}/><ReferenceLine y={0} stroke="#9aaab2"/>{kind==='correlation'&&<ReferenceLine x={0} stroke="#c8d1d7"/>}<Tooltip cursor={{strokeDasharray:'3 3'}} content={<RiskTooltip kind={kind}/>}/><Scatter name="Currencies" data={data} fill="#388675" isAnimationActive={false}><LabelList dataKey="label" position="top" offset={10} fill="#36495c" fontSize={11}/></Scatter></ScatterChart></ResponsiveContainer>:<div className="fx-carry-empty" role="status">{busy?'Checking FX history and the forward/options feed...':kind==='volatility'?'No usable Saxo report volatility and forward carry combination is available yet. The report is checked automatically.':'Forward quotes or sufficient FX/S&P 500 history are not available yet.'}</div>}</div>;
}

export default function FxCarryRisk({refresh}:{refresh:number}){
 const [data,setData]=useState<CarryRisk|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false),[weeks,setWeeks]=useState(52),[manual,setManual]=useState(0);
 const lastAttempt=useRef(0),previousRefresh=useRef(refresh),previousManual=useRef(manual);
 useEffect(()=>{
  let current=true;
  let request:AbortController|null=null;
  const force=previousRefresh.current!==refresh||previousManual.current!==manual;
  previousRefresh.current=refresh;previousManual.current=manual;
  const load=async(forceCheck=false)=>{
   if(request)return;
   const controller=new AbortController();request=controller;lastAttempt.current=Date.now();setBusy(true);
   const timeout=window.setTimeout(()=>controller.abort(),55000);
   try{
    const path=`/fx/carry-risk?weeks=${weeks}`;
    if(!forceCheck){
     try{const saved=await api<CarryRisk>(path+'&saved_only=true',{signal:controller.signal,cache:'no-store'});if(current&&saved.basis==='observed_forwards_and_implied_volatility'&&saved.weeks===weeks&&saved.rows.some(row=>row.usd_carry_pct!==null)){setData(saved);}}
     catch(reason){if(controller.signal.aborted)throw reason;}
    }
    const result=await api<CarryRisk>(path+(forceCheck?'&refresh=true':''),{signal:controller.signal,cache:'no-store'});
    if(result.basis!=='observed_forwards_and_implied_volatility'||result.weeks!==weeks||!Array.isArray(result.rows))throw Error('The carry/risk API returned an unexpected data basis.');
    if(current){setData(result);setError('');}
   }catch(reason){if(current)setError(controller.signal.aborted?'The carry/risk source check timed out. Saved observations retain their original dates.':reason instanceof Error?reason.message:'Carry/risk data could not be checked.');}
   // The backend's refresh window starts after our saved-data request. Wait a
   // full minute after completion so network latency cannot make a scheduled
   // poll arrive just before that window and reuse the previous source quote.
   finally{window.clearTimeout(timeout);request=null;if(current){lastAttempt.current=Date.now();setBusy(false);}}
  };
  void load(force);
  const stop=watchBondRefresh(()=>void load(),()=>!request,()=>lastAttempt.current,()=>false,()=>60_000);
  return()=>{current=false;stop();request?.abort();};
 },[weeks,refresh,manual]);
 const rows=data?.rows??[];
 const correlationRows=rows.filter(row=>row.risk_correlation!==null&&row.usd_carry_pct!==null);
 const volatilityRows=rows.filter(row=>row.implied_volatility_pct!==null&&row.pair_carry_pct!==null);
 const errors=[error,...(data?.errors??[])].filter(Boolean);
 const publicForwards=Boolean(data?.forward_feed_configured);
 const report=data?.volatility_status;
 const missingVolatility=rows.filter(row=>row.implied_volatility_pct===null).map(row=>row.currency);
 const checked=Object.values({...data?.source_checks,...data?.forward_source_checks}).sort()[0];
 return <section className="fx-carry-risk" aria-labelledby="fx-carry-heading">
  <div className="fx-carry-heading"><div><h2 id="fx-carry-heading">FX carry & risk</h2><p>Currency carry, equity sensitivity and volatility.</p></div><div className="fx-carry-controls"><div className="segmented" aria-label="Carry risk correlation lookback">{[26,52,104].map(value=><button type="button" key={value} className={weeks===value?'active':''} aria-pressed={weeks===value} onClick={()=>setWeeks(value)}>{value}W</button>)}</div><button type="button" className="button secondary tiny" disabled={busy} onClick={()=>setManual(value=>value+1)}><RefreshCw size={14} className={busy?'spin':''}/>{busy?'Checking…':'Refresh charts'}</button></div></div>
  <div className="fx-carry-status"><Badge tone={data?.status==='ready'?'green':'neutral'}>{publicForwards?'Investing.com forwards':data?.feed_configured?'Forward/options feed connected':'Forward/options feed required'}</Badge><span>Checks every minute while visible; FRED history every 5 minutes</span>{checked&&<span>Sources checked {stamp(checked)} SGT</span>}</div>
  <p className="fx-carry-basis">{publicForwards?'Carry uses spot bid/ask midpoints and quoted 1Y forward points from Investing.com. Website quotes may be delayed; source timestamps are shown below. Checks update the chart when new quotes are available.':'Carry uses observed spot and 12-month forward quotes.'} Weekly correlations use published FRED FX and S&amp;P 500 history.</p>
  {report&&<div className="fx-carry-vol-status" role="status"><strong>Options volatility: Saxo public report · {volatilityRows.length} pairs</strong><span>{report.enabled?'The app checks the report every minute. New report values appear automatically; the chart refreshes every minute while visible and catches up when you return.':'Saxo report checks are disabled.'} Values are published report snapshots.</span>{report.report_published_at&&<span>Report published {stamp(report.report_published_at)} SGT.</span>}{report.report_checked_at&&<span>Report checked {stamp(report.report_checked_at)} SGT.</span>}{missingVolatility.length>0&&<span>Not covered by the report: {missingVolatility.join(', ')}.</span>}</div>}
  {!!errors.length&&<div className="fx-carry-error" role="alert">{errors.join(' ')}</div>}
  <div className="fx-carry-grid">
   <Panel title="USD/ccy carry vs risk correlation" subtitle={`12M forward carry · ${weeks}-week correlation window`} actions={<Badge>{correlationRows.length} currencies</Badge>}><CarryScatter rows={rows} kind="correlation" busy={busy}/><div className="chart-footer"><span>Long USD, short foreign currency</span><span>Completed weeks · same-date endpoints</span></div></Panel>
   <Panel title="Annualized carry vs implied volatility" subtitle={`12M forward carry - ${data?.iv_tenor??'12M'} option implied volatility`} actions={<Badge>{volatilityRows.length} pairs</Badge>}><CarryScatter rows={rows} kind="volatility" busy={busy}/><div className="chart-footer"><span>Long the displayed pair’s base currency</span><span>{report?'Saxo published ATM volatility':'Observed options implied volatility'}</span></div></Panel>
  </div>
  {rows.some(row=>row.stale)&&<p className="fx-carry-error" role="status">Some quotes are over five minutes old, or their spot times are date-only. This includes saved weekend quotes. Hover over each point for source dates and times.</p>}
  <details className="fx-carry-method"><summary>Data dates &amp; calculation details</summary><p>{data?.methodology??'Loading source methodology...'}</p>
   <div className="table-scroll"><table><thead><tr><th>PAIR</th><th>FORWARD CARRY</th><th>IMPLIED VOL.</th><th>USD RISK CORR.</th><th>SPOT OBSERVED</th><th>FORWARD QUOTE TIME</th><th>OPTIONS QUOTE TIME</th></tr></thead><tbody>{rows.map(row=><tr key={row.pair}>
    <td>{row.source_url?<a href={row.source_url} target="_blank" rel="noopener noreferrer">{row.pair}</a>:row.pair}{row.source_check_failed&&<small className="unit">Source check failed</small>}</td>
    <td>{pct(row.pair_carry_pct)}{row.carry_basis==='quoted_1Y_tenor'&&<small className="unit">Quoted 1Y tenor</small>}</td>
    <td>{row.implied_volatility_pct===null?'Unavailable':number(row.implied_volatility_pct,2)+'%'}{row.iv_source&&<small className="unit">{row.iv_source_url?<a href={row.iv_source_url} target="_blank" rel="noopener noreferrer">{row.iv_source}</a>:row.iv_source}{row.iv_delivery==='daily_report'?' · published report':''}</small>}{row.iv_basis&&<small className="unit">{row.iv_basis}</small>}</td>
    <td>{row.risk_correlation===null?'Unavailable':number(row.risk_correlation,3)}<small className="unit">{row.weekly_observations} weeks through {row.correlation_end??'no endpoints'}</small></td>
    <td>{row.spot_time?stamp(row.spot_time)+' SGT':row.spot_observation_date?row.spot_observation_date+' (date only)':row.quote_time?stamp(row.quote_time)+' SGT':'Unavailable'}</td>
    <td>{row.quote_time?stamp(row.quote_time)+' SGT':'Unavailable'}</td><td>{row.iv_time?stamp(row.iv_time)+' SGT':'Feed required'}</td>
   </tr>)}</tbody></table></div><div className="fx-carry-links">{data?.sources.map(source=><a href={source.url} key={source.url} target="_blank" rel="noopener noreferrer">{source.name}</a>)}</div>
  </details>
 </section>;
}
