export type NormalizableRow={date:string;period:string;[key:string]:string|number|null|undefined};

// Standardize the selected date range before slicing for zoom, so dragging
// never changes the reference mean or standard deviation. Missing values stay
// missing; negative and zero-crossing series require no special denominator.
export function normalizeRows(rows:NormalizableRow[],keys:readonly string[]):NormalizableRow[]{
 const stats=new Map(keys.map(key=>{
  const values=rows.map(row=>row[key]).filter((value):value is number=>typeof value==='number'&&Number.isFinite(value));
  let mean=0,squares=0,count=0;
  for(const value of values){
   count++;const delta=value-mean;mean+=delta/count;squares+=delta*(value-mean);
  }
  const deviation=count?Math.sqrt(Math.max(0,squares/count)):0;
  return [key,{mean,deviation}] as const;
 }));
 return rows.map(row=>{
  const next={...row};
  for(const [key,{mean,deviation}] of stats){
   const value=row[key];
   next[key]=typeof value==='number'&&Number.isFinite(value)?deviation>0?(value-mean)/deviation:0:null;
  }
  return next;
 });
}
