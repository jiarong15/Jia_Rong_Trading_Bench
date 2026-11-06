// Exercise the actual React component and refresh scheduler without browser
// automation. Test quotes stay in this process; nothing is sent to the app.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const test=require('node:test');
const ts=require('typescript');
const jsx=require('react/jsx-runtime');

function compile(filename,dependencies,globals={}){
 const source=fs.readFileSync(require.resolve(filename),'utf8');
 const output=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 const exports={};
 vm.runInNewContext(output,{exports,require:name=>{if(!(name in dependencies))throw Error('Unexpected dependency: '+name);return dependencies[name];},...globals});
 return exports;
}

function fixture({report=false}={}){
 const window=new EventTarget(),document=new EventTarget();document.hidden=false;
 let now=1000000,tick,cleared=false,cursor=0,effects=[],tree;
 const slots=[],cleanups=[],requests=[];
 window.setInterval=(fn,ms)=>{assert.equal(ms,1000);tick=fn;return 1;};
 window.clearInterval=()=>{cleared=true;};
 window.setTimeout=()=>2;window.clearTimeout=()=>{};
 const globals={window,document,Date:{now:()=>now},AbortController};
 const scheduler=compile('../src/bondsData.ts',{},globals);
 const hooks={
  useState(initial){const index=cursor++;if(!(index in slots))slots[index]=initial;return[slots[index],value=>{slots[index]=typeof value==='function'?value(slots[index]):value;}];},
  useRef(initial){const index=cursor++;if(!(index in slots))slots[index]={current:initial};return slots[index];},
  useEffect(effect,deps){const index=cursor++;const before=slots[index];if(!before||deps.some((value,i)=>value!==before[i])){slots[index]=deps;effects.push(effect);}},
 };
 const row={currency:'EUR',pair:'EUR/USD',usd_carry_pct:1,pair_carry_pct:-.99,risk_correlation:-.3,weekly_observations:52,
  implied_volatility_pct:null,spot:1.1,forward_12m:1.111,spot_date:null,forward_date:null,quote_time:'2026-10-30T12:00:00Z',iv_time:null,iv_tenor:null,source:'Test public forward source',stale:false};
 let latest={basis:'observed_forwards_and_implied_volatility',status:'partial',feed_configured:false,forward_feed_configured:true,
  iv_tenor:'12M',weeks:52,poll_interval_seconds:60,source_checks:{},errors:[],rows:[row],sources:[],methodology:'Test observations'};
 if(report){
  latest.volatility_status={provider:'Saxo public OTC report',enabled:true,status:'daily_report',report_checked_at:'2026-10-30T12:31:00Z',report_published_at:'2026-10-30T12:30:00Z',poll_interval_seconds:60,errors:[]};
  Object.assign(latest.rows[0],{implied_volatility_pct:6.85,iv_tenor:'12M',iv_time:'2026-10-30T12:30:00Z',iv_source:'Saxo public OTC report',iv_delivery:'daily_report'});
 }
 let cached=structuredClone(latest),serverAttempt=-Infinity,fail=false;
 async function api(path){
  requests.push(path);now+=50; // Account for cache fetch and network latency.
  if(path.includes('saved_only=true'))return structuredClone(cached);
  if(fail)throw Error('Test source interruption');
  if(now-serverAttempt>=60000){serverAttempt=now;cached=structuredClone(latest);}
  now+=50;
  return structuredClone(cached);
 }
 const chartNames=['CartesianGrid','LabelList','ReferenceLine','ResponsiveContainer','Scatter','ScatterChart','Tooltip','XAxis','YAxis'];
 const components={Badge:'Badge',Panel:'Panel',number:value=>String(value),signed:value=>String(value),stamp:value=>value};
 const module=compile('../src/FxCarryRisk.tsx',{'react':hooks,'react/jsx-runtime':jsx,
  'recharts':Object.fromEntries(chartNames.map(name=>[name,name])),'lucide-react':{RefreshCw:'RefreshCw'},
  './api':{api},'./components':components,'./bondsData':scheduler,'./fxCarryRisk.css':{}},globals);
 function render(){cursor=0;tree=module.default({refresh:0});const pending=effects;effects=[];for(const effect of pending){const stop=effect();if(stop)cleanups.push(stop);}return tree;}
 async function settle(){for(let i=0;i<8;i++)await Promise.resolve();render();}
 function plots(){
  const result=[];
  function walk(node){
   if(!node||typeof node!=='object')return;
   if(Array.isArray(node)){node.forEach(walk);return;}
   if(node.type==='Scatter')result.push(node.props.data);
   if(typeof node.type==='function'&&node.type.name==='CarryScatter')walk(node.type(node.props));
   else walk(node.props?.children);
  }
  walk(tree);return result;
 }
 render();
 return{window,document,settle,plots,requests,advance:ms=>{now+=ms;},tick:()=>{if(!cleared)tick();},
  change:value=>{latest=structuredClone(latest);latest.rows[0].usd_carry_pct=value;latest.rows[0].quote_time='2026-10-30T12:01:00Z';},
  changeIv:(value,at='2026-10-31T12:30:00Z')=>{latest=structuredClone(latest);Object.assign(latest.rows[0],{implied_volatility_pct:value,iv_time:at});latest.volatility_status.report_published_at=at;},
  fail:value=>{fail=value;},stop:()=>{cleanups.forEach(stop=>stop());},cleared:()=>cleared};
}

test('a new source value moves the left scatter point on the next automatic poll despite request latency',async()=>{
 const f=fixture();await f.settle();assert.equal(f.plots()[0][0].y,1);
 f.change(2.5);const before=f.requests.length;
 f.advance(59999);f.tick();await f.settle();assert.equal(f.requests.length,before);
 f.advance(1);f.tick();await f.settle();
 assert.equal(f.plots()[0][0].y,2.5,'The next automatic poll must replace the old carry point.');
 assert.equal(f.plots()[0][0].x,-.3);assert.equal(f.plots().length,1,'No volatility points are fabricated.');
 f.stop();
});

test('hidden tabs catch up and reconnect updates the point without a manual refresh',async()=>{
 const f=fixture();await f.settle();f.change(3);f.document.hidden=true;const before=f.requests.length;
 f.advance(120000);f.tick();await f.settle();assert.equal(f.requests.length,before);
 f.document.hidden=false;f.document.dispatchEvent(new Event('visibilitychange'));await f.settle();assert.equal(f.plots()[0][0].y,3);
 f.change(4);f.advance(60000);f.window.dispatchEvent(new Event('online'));await f.settle();assert.equal(f.plots()[0][0].y,4);
 f.stop();assert.ok(f.cleared());
});

test('an interrupted check keeps the previous point and the next automatic poll recovers',async()=>{
 const f=fixture();await f.settle();f.change(5);f.fail(true);f.advance(60000);f.tick();await f.settle();
 assert.equal(f.plots()[0][0].y,1);
 f.fail(false);f.advance(60000);f.tick();await f.settle();assert.equal(f.plots()[0][0].y,5);
 const before=f.requests.length;f.stop();f.advance(60000);f.tick();f.window.dispatchEvent(new Event('online'));await f.settle();
 assert.equal(f.requests.length,before);
});

test('a newly published Saxo report moves the right chart automatically on the next minute check',async()=>{
 const f=fixture({report:true});await f.settle();assert.equal(f.plots()[1][0].x,6.85);
 f.changeIv(8.25);const before=f.requests.length;
 f.advance(59999);f.tick();await f.settle();assert.equal(f.requests.length,before);
 f.advance(1);f.tick();await f.settle();
 const point=f.plots()[1][0];assert.equal(point.x,8.25);assert.equal(point.iv_time,'2026-10-31T12:30:00Z');
 assert.equal(point.iv_source,'Saxo public OTC report');assert.equal(point.iv_delivery,'daily_report');
 assert.equal(f.plots()[0][0].y,1,'Report updates must not change the carry calculation.');f.stop();
});

test('report failures preserve the old publication date and the next automatic poll recovers',async()=>{
 const f=fixture({report:true});await f.settle();f.changeIv(7.5);f.fail(true);f.advance(60000);f.tick();await f.settle();
 assert.equal(f.plots()[1][0].x,6.85);assert.equal(f.plots()[1][0].iv_time,'2026-10-30T12:30:00Z');
 f.fail(false);f.advance(60000);f.tick();await f.settle();
 assert.equal(f.plots()[1][0].x,7.5);assert.equal(f.plots()[1][0].iv_time,'2026-10-31T12:30:00Z');f.stop();
});

test('returning to a hidden dashboard catches up to the latest report without a button click',async()=>{
 const f=fixture({report:true});await f.settle();f.document.hidden=true;f.changeIv(9.5);f.advance(120000);
 const before=f.requests.length;f.tick();await f.settle();assert.equal(f.requests.length,before);
 f.document.hidden=false;f.document.dispatchEvent(new Event('visibilitychange'));await f.settle();
 assert.equal(f.plots()[1][0].x,9.5);assert.equal(f.plots()[1][0].iv_time,'2026-10-31T12:30:00Z');f.stop();
});
