import {useCallback,useEffect,useRef,useState} from 'react';
import {CartesianGrid,Line,LineChart,ReferenceLine,ResponsiveContainer,Tooltip,XAxis,YAxis} from 'recharts';
import {ArrowUpRight,ChevronDown,Download,RefreshCw} from 'lucide-react';
import {api,download} from './api';
import {Badge,PageTitle,Panel,number,signed,stamp} from './components';
import {BOND_COUNTRIES,CURVE_METRICS,TENORS,bondPollInterval,filterBondRows,watchBondRefresh} from './bondsData';
import type {BondMarket,BondRow,MetricKey,YieldKey} from './bondsData';
import './bonds.css';
import './bop.css';

const day=(value:string)=>new Date(value+'T00:00:00Z').toLocaleDateString('en-GB',{timeZone:'UTC',day:'2-digit',month:'short',year:'numeric'});
const bp=(value:number|null|undefined)=>value==null?'Unavailable':`${signed(value,1)} bp`;

export function BondHistory({rows,field,unit,color='#388675',observationsOnly=false,emptyMessage='No matching published observations for this window.'}:{rows:BondRow[];field:YieldKey|MetricKey;unit:'%'|'bp';color?:string;observationsOnly?:boolean;emptyMessage?:string}){
 // FRED includes empty holiday rows. Omit dates with no published maturities
 // so closures do not break every line. Keep a missing leg on an otherwise
 // published date as null: connectNulls=false still exposes genuine gaps.
 const plotted=rows.filter(row=>observationsOnly?row[field]!=null:TENORS.some(tenor=>row[tenor.key]!=null));
 const present=plotted.some(row=>row[field]!=null);
 return <div className="bonds-history">{!present?<div className="bonds-chart-empty">{emptyMessage}</div>:<ResponsiveContainer width="100%" height="100%"><LineChart data={plotted} margin={{top:12,right:18,left:0,bottom:4}}><CartesianGrid vertical={false} stroke="#e9edf2" strokeDasharray="3 4"/><XAxis dataKey="date" tickFormatter={value=>value.slice(2)} minTickGap={65} axisLine={false} tickLine={false}/><YAxis width={65} domain={['auto','auto']} tickFormatter={value=>unit==='%'?`${number(value,1)}%`:number(value,0)} axisLine={false} tickLine={false}/><Tooltip labelFormatter={value=>day(String(value))} formatter={value=>[`${number(Number(value),unit==='%'?2:1)} ${unit}`,field]} contentStyle={{borderRadius:8}}/>{unit==='bp'&&<ReferenceLine y={0} stroke="#bbc7cd" strokeDasharray="4 4"/>}<Line type="linear" dataKey={field} name={field} stroke={color} strokeWidth={2} dot={false} connectNulls={false} isAnimationActive={false}/></LineChart></ResponsiveContainer>}</div>;
}

export default function BondsPage(){
 const [market,setMarket]=useState<BondMarket|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
 const [country,setCountry]=useState('USA');
 const [selected,setSelected]=useState<YieldKey>('y2'),[period,setPeriod]=useState('1Y');
 const request=useRef<AbortController|null>(null),lastAttempt=useRef(0),failed=useRef(false);
 const load=useCallback(async(force=false)=>{
  if(request.current)return;
  const controller=new AbortController();request.current=controller;lastAttempt.current=Date.now();setBusy(true);
  const timeout=window.setTimeout(()=>controller.abort(),120_000);
  try{
   if(['CHN','JPN','GBR','CAN','SGP'].includes(country)&&!force){
    try{
     const saved=await api<BondMarket>(`/bonds/treasury?country=${country}&saved_only=true`,{signal:controller.signal,cache:'no-store'});
     if(request.current===controller&&saved.country?.code===country&&saved.rows?.length){setMarket(saved);}
    }catch(reason){if(controller.signal.aborted)throw reason;}
   }
   const result=await api<BondMarket>(`/bonds/treasury?country=${country}${force?'&refresh=true':''}`,{signal:controller.signal,cache:'no-store'});
   if(result.country?.code!==country)throw new Error('The yield server returned data for a different country.');
   if(!Array.isArray(result.rows)||!Array.isArray(result.quotes)||!Array.isArray(result.metrics))throw new Error('The yield server returned an invalid response.');
   if(request.current===controller){
    failed.current=Boolean(result.error);
    if(failed.current)lastAttempt.current=Date.now();
    else if(['JPN','GBR','CAN','SGP'].includes(country)&&result.last_attempt_at){
     const checked=Date.parse(result.last_attempt_at);
     if(Number.isFinite(checked)&&checked<=Date.now())lastAttempt.current=checked;
    }
    setMarket(result);setSelected(current=>result.quotes.some(item=>item.key===current&&item.series_id)?current:'y10');setError('');
   }
  }catch(reason){if(request.current===controller){failed.current=true;lastAttempt.current=Date.now();setError(controller.signal.aborted?'The official yield request timed out. Saved observations remain dated as originally published.':reason instanceof Error?reason.message:'Could not load Treasury yields.');}}
  finally{window.clearTimeout(timeout);if(request.current===controller){request.current=null;setBusy(false);}}
 },[country]);
 useEffect(()=>{
  void load();
  const stop=watchBondRefresh(()=>void load(),()=>!request.current,()=>lastAttempt.current,()=>failed.current,()=>country==='USA'?bondPollInterval():['CHN','JPN','GBR','CAN','SGP'].includes(country)?60_000:300_000);
  const manual=()=>void load(true);window.addEventListener('jr-trading-workbench:refresh-bonds',manual);
  return()=>{stop();window.removeEventListener('jr-trading-workbench:refresh-bonds',manual);const pending=request.current;request.current=null;pending?.abort();};
 },[load]);
 const rows=filterBondRows(market?.rows??[],period,market?.latest_observation_date??null);
 const countryInfo=BOND_COUNTRIES.find(item=>item.code===country)!;
 const auctionCountry=country==='JPN'||country==='GBR';
 const tenor=TENORS.find(item=>item.key===selected)!;
 const selectedQuote=market?.quotes.find(item=>item.key===selected);
 const seriesUrl=market?.sources.find(item=>item.series_id===selectedQuote?.series_id)?.url;
 const message=error||market?.error||(market?.status==='disabled'?'Official yield source checks are disabled on this server.':'');
 const sourceChecked=market?.status==='ready'&&!error;
 return <>
  <PageTitle eyebrow="MARKETS" title="Bonds" description="Official sovereign yields, curve spreads and butterflies." actions={<><button className="button secondary" disabled={!market?.rows.length} onClick={()=>download(`${country.toLowerCase()}-sovereign-yields.json`,market)}><Download size={15}/>Export data</button><button className="button primary" disabled={busy} onClick={()=>void load(true)}><RefreshCw size={15} className={busy?'spin':''}/>{busy?'Checking source…':'Refresh yields'}</button></>}/>
  <div className="bop-country-tabs bonds-country-tabs" role="group" aria-label="Bonds country">{BOND_COUNTRIES.map(item=><button key={item.code} type="button" aria-pressed={country===item.code} className={country===item.code?'active':''} onClick={()=>{if(item.code!==country){setMarket(null);setError('');setCountry(item.code);}}}><span>{item.label}</span><small>{item.currency}</small></button>)}</div>
  <div className="bonds-source"><Badge tone={sourceChecked?'green':'neutral'}>{sourceChecked?(auctionCountry?'Official releases':'Official daily data'):busy?'Checking source':country==='GBR'&&market?.source_warning?'3M update interrupted':market?.rows.length?'Saved daily data':'Awaiting official data'}</Badge><span>{market?.source??countryInfo.name}</span><span>{market?.latest_observation_date?`Latest observation ${day(market.latest_observation_date)}`:'Observation date unavailable'}</span><span>{market?.last_checked_at?`${country==='GBR'?'Gilt source':'Source'} checked ${stamp(market.last_checked_at)} SGT`:'No successful source check yet'}</span>{country==='GBR'&&market?.source_checks?.bills.last_checked_at&&<span>3M API last verified {stamp(market.source_checks.bills.last_checked_at)} SGT</span>}</div>
  <p className="bonds-frequency">{country==='USA'?'Automatically checks every minute around the U.S. daily release and every 5 minutes otherwise.':country==='CHN'?'Automatically checks the ChinaBond API every minute.':country==='JPN'?'Automatically checks the Japan data API every minute for new Ministry of Finance releases.':country==='GBR'?'Automatically checks the UK data API every minute for Bank of England gilt releases and UK DMO bill tender results.':country==='CAN'?'Automatically checks the Bank of Canada Valet JSON API every minute for new Canadian Treasury-bill and government bond observations.':'Automatically checks the Singapore data API every minute for new official MAS SGS benchmark observations.'} New observations update all charts automatically. Figures follow each source's publication schedule.</p>
  {market?.coverage_note&&<p className="bonds-coverage">{market.coverage_note}</p>}
  {message&&<div className="equity-error" role="alert"><strong>Yield updates interrupted</strong><p>{message}</p><button className="button secondary tiny" disabled={busy} onClick={()=>void load(true)}>Retry source</button></div>}
  {(market?.source_warning||market?.cache_warning||market?.observation_warning)&&<p className="bonds-warning" role="status">{[market.source_warning,market.cache_warning,market.observation_warning].filter(Boolean).join(' ')}</p>}
  <div className="bonds-quotes">{TENORS.map(item=>{const quote=market?.quotes.find(value=>value.key===item.key);return <button type="button" className={`metric metric-select ${selected===item.key?'chosen':''}`} key={item.key} disabled={!!market&&!quote?.series_id} aria-pressed={selected===item.key} onClick={()=>setSelected(item.key)}><div className="metric-label">{countryInfo.prefix} {item.label} {auctionCountry&&item.key==='m3'?(country==='GBR'?'bill tender yield':'auction yield'):'yield'}<ArrowUpRight size={14}/></div><div className="metric-value">{quote?.value==null?'—':`${number(quote.value,2)}%`}</div><div className="metric-foot"><span>{quote?.date?day(quote.date):market&&!quote?.series_id?(country==='SGP'&&item.key==='m3'?'Discontinued June 2013':'Not published by this source'):'Not available'}</span><span>{bp(quote?.change_bp)}{quote?.previous_date?' vs prior observation':''}</span></div></button>;})}</div>
  <div className="bonds-range"><span>Chart history</span><div className="segmented" aria-label="Sovereign yield chart date range">{['1M','3M','1Y','5Y','10Y','All'].map(value=><button key={value} className={period===value?'active':''} aria-pressed={period===value} onClick={()=>setPeriod(value)}>{value}</button>)}</div></div>
  <div className="bonds-top-charts">
   <Panel title={`${countryInfo.prefix} ${tenor.label} · yield history`} subtitle={`${auctionCountry&&selected==='m3'?(country==='GBR'?'UK Treasury-bill average accepted tender yield':'Treasury-bill auction average-price yield'):market?.yield_basis??'Government yields'} · percent per annum`}><BondHistory rows={rows} field={selected} unit="%" observationsOnly={auctionCountry&&selected==='m3'}/><div className="chart-footer"><span>{rows.filter(row=>row[selected]!==null).length} published observations</span>{seriesUrl&&<a href={seriesUrl} target="_blank" rel="noopener noreferrer">View source<ArrowUpRight size={11}/></a>}</div></Panel>
   <Panel title={`${countryInfo.prefix} government yield curve`} subtitle={market?.curve_date?`Common observation date · ${day(market.curve_date)}`:'Matching-date published maturities required'}><div className="bonds-history">{market?.curve.length?<ResponsiveContainer width="100%" height="100%"><LineChart data={market.curve} margin={{top:12,right:18,left:0,bottom:4}}><CartesianGrid vertical={false} stroke="#e9edf2" strokeDasharray="3 4"/><XAxis dataKey="years" type="number" ticks={[.25,2,5,10,30]} domain={[.25,30]} tickFormatter={value=>value===.25?'3M':`${value}Y`} axisLine={false} tickLine={false}/><YAxis width={60} domain={['auto','auto']} tickFormatter={value=>`${number(value,1)}%`} axisLine={false} tickLine={false}/><Tooltip labelFormatter={value=>Number(value)===.25?'3M':`${value}Y`} formatter={(value,name)=>[`${number(Number(value),2)}%`,name==='value'?'Latest common date':'Previous common date']} contentStyle={{borderRadius:8}}/><Line dataKey="previous" stroke="#bdc8d4" strokeDasharray="4 4" dot={false} connectNulls={false} isAnimationActive={false}/><Line dataKey="value" stroke="#388675" strokeWidth={2} dot={{r:3}} isAnimationActive={false}/></LineChart></ResponsiveContainer>:<div className="bonds-chart-empty">{busy?'Loading official yields…':'No matching-date published curve loaded.'}</div>}</div><div className="chart-footer"><span>Green: {market?.curve_date??'—'}</span><span>Dashed: {market?.previous_curve_date??'—'}</span></div></Panel>
  </div>
  {(['spread','butterfly'] as const).map(kind=><details key={kind} className="bonds-analytics"><summary className="bonds-analytics-toggle"><span><span className="bonds-analytics-title">{kind==='spread'?'Yield curve spreads':'Yield curve butterflies'}</span><span className="bonds-analytics-description">{kind==='spread'?'Longer maturity minus shorter maturity · basis points':'Twice the belly yield minus the two wing yields · basis points · unweighted'}</span></span><ChevronDown size={18} aria-hidden="true"/></summary><div className="bonds-analytics-grid">{CURVE_METRICS.filter(item=>item.kind===kind).map(item=>{const metric=market?.metrics.find(value=>value.key===item.key);return <Panel key={item.key} title={item.key} subtitle={`${item.formula} · bp`} actions={<Badge>{metric?.date??(country==='SGP'&&item.key==='3m10s'?'3M discontinued':'No observation')}</Badge>}><div className="bonds-measure"><strong>{bp(metric?.value)}</strong><span>{metric?.change_bp==null?'':`${bp(metric.change_bp)} vs prior observation`}</span></div><BondHistory rows={rows} field={item.key} unit="bp" color={item.color} observationsOnly={auctionCountry&&item.key==='3m10s'} emptyMessage={country==='SGP'&&item.key==='3m10s'?'MAS ended 3M Treasury-bill issuance in June 2013. No current 3m10s series is available.':undefined}/><div className="chart-footer"><span>Published dates · same-date legs · missing legs stay gaps</span><span>{rows.filter(row=>row[item.key]!==null).length} observations</span></div></Panel>;})}</div></details>)}
 </>;
}
