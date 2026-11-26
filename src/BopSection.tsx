import {useCallback,useEffect,useRef,useState} from 'react';
import {ArrowUpRight,Globe2,RefreshCw} from 'lucide-react';
import {api} from './api';
import {Metric,number} from './components';
import BopChart from './BopChart';
import BopMacroPanels from './BopMacroPanels';
import type {MacroData} from './BopMacroPanels';
import {watchBopRefresh} from './bopRefresh';
import {filterBopRange,latestBopDate} from './bopFilters';
import type {BopFrequency} from './bopFilters';
import './bop.css';

type Frequency=BopFrequency;
type Field='current_account'|'goods'|'services'|'primary_income'|'secondary_income'|'net_lending'|'financial_account'|'statistical_discrepancy'|'income_receipts'|'income_payments'|'customs_goods'|'customs_exports'|'customs_imports'|'goods_gap'|'capital_account'|'net_financial_inflow';
type Row={date:string;period:string;bop_goods_nsa?:number|null}&Record<Field,number|null>;
type Country={code:string;name:string;label:string;currency:string;connected:boolean};
type Source={name:string;url:string;retrieved_at:string;series:{field:string;id:string;name:string;url:string}[]};
export type BopData={macro?:MacroData;country:Country;countries:Country[];frequency:Frequency;status:'ready'|'not_connected';message?:string;units?:string;reporting_currency?:string;seasonal_adjustment?:string;retrieved_at?:string;census_latest_month?:string;customs_latest_month?:string;series:Row[];latest:Row|null;insights:{title:string;text:string;tone:string}[];sources:Source[];methodology?:string[];reference_url:string;refresh_error?:string|null;refresh_available?:boolean;refresh_cooldown_seconds?:number;delivery?:'source_checked'|'saved_snapshot';last_attempt_at?:string|null;last_successful_check_at?:string;source_warning?:string|null;period_start?:string;period_end?:string;contributing_quarters?:Row[];latest_quarter?:Row;validation?:{status:string;tolerance_usd_millions?:number;tolerance_jpy_billions?:number;checks:{name:string;observations_checked:number;max_difference_usd_millions?:number;max_difference_jpy_billions?:number}[]}};
const COUNTRIES:Country[]=[
 {code:'USA',name:'United States',label:'USA',currency:'USD',connected:true},
 {code:'CHN',name:'China',label:'China',currency:'CNY',connected:true},
 {code:'JPN',name:'Japan',label:'Japan',currency:'JPY',connected:true},
 {code:'GBR',name:'United Kingdom',label:'UK',currency:'GBP',connected:true},
 {code:'CAN',name:'Canada',label:'Canada',currency:'CAD',connected:true},
 {code:'AUS',name:'Australia',label:'Australia',currency:'AUD',connected:true},
 {code:'NZL',name:'New Zealand',label:'New Zealand',currency:'NZD',connected:true},
 {code:'CHE',name:'Switzerland',label:'Switzerland',currency:'CHF',connected:true},
 {code:'SGP',name:'Singapore',label:'Singapore',currency:'SGD',connected:true},
];
const COLORS={goods:'#cf805f',services:'#388675',primary_income:'#8c78b1',secondary_income:'#a1abba',current_account:'#233a57',customs:'#cc7656',gap:'#6979b4',receipts:'#388675',payments:'#cf805f',financing:'#6e83b5',discrepancy:'#b08a4a'};
const amount=(value:number|null|undefined,currency:string)=>value==null?'Unavailable':`${value<0?'-':'+'}${currency==='CAD'?'C$':currency==='CHF'?'CHF ':currency==='NZD'?'NZ$':currency==='AUD'?'A$':currency==='SGD'?'S$':currency==='GBP'?'£':currency==='JPY'?'¥':'$'}${number(Math.abs(value),1)}bn`;
const retrieved=(value:string)=>new Date(value).toLocaleString('en-SG',{timeZone:'Asia/Singapore',year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false});

export function BopContent({data,range}:{data:BopData;range:string}){
 const endDate=latestBopDate(data.series,data.macro?.quarterly??[],data.macro?.trade??[]);
 const rows=filterBopRange(data.series,range,endDate);
 const latest=data.latest;
 if(!latest)return null;
 const currency=data.reporting_currency??data.country.currency;
 const japan=data.country.code==='JPN';
 const china=data.country.code==='CHN';
 const uk=data.country.code==='GBR';
 const singapore=data.country.code==='SGP';
 const australia=data.country.code==='AUS';
 const newZealand=data.country.code==='NZL';
 const swiss=data.country.code==='CHE';
 const canada=data.country.code==='CAN';
 const originalComparison=uk||australia;
 const amountUnit=`${currency} bn`;
 const unit=`${amountUnit} · ${data.frequency==='trailing4q'?'rolling 4-quarter sum':'quarterly flow'} · ${data.seasonal_adjustment==='Not seasonally adjusted'?'NSA':'SA'}`;
 const rolling=data.frequency==='trailing4q';
 const foot=(rolling?`${data.period_start??''} to ${latest.period} · four-quarter total`:`${latest.period} only · not annualized`)+(china?' · USD':'');
 return <>
  <div className="metrics-grid bop-metrics"><Metric accent label="Current account" value={amount(latest.current_account,currency)} foot={uk?`${foot} · SA · includes precious metals`:foot}/><Metric label="Goods balance · BOP" value={amount(latest.goods,currency)} foot={uk?`${foot} · SA`:foot}/><Metric label="Services balance" value={amount(latest.services,currency)} foot={uk?`${foot} · SA`:foot}/><Metric label="Customs − BOP goods" value={amount(latest.goods_gap,currency)} foot={latest.goods_gap==null?'Matching complete customs quarters required':originalComparison?`${foot} · NSA`:foot}/></div>
  <div className="bop-chart-grid">
   <BopChart title="Current account: the building blocks" subtitle={unit} unit={amountUnit} rows={rows} stacked plots={[
    {key:'goods',name:'Goods',color:COLORS.goods,bar:true,stack:'ca'}, {key:'services',name:'Services',color:COLORS.services,bar:true,stack:'ca'},
    {key:'primary_income',name:'Primary income',color:COLORS.primary_income,bar:true,stack:'ca'},{key:'secondary_income',name:'Secondary income',color:COLORS.secondary_income,bar:true,stack:'ca'},
    {key:'current_account',name:'Current account',color:COLORS.current_account},
   ]} note="Goods + services + primary income + secondary income = current account. Positive and negative components stack separately; the line shows their net balance."/>
   <BopChart title="Customs vs. BOP goods balance" subtitle={originalComparison?unit.replace(/SA$/,'NSA'):unit} unit={amountUnit} rows={rows} plots={[{key:'customs_goods',name:canada?'Statistics Canada customs':swiss?'FOCBS broad customs via SNB':newZealand?'Stats NZ merchandise':australia?'ABS merchandise':singapore?'Enterprise Singapore customs':uk?'HMRC customs':china?'GACC customs via NBS':japan?'Japan Customs':'Census / customs',color:COLORS.customs},{key:originalComparison?'bop_goods_nsa':'goods',name:'BOP goods',color:COLORS.current_account,dash:true}]} note={canada?'Statistics Canada all-country customs merchandise trade versus BOP goods, CAD bn, both NSA. Canadian customs exports and imports are valued FOB. Economic ownership, coverage, valuation, timing and revisions can explain the gap. Only complete three-month customs quarters are used.':swiss?'FOCBS broad foreign trade as published by the SNB, including non-monetary gold, versus full BOP goods, CHF bn, both NSA. Customs imports are CIF; BOP imports are FOB and follow economic ownership. Merchanting, supplements, freight, timing and revisions also affect the gap. This differs from the monthly business-cycle total 1 below, which excludes precious-metal and other volatile categories.':newZealand?'Stats NZ merchandise exports (including re-exports, FOB) minus imports (CIF) versus BOP goods (economic ownership, imports FOB), NZD bn, both NSA. Coverage, ownership, merchanting, freight, timing and revisions can explain the gap.':australia?'ABS merchandise exports at FOB value minus imports at customs value versus original BOP goods, both NSA. Imports at customs value exclude international freight and insurance. Ownership, merchanting, coverage, commodity price and timing adjustments and revisions can explain the gap. The SA goods headline above uses a separate series.':singapore?'Enterprise Singapore merchandise trade versus DOS BOP goods, SGD bn, both NSA. Merchandise exports include domestic exports and re-exports. Customs imports are CIF; BOP imports are FOB and follow economic ownership. Merchanting, goods for processing, freight, coverage and revisions can explain the gap.':uk?'HMRC world goods trade versus ONS LQCT BOP goods, both NSA and including non-monetary gold. The SA goods headline above uses BOKI and can differ. Customs imports are CIF; BOP imports are FOB. Ownership, merchanting, processing, Brexit collection changes and revisions also affect the gap.':china?'China Customs (GACC via NBS) versus SAFE BOP goods, USD bn, both NSA. Quarter flows are differences of official year-to-date customs totals, preserving combined January–February reports. Customs imports are CIF, BOP imports FOB. Ownership, processing and merchanting, timing and revisions also affect this gap; it is not a direct misreporting measure.':japan?'Japan Customs world goods trade (exports FOB, imports CIF) versus BOJ/MOF BOP goods (economic ownership; imports FOB). Both are unadjusted. Coverage, freight, insurance, timing and revisions can explain the gap.':'The US counterpart to the article\'s goods comparison. Customs covers border movements; BOP follows economic ownership. No estimated goods balance is added.'}/>
  </div>
  <BopMacroPanels key={data.country.code+data.frequency+range} data={data.macro} range={range} frequency={data.frequency} endDate={endDate}/>
 </>;
}

export default function BopSection(){
 const [country,setCountry]=useState('USA');const [frequency,setFrequency]=useState<Frequency>('trailing4q');const [range,setRange]=useState('10Y');
 const [data,setData]=useState<BopData|null>(null);const [busy,setBusy]=useState(true);const [error,setError]=useState('');const [refreshing,setRefreshing]=useState(false);
 const sequence=useRef(0);const request=useRef<AbortController|null>(null);
 const checkedCountry=useRef<string|null>(null);
 const inFlight=useRef(false);const lastSourceAttempt=useRef(0);
 const load=useCallback(async(refresh=false,background=false)=>{
  inFlight.current=true;if(refresh)lastSourceAttempt.current=Date.now();
  const id=++sequence.current;request.current?.abort();const controller=new AbortController();request.current=controller;setBusy(true);setRefreshing(refresh);setError('');
  const path=`/fx/bop?country=${country}&frequency=${frequency}`;
  try{
   if(refresh&&!background){try{const cached=await api<BopData>(`${path}&refresh=false`,{signal:controller.signal,cache:'no-store'});if(id===sequence.current)setData(cached);}catch{/* The live request can recover when no saved dataset exists. */}}
   if(id!==sequence.current)return;
   const result=await api<BopData>(`${path}&refresh=${refresh}`,{signal:controller.signal,cache:'no-store'});if(id===sequence.current){setData(result);if(refresh)checkedCountry.current=country;}
  }
  catch(e){if(id===sequence.current)setError(e instanceof Error?e.message:'BOP data could not be loaded.');}
  finally{if(id===sequence.current){inFlight.current=false;setBusy(false);setRefreshing(false);}}
 },[country,frequency]);
 useEffect(()=>{void load(checkedCountry.current!==country);return()=>{sequence.current++;request.current?.abort();inFlight.current=false;};},[load,country]);
 useEffect(()=>{
  if(!COUNTRIES.find(item=>item.code===country)?.connected||data?.refresh_available===false)return;
  return watchBopRefresh(()=>void load(true,true),()=>!inFlight.current,()=>lastSourceAttempt.current);
 },[load,country,data?.refresh_available]);
 useEffect(()=>{const refresh=()=>void load(true);window.addEventListener('jr-trading-workbench:refresh-fx',refresh);window.addEventListener('jr-trading-workbench:refresh-bop',refresh);return()=>{window.removeEventListener('jr-trading-workbench:refresh-fx',refresh);window.removeEventListener('jr-trading-workbench:refresh-bop',refresh);};},[load]);
 const shown=data?.country.code===country&&data.frequency===frequency?data:null;
 const selected=(shown?.countries??COUNTRIES).find(item=>item.code===country)!;
 return <section className="bop-section" id="fx-bop" aria-labelledby="bop-title" aria-busy={busy}>
  <header className="bop-heading"><div><span className="eyebrow">MACRO / EXTERNAL SECTOR</span><h2 id="bop-title"><Globe2 size={23}/>Balance of payments</h2></div><div className="bop-heading-actions"><button className="button secondary" disabled={busy||!selected.connected||shown?.refresh_available===false} onClick={()=>void load(true)}><RefreshCw size={14} className={refreshing?'spin':''}/>{refreshing?'Checking sources…':'Refresh data'}</button></div></header>
  <div className="bop-country-tabs" role="group" aria-label="BOP country">{(shown?.countries??COUNTRIES).map(item=><button key={item.code} type="button" aria-pressed={country===item.code} className={country===item.code?'active':''} onClick={()=>setCountry(item.code)}><span>{item.label}</span><small>{item.connected?item.currency:'Not connected'}</small></button>)}</div>
  <div className="bop-controls"><div><strong>{selected.name}</strong><span>{selected.currency} · external accounts{shown?.reporting_currency&&shown.reporting_currency!==selected.currency?` · amounts in ${shown.reporting_currency}`:''}</span></div><div className="bop-control-groups"><div className="segmented" role="group" aria-label="BOP frequency"><button aria-pressed={frequency==='quarterly'} className={frequency==='quarterly'?'active':''} onClick={()=>setFrequency('quarterly')}>Quarterly</button><button aria-pressed={frequency==='trailing4q'} className={frequency==='trailing4q'?'active':''} onClick={()=>setFrequency('trailing4q')}>Rolling 4Q</button></div><div className="segmented" role="group" aria-label="BOP date range">{['1Y','5Y','10Y','All'].map(value=><button key={value} aria-pressed={range===value} className={range===value?'active':''} onClick={()=>setRange(value)}>{value}</button>)}</div></div></div>
  {error&&<div className="bop-message error" role="alert"><strong>BOP request failed</strong><p>{error}</p>{shown?.status==='ready'&&<p>The previous snapshot is still shown with its original date.</p>}<button className="button secondary tiny" onClick={()=>void load(true)}>Retry BOP</button></div>}
  {refreshing&&selected.connected&&<div className="bop-message" role="status">Checking the latest official {country==='CAN'?'Statistics Canada and BIS':country==='CHE'?'SNB, SECO, FOCBS and BIS':country==='NZL'?'Stats NZ, Infoshare and BIS':country==='AUS'?'ABS and BIS':country==='SGP'?'SingStat, MAS, Enterprise Singapore and BIS':country==='GBR'?'ONS, HMRC and Bank of England':country==='CHN'?'SAFE, NBS, China Customs and BIS':country==='JPN'?'Bank of Japan, Japan Customs and Cabinet Office':'FRED, Census and Treasury'} data… Saved figures remain visible until the source check finishes.</div>}
  {shown?.refresh_error&&<div className="bop-message error" role="alert"><strong>Source refresh unavailable</strong><p>{shown.refresh_error}</p>{shown.last_attempt_at&&<p>Last attempt: {retrieved(shown.last_attempt_at)} SGT.{(shown.refresh_cooldown_seconds??0)>0?` Retry available in ${shown.refresh_cooldown_seconds}s.`:''}</p>}</div>}
  {shown?.source_warning&&<p className="bop-message">{shown.source_warning}</p>}
  {!shown&&busy&&<div className="bop-message" role="status">Loading {selected.name} external accounts…</div>}
  {shown?.status==='not_connected'&&<div className="bop-country-empty"><Globe2 size={28}/><h3>{selected.name} BOP data is not connected</h3><p>{shown.message}</p><button className="button primary" onClick={()=>setCountry('USA')}>View USA data<ArrowUpRight size={14}/></button></div>}
  {shown?.status==='ready'&&<BopContent data={shown} range={range}/>}
 </section>;
}
