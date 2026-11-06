// Exercise the actual Rates page and its shared refresh scheduler without a
// browser debugger. All source updates in these tests are in-memory fixtures.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test'),ts=require('typescript'),jsx=require('react/jsx-runtime');

function compile(file,dependencies,globals={}){
 const exports={};const output=ts.transpileModule(fs.readFileSync(require.resolve(file),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 vm.runInNewContext(output,{exports,require:name=>{assert.ok(name in dependencies,`Unexpected dependency ${name}`);return dependencies[name];},...globals});return exports;
}

function fixture(){
 const window=new EventTarget(),document=new EventTarget();document.hidden=false;
 let now=1000000,tick,cleared=false,cursor=0,effects=[],tree,fail=false;
 const slots=[],cleanups=[],requests=[];
 window.setInterval=(fn,ms)=>{assert.equal(ms,1000);tick=fn;return 1;};window.clearInterval=()=>cleared=true;
 window.setTimeout=()=>2;window.clearTimeout=()=>{};
 const globals={window,document,Date:class extends Date{static now(){return now;}},AbortController};
 const scheduler=compile('../src/bondsData.ts',{},globals);
 const hooks={
  useState(initial){const i=cursor++;if(!(i in slots))slots[i]=initial;return[slots[i],value=>slots[i]=typeof value==='function'?value(slots[i]):value];},
  useRef(initial){const i=cursor++;if(!(i in slots))slots[i]={current:initial};return slots[i];},
  useEffect(effect,deps){const i=cursor++,previous=slots[i];if(!previous||deps.some((v,j)=>v!==previous[j])){slots[i]=deps;effects.push(effect);}},
  useMemo(fn){return fn();},useCallback(fn,deps){const i=cursor++,previous=slots[i];if(!previous||deps.some((v,j)=>v!==previous.deps[j]))slots[i]={fn,deps};return slots[i].fn;},
 };
 const base={key:'test',label:'Published measure',series_id:'TEST',transformation:'level',unit:'%',axis:'left',style:'line',frequency:'Monthly',source:'Test official source',seasonal_adjustment:'Seasonally adjusted',source_url:null,
  observations:[{date:'2026-08-01',value:3},{date:'2026-09-01',value:3.2}],last_value:3.2,last_observation_date:'2026-09-01',last_checked_at:null,status:'ready',error:null};
 let latest={country:'USA',status:'partial',poll_interval_seconds:60,history_start:'1990-01-01',last_attempt_at:null,cache_warning:null,notice:'Published data',charts:['cpi','pce','employment','wages','inflation_expectations','jobs','housing'].map(key=>({key,title:key,subtitle:'Test published data',left_unit:'% YoY',notes:'Test definitions',series:[{...structuredClone(base),key}]}))};
 let cached=structuredClone(latest),serverAttempt=-Infinity;
 async function api(path){requests.push(path);now+=50;if(path.includes('saved_only=true'))return structuredClone(cached);if(fail)throw Error('Source interruption');if(now-serverAttempt>=60000){cached=structuredClone(latest);serverAttempt=now;}now+=50;return structuredClone(cached);}
 const chartNames=['Bar','CartesianGrid','ComposedChart','Line','ReferenceLine','ResponsiveContainer','Tooltip','XAxis','YAxis'];
 const components={Badge:'Badge',PageTitle:'PageTitle',Panel:'Panel',number:value=>String(value),stamp:value=>value};
 const module=compile('../src/RatesPage.tsx',{'react':hooks,'react/jsx-runtime':jsx,'recharts':Object.fromEntries(chartNames.map(n=>[n,n])),
  'lucide-react':{Download:'Download',RefreshCw:'RefreshCw'},'./api':{api,download(){}},'./components':components,'./bondsData':scheduler,'./rates.css':{}},globals);
 function render(){cursor=0;tree=module.default();const pending=effects;effects=[];for(const effect of pending){const cleanup=effect();if(cleanup)cleanups.push(cleanup);}}
 async function settle(){for(let i=0;i<10;i++)await Promise.resolve();render();}
 function panels(){const result=[];function walk(node){if(!node||typeof node!=='object')return;if(Array.isArray(node)){node.forEach(walk);return;}if(node.type===module.RatesChartPanel)result.push(node.props);walk(node.props?.children);}walk(tree);return result;}
 render();return {module,window,document,requests,settle,panels,advance:ms=>now+=ms,tick:()=>{if(!cleared)tick();},fail:v=>fail=v,
  change:value=>{latest=structuredClone(latest);const series=latest.charts[0].series[0];series.last_value=value;series.last_observation_date='2026-10-01';series.observations.push({date:'2026-10-01',value});},
  changeSwap:(value,at)=>{latest=structuredClone(latest);const series=latest.charts[4].series[0];Object.assign(series,{key:'inflation_swap',label:'5Y zero-coupon inflation swap · trade median',frequency:'Daily',delivery:'reported_transactions',source:'DTCC public dissemination',last_value:value,last_observation_date:'2026-10-30',last_observation_at:at,last_checked_at:'2026-10-31T12:00:00Z',trade_count:7,history_start:'2026-10-01',pending_live_files:0,pending_history_files:12});series.observations=[{date:'2026-10-29',value:2.5},{date:'2026-10-30',value}];},
  stop:()=>cleanups.forEach(fn=>fn())};
}

test('seven panels load and a new source release updates the actual Rates page on the next automatic poll',async()=>{
 const f=fixture();await f.settle();assert.equal(f.panels().length,7);assert.equal(f.panels()[0].chart.series[0].last_value,3.2);
 f.change(3.6);const before=f.requests.length;f.advance(59999);f.tick();await f.settle();assert.equal(f.requests.length,before);
 f.advance(1);f.tick();await f.settle();assert.equal(f.panels()[0].chart.series[0].last_value,3.6);assert.equal(f.panels()[0].chart.series[0].last_observation_date,'2026-10-01');f.stop();
});

test('hidden pages catch up, interrupted checks preserve data, and reconnect recovers without manual refresh',async()=>{
 const f=fixture();await f.settle();f.document.hidden=true;f.change(4);f.advance(120000);const before=f.requests.length;f.tick();await f.settle();assert.equal(f.requests.length,before);
 f.document.hidden=false;f.document.dispatchEvent(new Event('visibilitychange'));await f.settle();assert.equal(f.panels()[0].chart.series[0].last_value,4);
 f.fail(true);f.change(5);f.advance(60000);f.tick();await f.settle();assert.equal(f.panels()[0].chart.series[0].last_value,4);
 f.fail(false);f.window.dispatchEvent(new Event('online'));await f.settle();assert.equal(f.panels()[0].chart.series[0].last_value,5);
 f.stop();const count=f.requests.length;f.advance(120000);f.tick();f.window.dispatchEvent(new Event('online'));await f.settle();assert.equal(f.requests.length,count);
});

test('a new DTCC trade median and actual trade timestamp redraw automatically with clear delivery labels',async()=>{
 const f=fixture();await f.settle();f.changeSwap(2.6,'2026-10-30T18:00:00Z');f.advance(60000);f.tick();await f.settle();
 let chart=f.panels()[4].chart;assert.equal(chart.series[0].last_value,2.6);
 f.changeSwap(2.7,'2026-10-30T19:00:00Z');f.advance(60000);f.tick();await f.settle();chart=f.panels()[4].chart;
 assert.equal(chart.series[0].last_value,2.7);assert.equal(chart.series[0].last_observation_at,'2026-10-30T19:00:00Z');
 const tree=f.module.RatesChartPanel({chart,period:'5Y'});const strings=[];
 function walk(node){if(typeof node==='string'||typeof node==='number'){strings.push(String(node));return;}if(!node||typeof node!=='object')return;if(Array.isArray(node)){node.forEach(walk);return;}walk(node.props?.children);}walk(tree);
 const text=strings.join(' ');assert.ok(text.includes('reported trades'));assert.ok(text.includes('7 eligible trades'));assert.ok(text.includes('reporting delays apply'));assert.ok(text.includes('19:00:00Z'));assert.ok(text.includes('12 daily files remaining'));f.stop();
});

test('mixed-frequency lines preserve exact native dates and do not invent daily monthly observations',()=>{
 const f=fixture(),module=f.module;f.stop();
 const chart={series:[{key:'expected',frequency:'Monthly',last_observation_date:'2026-09-01',observations:[{date:'2026-09-01',value:0},{date:'2026-10-01',value:null}]},
  {key:'breakeven',frequency:'Daily',last_observation_date:'2026-10-30',observations:[{date:'2026-10-28',value:null},{date:'2026-10-29',value:2.4},{date:'2026-10-30',value:2.5}]}]};
 const copy=JSON.stringify(chart);const plotted=module.ratesChartData(chart,'1Y');
 assert.equal(plotted.series[0].points.length,2);assert.equal(plotted.series[0].points[0].expected,0);assert.equal(plotted.series[0].points[1].expected,null);
 assert.equal(plotted.rows.length,4);assert.equal(plotted.rows.find(r=>r.date==='2026-10-30').expected,undefined);
 assert.equal(JSON.stringify(chart),copy);assert.equal(module.periodStart('1Y','2028-02-29'),'2027-02-28');
});

test('real chart panel keeps separate labelled axes and an unavailable swap has no plotted values',async()=>{
 const f=fixture();await f.settle();
 const chart=f.panels()[0].chart;chart.right_unit='Jobs, thousands';chart.series.push({...structuredClone(chart.series[0]),key:'jobs',axis:'right',style:'bar',unit:'thousand jobs'});
 const tree=f.module.RatesChartPanel({chart,period:'5Y'});const elements=[];
 function walk(node){if(!node||typeof node!=='object')return;if(Array.isArray(node)){node.forEach(walk);return;}elements.push(node);walk(node.props?.children);}walk(tree);
 assert.equal(elements.filter(n=>n.type==='YAxis').length,2);
 assert.equal(elements.find(n=>n.type==='Bar').props.yAxisId,'right');assert.equal(elements.find(n=>n.type==='Line').props.connectNulls,false);
 const swap={...chart.series[0],key:'swap',observations:[],last_value:null,last_observation_date:null,status:'awaiting_source',error:'Source needed'};
 const data=f.module.ratesChartData({...chart,series:[swap]},'5Y');assert.equal(data.rows.length,0);f.stop();
});
