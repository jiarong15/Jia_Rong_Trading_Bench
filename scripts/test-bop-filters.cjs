const {test}=require('node:test');
const assert=require('node:assert/strict');
const {filterBopRange,latestBopDate}=require('../src/bopFilters.ts');
const rows=Array.from({length:60},(_,index)=>({date:`${2012+Math.floor(index/4)}-${String(index%4*3+1).padStart(2,'0')}-01`,value:index}));

test('1Y, 5Y, 10Y and All select exact calendar windows at the common latest quarter',()=>{
 const end='2026-04-01';
 const one=filterBopRange(rows,'1Y',end);
 assert.equal(one.length,4);assert.equal(one[0].date,'2025-07-01');assert.equal(one.at(-1).date,end);
 const five=filterBopRange(rows,'5Y',end),ten=filterBopRange(rows,'10Y',end);
 assert.equal(five.length,20);assert.equal(five[0].date,'2021-07-01');assert.equal(five.at(-1).date,end);
 assert.equal(ten.length,40);assert.equal(ten[0].date,'2016-07-01');assert.equal(ten.at(-1).date,end);
 assert.equal(filterBopRange(rows,'All',end).length,60);
});

test('sparse histories never reach further back to fill an observation count',()=>{
 const sparse=rows.filter((_,index)=>index%3!==0);
 const selected=filterBopRange(sparse,'5Y','2026-04-01');
 assert.ok(selected.length<20&&selected.every(row=>row.date>='2021-07-01'&&row.date<='2026-04-01'));
 assert.deepEqual(sparse,rows.filter((_,index)=>index%3!==0));
});

test('every chart uses the same anchor even if one provider releases a quarter later',()=>{
 const core=rows.filter(row=>row.date<='2026-04-01');
 const trade=rows.filter(row=>row.date<='2026-07-01');
 const end=latestBopDate(core,trade,[]);
 assert.equal(end,'2026-07-01');
 assert.equal(filterBopRange(core,'5Y',end)[0].date,'2021-10-01');
 assert.equal(filterBopRange(trade,'5Y',end)[0].date,'2021-10-01');
 assert.equal(filterBopRange(core,'5Y',end).length,19);
 assert.equal(filterBopRange(trade,'5Y',end).length,20);
});

test('empty and shorter histories remain valid for every range',()=>{
 assert.equal(latestBopDate([],[]),undefined);
 assert.deepEqual(filterBopRange([],'5Y'),[]);
 for(const range of ['1Y','5Y','10Y','All'])assert.deepEqual(filterBopRange(rows.slice(-2),range,'2026-10-01'),rows.slice(-2));
});
