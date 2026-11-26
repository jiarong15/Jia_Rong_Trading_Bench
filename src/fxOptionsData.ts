export type GreekMetric='gamma'|'delta'|'vega'|'notional';
export type OptionSide='call'|'put';
export type HeatmapCell={row:number;column:number;side:OptionSide;count:number;modelled_count?:number;gamma:number|null;delta:number|null;vega:number|null;notional:number|null};
export type HeatmapData={pair:string;status:string;basis:'reported_trades'|'open_interest'|'not_connected';source:string;source_url:string|null;checked_at:string|null;as_of:string|null;last_trade_at?:string|null;spot:number|null;spot_at:string|null;spot_date?:string|null;volatility_published_at?:string|null;market_source?:string;market_source_url?:string|null;market_checked_at?:string|null;pricing_checked_at?:string|null;pricing_date?:string|null;volatility_curve?:{tenor:string;years:number;volatility_pct:number;bid_pct:number;ask_pct:number}[];window_start:string|null;window_end:string|null;rows:string[];columns:string[];cells:HeatmapCell[];metrics:Record<GreekMetric,{available:boolean;definition:string;coverage_note?:string}>;notices:string[];error:string|null;poll_interval_seconds:number};
export const METRICS:Record<GreekMetric,string>={gamma:'Gamma notional',delta:'Delta notional',vega:'Vega notional',notional:'Notional'};

export function validateHeatmap(data:HeatmapData,pair:string){
 if(data.pair!==pair||!['reported_trades','open_interest','not_connected'].includes(data.basis)||!Array.isArray(data.rows)||!Array.isArray(data.columns)||data.rows.length>40||data.columns.length>20||!Array.isArray(data.cells))throw Error('The FX options response is incomplete or belongs to another pair.');
 const identities=new Set<string>();
 for(const metric of Object.keys(METRICS) as GreekMetric[])if(!data.metrics?.[metric]||typeof data.metrics[metric].available!=='boolean'||typeof data.metrics[metric].definition!=='string')throw Error('Missing FX options metric definitions.');
 for(const cell of data.cells){
  const id=`${cell.row}/${cell.column}/${cell.side}`;
  if(!Number.isInteger(cell.row)||!Number.isInteger(cell.column)||cell.row<0||cell.column<0||cell.row>=data.rows.length||cell.column>=data.columns.length||!['call','put'].includes(cell.side)||identities.has(id)||!Number.isInteger(cell.count)||cell.count<0)throw Error('Invalid FX options heatmap bucket.');
  identities.add(id);
  if(cell.modelled_count!==undefined&&(!Number.isInteger(cell.modelled_count)||cell.modelled_count<0||cell.modelled_count>cell.count))throw Error('Invalid FX options coverage count.');
  for(const metric of Object.keys(METRICS) as GreekMetric[])if(cell[metric]!==null&&(!Number.isFinite(cell[metric])||Number(cell[metric])<0))throw Error('Invalid FX options notional.');
 }
 return data;
}

export function heatmapValues(data:HeatmapData|null,metric:GreekMetric,calls:boolean,puts:boolean){
 const available=Boolean(data?.metrics[metric]?.available)&&data?.basis!=='not_connected';
 const summarize=(cells:HeatmapCell[])=>{
  let count=0,coveredCount=0,value=0;
  for(const cell of cells){
   count+=cell.count;
   if(!available||cell[metric]===null)continue;
   value+=cell[metric];
   coveredCount+=metric==='notional'?cell.count:cell.modelled_count??cell.count;
  }
  return {value:!available||count>0&&coveredCount===0?null:value,count,coveredCount,partial:available&&coveredCount<count};
 };
 const selected=(data?.cells??[]).filter(cell=>cell.side==='call'?calls:puts);
 const buckets=Array.from({length:data?.rows.length??0},()=>Array.from({length:data?.columns.length??0},()=>[] as HeatmapCell[]));
 for(const cell of selected)buckets[cell.row][cell.column].push(cell);
 const summaries=buckets.map(row=>row.map(summarize));
 const rows=buckets.map(row=>summarize(row.flat())),columns=(data?.columns??[]).map((_,index)=>summarize(buckets.flatMap(row=>row[index])));
 const total=summarize(selected),call=summarize((data?.cells??[]).filter(cell=>cell.side==='call')),put=summarize((data?.cells??[]).filter(cell=>cell.side==='put'));
 const grid=summaries.map(row=>row.map(cell=>cell.value)),max=Math.max(0,...grid.flat().filter((v):v is number=>v!==null));
 return {available,grid,gridPartial:summaries.map(row=>row.map(cell=>cell.partial)),rowTotals:rows.map(row=>row.value),rowPartial:rows.map(row=>row.partial),
  columnTotals:columns.map(column=>column.value),columnPartial:columns.map(column=>column.partial),total:total.value,partial:total.partial,coveredCount:total.coveredCount,max,count:total.count,
  ratioPartial:call.partial||put.partial,ratioCoveredCount:call.coveredCount+put.coveredCount,ratioCount:call.count+put.count,
  putCallRatio:call.value!==null&&put.value!==null&&call.value>0?put.value/call.value:null};
}

export function compactUsd(value:number|null){
 if(value===null)return '—';
 const absolute=Math.abs(value),scale=absolute>=1e9?1e9:absolute>=1e6?1e6:absolute>=1e3?1e3:1,suffix=scale===1e9?'bn':scale===1e6?'m':scale===1e3?'k':'';
 return '$'+new Intl.NumberFormat('en-US',{maximumFractionDigits:scale===1?0:1}).format(value/scale)+suffix;
}

export function heatColor(value:number|null,max:number){
 if(value===null)return 'transparent';
 if(value===0||max<=0)return '#f5f7fb';
 const intensity=Math.sqrt(Math.min(1,value/max));
 return `hsl(224 78% ${96-intensity*53}%)`;
}
