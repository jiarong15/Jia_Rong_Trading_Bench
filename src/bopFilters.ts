export type BopFrequency='quarterly'|'trailing4q';

const quarterIndex=(date:string)=>Number(date.slice(0,4))*4+Math.floor((Number(date.slice(5,7))-1)/3);

export function latestBopDate(...groups:Array<ReadonlyArray<{date:string}>>):string|undefined{
 let latest:string|undefined;
 for(const rows of groups)for(const row of rows)if(!latest||row.date>latest)latest=row.date;
 return latest;
}

// One calendar window for every chart, including histories with missing dates
// or fewer observations. Slice-by-count can otherwise extend a 5Y view past 5Y.
export function filterBopRange<T extends {date:string}>(rows:T[],range:string,endDate?:string):T[]{
 const years=range==='1Y'?1:range==='5Y'?5:range==='10Y'?10:null;
 if(!endDate||years===null)return rows;
 const end=quarterIndex(endDate),start=end-years*4+1;
 return rows.filter(row=>{const at=quarterIndex(row.date);return at>=start&&at<=end;});
}
