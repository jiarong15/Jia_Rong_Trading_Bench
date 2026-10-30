const {test}=require('node:test');
const assert=require('node:assert/strict');
const {normalizeRows}=require('../src/bopNormalize.ts');
const rows=values=>values.map((value,i)=>({date:`2025-${String(i+1).padStart(2,'0')}-01`,period:`M${i+1}`,value}));
const close=(a,b)=>assert.ok(Math.abs(a-b)<1e-10,`${a} != ${b}`);

test('positive, negative and zero-crossing series have zero mean and unit population deviation',()=>{
 for(const values of [[100,110,120,130],[-5,-4,-3,-2],[-10,-1,0,4,15]]){
  const valuesOut=normalizeRows(rows(values),['value']).map(row=>row.value);
  close(valuesOut.reduce((sum,value)=>sum+value,0)/valuesOut.length,0);
  close(valuesOut.reduce((sum,value)=>sum+value*value,0)/valuesOut.length,1);
  assert.ok(valuesOut.every((value,i)=>!i||value>valuesOut[i-1]));
 }
});

test('different units and offsets normalize to the same comparable scale',()=>{
 const input=rows([-4,-3,-2,-1]).map(row=>({...row,dollarIndex:120+row.value*5}));
 const output=normalizeRows(input,['value','dollarIndex']);
 for(const row of output)close(row.value,row.dollarIndex);
});

test('missing and non-finite observations remain gaps without contaminating valid values',()=>{
 const input=rows([1,null,undefined,3,NaN,Infinity]);
 const output=normalizeRows(input,['value','missingSeries']);
 assert.deepEqual(output.map(row=>row.value),[-1,null,null,1,null,null]);
 assert.ok(output.every(row=>row.missingSeries===null));
});

test('constant and single-observation series normalize to zero without dividing by zero',()=>{
 for(const values of [[7,7,7],[.1,.1,.1],[0,0,0],[-3,-3],[-1]]){
  assert.deepEqual(normalizeRows(rows(values),['value']).map(row=>row.value),values.map(()=>0));
 }
 assert.deepEqual(normalizeRows([],['value']),[]);
});

test('normalization leaves original observations, dates and unrelated fields intact',()=>{
 const input=Object.freeze(rows([-2,0,2]).map(row=>Object.freeze({...row,other:42})));
 const output=normalizeRows(input,['value']);
 assert.deepEqual(input.map(row=>row.value),[-2,0,2]);
 assert.notEqual(output[0],input[0]);
 output.forEach((row,i)=>{assert.equal(row.date,input[i].date);assert.equal(row.period,input[i].period);assert.equal(row.other,42);});
});

test('zooming slices the already-normalized range instead of rebasing the visible segment',()=>{
 const input=rows([10,20,30,40,50]);
 const full=normalizeRows(input,['value']);
 const zoomed=full.slice(2,5);
 close(zoomed[0].value,0);
 assert.ok(zoomed[2].value>1.4);
 assert.notEqual(zoomed[0].value,normalizeRows(input.slice(2,5),['value'])[0].value);
});

test('a changed selected range or fresh observations recompute every affected score',()=>{
 const input=rows([1,2,3]);
 close(normalizeRows(input,['value'])[1].value,0);
 assert.ok(normalizeRows(rows([1,2,30]),['value'])[1].value<0);
 assert.ok(normalizeRows(input.slice(1),['value'])[0].value<0);
});

test('small variation around a large offset remains measurable',()=>{
 const output=normalizeRows(rows([1e12,1e12+1,1e12+2]),['value']);
 close(output[1].value,0);close(output[0].value,-Math.sqrt(1.5));close(output[2].value,Math.sqrt(1.5));
});
