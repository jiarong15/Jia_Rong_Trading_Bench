const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test'),ts=require('typescript');
function fixture(){
 const window=new EventTarget(),document=new EventTarget();document.hidden=false;
 let now=Date.parse('2026-10-30T00:00:00Z'),calls=0,busy=false,failed=false,tick,cleared=false,last=now;
 class Clock extends Date{constructor(...args){super(...(args.length?args:[now]));}static now(){return now;}}
 window.setInterval=(callback,ms)=>{assert.equal(ms,1000);tick=callback;return 12;};window.clearInterval=()=>cleared=true;
 const exports={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(require.resolve('../src/bondsData.ts'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{exports,Date:Clock,window,document});
 const stop=exports.watchBondRefresh(()=>{calls++;last=now;busy=true;},()=>!busy,()=>last,()=>failed);
 return {exports,window,document,stop,calls:()=>calls,advance:ms=>now+=ms,at:value=>{now=Date.parse(value);last=now;},finish:()=>busy=false,fail:()=>{busy=false;failed=true;last=now;},tick:()=>{if(!cleared)tick();}};
}
test('all charts use calendar cutoffs anchored to published observations, including leap years',()=>{
 const f=fixture(),filter=f.exports.filterBondRows;
 const rows=['2026-09-27','2026-09-28','2026-10-28','2026-10-29'].map(date=>({date}));
 assert.deepEqual(Array.from(filter(rows,'1M','2026-10-28'),row=>row.date),['2026-09-28','2026-10-28']);
 const leap=['2027-02-27','2027-02-28','2028-02-29'].map(date=>({date}));
 assert.equal(filter(leap,'1Y','2028-02-29')[0].date,'2027-02-28');
 assert.equal(filter(rows,'All','2026-10-28'),rows);f.stop();
});
test('source polling pauses while hidden, catches up, and does not overlap requests',()=>{
 const f=fixture();f.advance(300000);f.document.hidden=true;f.tick();assert.equal(f.calls(),0);
 f.document.hidden=false;f.document.dispatchEvent(new Event('visibilitychange'));assert.equal(f.calls(),1);
 f.advance(300000);f.tick();f.window.dispatchEvent(new Event('online'));assert.equal(f.calls(),1);
 f.finish();f.tick();assert.equal(f.calls(),2);f.stop();
});
test('reconnection retries immediately and cleanup removes every refresh callback',()=>{
 const f=fixture();f.window.dispatchEvent(new Event('online'));assert.equal(f.calls(),1);f.finish();f.stop();
 f.advance(600000);f.tick();f.window.dispatchEvent(new Event('online'));f.window.dispatchEvent(new Event('focus'));f.document.dispatchEvent(new Event('visibilitychange'));assert.equal(f.calls(),1);
});

test('one-minute release checks cover summer and winter; other hours and weekends use five minutes',()=>{
 const f=fixture(),interval=f.exports.bondPollInterval;
 for(const stamp of ['2026-07-06T20:15:00Z','2026-12-07T21:15:00Z','2026-10-30T22:59:59Z'])assert.equal(interval(Date.parse(stamp)),60000);
 for(const stamp of ['2026-10-30T19:59:59Z','2026-10-30T23:00:00Z','2026-10-31T21:15:00Z'])assert.equal(interval(Date.parse(stamp)),300000);
 f.at('2026-10-30T19:59:00Z');f.advance(59000);f.tick();assert.equal(f.calls(),0);
 f.advance(1000);f.tick();assert.equal(f.calls(),1);f.finish();
 f.advance(59000);f.tick();assert.equal(f.calls(),1);f.advance(1000);f.tick();assert.equal(f.calls(),2);f.stop();
});

test('failed requests retry thirty seconds after completion without waiting five minutes',()=>{
 const f=fixture();f.window.dispatchEvent(new Event('online'));f.advance(24000);f.fail();
 f.advance(29000);f.tick();assert.equal(f.calls(),1);f.advance(1000);f.tick();assert.equal(f.calls(),2);f.stop();
});

test('a reconnect while hidden is remembered for the next visible or idle moment',()=>{
 const f=fixture();f.document.hidden=true;f.window.dispatchEvent(new Event('online'));assert.equal(f.calls(),0);
 f.document.hidden=false;f.document.dispatchEvent(new Event('visibilitychange'));assert.equal(f.calls(),1);
 f.window.dispatchEvent(new Event('online'));assert.equal(f.calls(),1);f.finish();f.tick();assert.equal(f.calls(),2);f.stop();
});

test('actual history charts join holidays, preserve missing legs and leave source rows unchanged',()=>{
 const f=fixture(),data=f.exports;f.stop();
 const chartNames=['CartesianGrid','Line','LineChart','ReferenceLine','ResponsiveContainer','Tooltip','XAxis','YAxis'];
 const dependencies={'react':{},'react/jsx-runtime':require('react/jsx-runtime'),
  'recharts':Object.fromEntries(chartNames.map(name=>[name,name])),'lucide-react':{},'./api':{},'./components':{},
  './bondsData':data,'./bonds.css':{},'./bop.css':{}};
 const exports={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(require.resolve('../src/BondsPage.tsx'),'utf8'),
  {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText,
  {exports,require:name=>{assert.ok(name in dependencies);return dependencies[name];}});
 const fields=[...data.TENORS.map(t=>t.key),...data.CURVE_METRICS.map(t=>t.key)];
 const published=date=>({date,...Object.fromEntries(data.TENORS.map(t=>[t.key,4])),...Object.fromEntries(data.CURVE_METRICS.map(t=>[t.key,0]))});
 const holiday={date:'2025-12-25',...Object.fromEntries(fields.map(key=>[key,null]))};
 const missingLeg={...published('2025-12-29'),y2:null,'2s10s':null,'2s5s10s':null,'2s10s30s':null};
 const rows=[published('2025-12-24'),holiday,published('2025-12-26'),missingLeg,published('2025-12-30')];
 const before=JSON.stringify(rows);
 function find(node,type){
  if(!node||typeof node!=='object')return null;
  if(Array.isArray(node))return node.map(child=>find(child,type)).find(Boolean);
  return node.type===type?node:find(node.props?.children,type);
 }
 for(const field of fields){
  const tree=exports.BondHistory({rows,field,unit:field.startsWith('y')||field==='m3'?'%':'bp'});
  const chart=find(tree,'LineChart'),line=find(tree,'Line');
  assert.deepEqual(Array.from(chart.props.data,row=>row.date),['2025-12-24','2025-12-26','2025-12-29','2025-12-30']);
  assert.equal(chart.props.data[2],missingLeg,'A missing leg on a published date must remain a chart gap.');
  assert.equal(line.props.connectNulls,false);
 }
 const spread=find(exports.BondHistory({rows,field:'2s10s',unit:'bp'}),'LineChart');
 assert.equal(spread.props.data[0]['2s10s'],0,'Zero spreads are valid observations.');
 assert.equal(spread.props.data[2]['2s10s'],null,'Do not fabricate a spread from a missing leg.');
 const auctions=find(exports.BondHistory({rows,field:'2s10s',unit:'bp',observationsOnly:true}),'LineChart');
 assert.equal(auctions.props.data.length,3,'Auction-date charts must retain their existing published-only behavior.');
 assert.equal(JSON.stringify(rows),before,'The API/export data and all missing values must remain intact.');
 const empty=exports.BondHistory({rows:[holiday],field:'2s10s',unit:'bp'});
 assert.equal(find(empty,'LineChart'),null,'All-missing histories must remain unavailable.');
});
