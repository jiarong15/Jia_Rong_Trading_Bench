export type GreekMetric='gamma'|'delta'|'vega'|'notional';
export type OptionSide='call'|'put';
export type HeatmapCell={row:number;column:number;side:OptionSide;count:number;gamma:number|null;delta:number|null;vega:number|null;notional:number|null};
export type HeatmapData={pair:string;status:string;basis:'reported_trades'|'open_interest'|'not_connected';source:string;source_url:string|null;checked_at:string|null;as_of:string|null;last_trade_at?:string|null;spot:number|null;spot_at:string|null;spot_date?:string|null;volatility_published_at?:string|null;window_start:string|null;window_end:string|null;rows:string[];columns:string[];cells:HeatmapCell[];metrics:Record<GreekMetric,{available:boolean;definition:string;coverage_note?:string}>;notices:string[];error:string|null;poll_interval_seconds:number};
export const METRICS:Record<GreekMetric,string>={gamma:'Gamma notional',delta:'Delta notional',vega:'Vega notional',notional:'Notional'};

export function validateHeatmap(data:HeatmapData,pair:string){
 if(data.pair!==pair||!['reported_trades','open_interest','not_connected'].includes(data.basis)||!Array.isArray(data.rows)||!Array.isArray(data.columns)||data.rows.length>40||data.columns.length>20||!Array.isArray(data.cells))throw Error('The FX options response is incomplete or belongs to another pair.');
 const identities=new Set<string>();
 for(const metric of Object.keys(METRICS) as GreekMetric[])if(!data.metrics?.[metric]||typeof data.metrics[metric].available!=='boolean'||typeof data.metrics[metric].definition!=='string')throw Error('Missing FX options metric definitions.');
 for(const cell of data.cells){
  const id=`${cell.row}/${cell.column}/${cell.side}`;
  if(!Number.isInteger(cell.row)||!Number.isInteger(cell.column)||cell.row<0||cell.column<0||cell.row>=data.rows.length||cell.column>=data.columns.length||!['call','put'].includes(cell.side)||identities.has(id)||!Number.isInteger(cell.count)||cell.count<0)throw Error('Invalid FX options heatmap bucket.');
  identities.add(id);
  for(const metric of Object.keys(METRICS) as GreekMetric[])if(cell[metric]!==null&&(!Number.isFinite(cell[metric])||Number(cell[metric])<0))throw Error('Invalid FX options notional.');
 }
 return data;
}

export function heatmapValues(data:HeatmapData|null,metric:GreekMetric,calls:boolean,puts:boolean){
 const available=Boolean(data?.metrics[metric]?.available)&&data?.basis!=='not_connected';
 const grid=Array.from({length:data?.rows.length??0},()=>Array<number|null>(data?.columns.length??0).fill(available?0:null));
 let callTotal:number|null=0,putTotal:number|null=0,count=0;
 if(data)for(const cell of data.cells){
  const selected=cell.side==='call'?calls:puts;
  if(selected)count+=cell.count;
  if(!available)continue;
  const value=cell[metric];
  if(value===null){if(cell.side==='call')callTotal=null;else putTotal=null;if(cell.side==='call'?calls:puts)grid[cell.row][cell.column]=null;continue;}
  if(cell.side==='call'){if(callTotal!==null)callTotal+=value;}else if(putTotal!==null)putTotal+=value;
  if(cell.side==='call'?!calls:!puts)continue;
  const prior=grid[cell.row][cell.column];grid[cell.row][cell.column]=prior===null?null:prior+value;
 }
 const sum=(values:(number|null)[])=>values.some(value=>value===null)?null:values.reduce<number>((total,value)=>total+(value??0),0);
 const rowTotals=grid.map(sum),columnTotals=(data?.columns??[]).map((_,index)=>sum(grid.map(row=>row[index])));
 const total=available?sum(rowTotals):null,max=Math.max(0,...grid.flat().filter((v):v is number=>v!==null));
 return {available,grid,rowTotals,columnTotals,total,max,count,putCallRatio:available&&callTotal!==null&&putTotal!==null&&callTotal>0?putTotal/callTotal:null};
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
