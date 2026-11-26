export type YieldKey='m3'|'y2'|'y5'|'y10'|'y30';
export type MetricKey='2s10s'|'3m10s'|'5s30s'|'5s10s'|'10s30s'|'2s5s'|'2s5s10s'|'2s10s30s';
export type BondRow={date:string}&Record<YieldKey|MetricKey,number|null>;
export type Observation={value:number|null;date:string|null;previous_date:string|null;change_bp:number|null};
export type YieldQuote=Observation&{key:YieldKey;label:string;series_id:string|null;years:number};
export type BondCountry={code:string;name:string;label:string;currency:string;prefix:string;source:string;url:string;basis:string;coverage_note:string};
export const BOND_COUNTRIES=[{code:'USA',label:'USA',currency:'USD',prefix:'US',name:'United States'}, {code:'CHN',label:'China',currency:'CNY',prefix:'China',name:'China'}, {code:'JPN',label:'Japan',currency:'JPY',prefix:'Japan',name:'Japan'}, {code:'GBR',label:'UK',currency:'GBP',prefix:'UK',name:'United Kingdom'}, {code:'CAN',label:'Canada',currency:'CAD',prefix:'Canada',name:'Canada'}, {code:'SGP',label:'Singapore',currency:'SGD',prefix:'Singapore',name:'Singapore'}];
export type CurveMetric=Observation&{key:MetricKey;kind:'spread'|'butterfly';formula:string};
export type BondMarket={country:BondCountry;countries:BondCountry[];coverage_note:string;yield_basis:string;status:'ready'|'stale'|'unavailable'|'disabled';source:string;source_route:string|null;source_warning?:string|null;source_checks?:{gilts:{status:string;last_checked_at:string|null};bills:{status:string;last_checked_at:string|null}}|null;frequency:string;rows:BondRow[];quotes:YieldQuote[];metrics:CurveMetric[];curve:{tenor:string;years:number;value:number;previous:number|null}[];curve_date:string|null;previous_curve_date:string|null;latest_observation_date:string|null;last_checked_at:string|null;last_attempt_at:string|null;error:string|null;cache_warning:string|null;observation_warning:string|null;notice:string;sources:{tenor:string;series_id:string;url:string}[]};
export const TENORS:{key:YieldKey;label:string;series_id:string}[]=[{key:'m3',label:'3M',series_id:'DGS3MO'},{key:'y2',label:'2Y',series_id:'DGS2'},{key:'y5',label:'5Y',series_id:'DGS5'},{key:'y10',label:'10Y',series_id:'DGS10'},{key:'y30',label:'30Y',series_id:'DGS30'}];
export const CURVE_METRICS:{key:MetricKey;kind:'spread'|'butterfly';formula:string;color:string}[]=[
 {key:'2s10s',kind:'spread',formula:'10Y − 2Y',color:'#388675'},
 {key:'3m10s',kind:'spread',formula:'10Y − 3M',color:'#527dcc'},
 {key:'5s30s',kind:'spread',formula:'30Y − 5Y',color:'#8c78b1'},
 {key:'5s10s',kind:'spread',formula:'10Y − 5Y',color:'#c18451'},
 {key:'10s30s',kind:'spread',formula:'30Y − 10Y',color:'#4e8d9a'},
 {key:'2s5s',kind:'spread',formula:'5Y − 2Y',color:'#a56578'},
 {key:'2s5s10s',kind:'butterfly',formula:'2 × 5Y − 2Y − 10Y',color:'#8c78b1'},
 {key:'2s10s30s',kind:'butterfly',formula:'2 × 10Y − 2Y − 30Y',color:'#527dcc'}
];

export function filterBondRows(rows:BondRow[],period:string,end:string|null){
 if(period==='All'||!end)return rows;
 const anchor=new Date(end+'T00:00:00Z');const cut=new Date(anchor);
 if(period==='1M'||period==='3M'){
  const day=cut.getUTCDate();cut.setUTCDate(1);cut.setUTCMonth(cut.getUTCMonth()-(period==='1M'?1:3));
  const last=new Date(Date.UTC(cut.getUTCFullYear(),cut.getUTCMonth()+1,0)).getUTCDate();cut.setUTCDate(Math.min(day,last));
 }else{
  const year=cut.getUTCFullYear()-({'1Y':1,'5Y':5,'10Y':10}[period]??1),month=cut.getUTCMonth(),day=cut.getUTCDate();
  cut.setUTCDate(1);cut.setUTCFullYear(year);const last=new Date(Date.UTC(year,month+1,0)).getUTCDate();cut.setUTCDate(Math.min(day,last));
 }
 const start=cut.toISOString().slice(0,10);
 return rows.filter(row=>row.date>=start&&row.date<=end);
}

// 20:00–23:00 UTC covers 16:15 Eastern and subsequent FRED publication in both DST seasons.
export function bondPollInterval(now=Date.now()){
 const utc=new Date(now),weekday=utc.getUTCDay(),hour=utc.getUTCHours();
 return weekday>=1&&weekday<=5&&hour>=20&&hour<23?60_000:300_000;
}

export function watchBondRefresh(refresh:()=>void,canRefresh:()=>boolean,lastAttempt:()=>number,failed:()=>boolean=()=>false,cadence:()=>number=()=>bondPollInterval()){
 let reconnect=false;
 const check=()=>{
  if(!document.hidden&&canRefresh()&&(reconnect||Date.now()-lastAttempt()>=(failed()?30_000:cadence()))){reconnect=false;refresh();}
 };
 const tick=()=>check(),online=()=>{reconnect=true;check();};
 // Reevaluate the cadence on entering the release window and after device sleep.
 const timer=window.setInterval(tick,1_000);
 document.addEventListener('visibilitychange',tick);window.addEventListener('focus',tick);window.addEventListener('online',online);
 return()=>{window.clearInterval(timer);document.removeEventListener('visibilitychange',tick);window.removeEventListener('focus',tick);window.removeEventListener('online',online);};
}
