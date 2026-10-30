const {test}=require('node:test');
const assert=require('node:assert/strict');
const {clampZoomPoint,selectedZoom}=require('../src/bopZoom.ts');
const periods=['2025 Q1','2025 Q2','2025 Q3','2025 Q4'];
const invertX=x=>periods[Math.max(0,Math.min(3,Math.round(x/100)))];
const invertY=y=>200-y*2;

test('rectangle selects the actual quarter range and both value-axis bounds',()=>{
 assert.deepEqual(selectedZoom({x:100,y:20},{x:300,y:100},periods,invertX,invertY),{from:'2025 Q2',to:'2025 Q4',y:[0,160]});
});
test('all four drag directions select the same area',()=>{
 for(const [start,end] of [[{x:100,y:20},{x:300,y:100}],[{x:300,y:100},{x:100,y:20}],[{x:100,y:100},{x:300,y:20}],[{x:300,y:20},{x:100,y:100}]]){
  assert.deepEqual(selectedZoom(start,end,periods,invertX,invertY),{from:'2025 Q2',to:'2025 Q4',y:[0,160]});
 }
});
test('horizontal drag selects dates with automatic value-axis scaling',()=>{
 assert.deepEqual(selectedZoom({x:0,y:20},{x:300,y:24},periods,invertX,invertY),{from:'2025 Q1',to:'2025 Q4',y:null});
});
test('ordinary clicks and a single quarter do not create empty zooms',()=>{
 assert.equal(selectedZoom({x:20,y:20},{x:23,y:21},periods,invertX,invertY),null);
 assert.equal(selectedZoom({x:10,y:20},{x:20,y:100},periods,invertX,invertY),null);
});
test('repeated zoom uses the currently visible quarters and value scale',()=>{
 const visible=periods.slice(1);
 assert.deepEqual(selectedZoom({x:0,y:0},{x:100,y:50},visible,x=>visible[Math.round(x/100)],y=>80-y),{from:'2025 Q2',to:'2025 Q3',y:[30,80]});
});
test('dragging beyond the chart edges clamps both coordinates to the plot',()=>{
 const area={x:20,y:10,width:300,height:150};
 assert.deepEqual(clampZoomPoint({x:-100,y:900},area),{x:20,y:160});
 assert.deepEqual(clampZoomPoint({x:900,y:-100},area),{x:320,y:10});
});
test('invalid scale outputs and non-finite points cannot corrupt a domain',()=>{
 assert.equal(selectedZoom({x:0,y:0},{x:300,y:100},periods,()=>undefined,invertY),null);
 assert.equal(selectedZoom({x:0,y:0},{x:300,y:100},periods,invertX,()=>NaN),null);
 assert.equal(selectedZoom({x:NaN,y:0},{x:300,y:100},periods,invertX,invertY),null);
});

test('a dual-axis rectangle selects the same visual area in each series own units',()=>{
 for(const [start,end] of [[{x:100,y:20},{x:300,y:100}],[{x:300,y:100},{x:100,y:20}],[{x:100,y:100},{x:300,y:20}],[{x:300,y:20},{x:100,y:100}]]){
  const bounds=selectedZoom(start,end,periods,invertX,y=>-1-y*.02,y=>140-y*.1);
  assert.equal(bounds.from,'2025 Q2');assert.equal(bounds.to,'2025 Q4');
  assert.deepEqual(bounds.y,[-3,-1.4]);assert.deepEqual(bounds.rightY,[130,138]);
 }
});

test('a dual-axis horizontal selection fits both value axes automatically',()=>{
 assert.deepEqual(selectedZoom({x:0,y:20},{x:300,y:24},periods,invertX,invertY,y=>140-y*.1),{from:'2025 Q1',to:'2025 Q4',y:null,rightY:null});
});

test('repeated dual-axis zoom uses each currently visible value scale',()=>{
 const visible=periods.slice(1);
 assert.deepEqual(selectedZoom({x:0,y:0},{x:100,y:50},visible,x=>visible[Math.round(x/100)],y=>-1.4-y*.02,y=>138-y*.04),{from:'2025 Q2',to:'2025 Q3',y:[-2.4,-1.4],rightY:[136,138]});
});

test('an invalid secondary scale cannot corrupt the combined chart zoom',()=>{
 assert.equal(selectedZoom({x:0,y:0},{x:300,y:100},periods,invertX,invertY,()=>NaN),null);
});
