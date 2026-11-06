const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),test=require('node:test'),ts=require('typescript'),jsx=require('react/jsx-runtime'),{renderToString}=require('react-dom/server');

function compile(file,dependencies={},globals={}){
 const exports={};const code=ts.transpileModule(fs.readFileSync(require.resolve(file),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 vm.runInNewContext(code,{exports,require:name=>{assert.ok(name in dependencies,`Unexpected dependency ${name}`);return dependencies[name];},...globals});return exports;
}
const math=compile('../src/fxOptionsData.ts');
function data(){return {pair:'EUR/USD',status:'ready',basis:'reported_trades',source:'DTCC public FX trades · estimated Greeks',source_url:'https://pddata.dtcc.com/ppd/',checked_at:'2026-10-31T12:00:00Z',as_of:'2026-10-31T12:00:00Z',last_trade_at:'2026-10-30T20:00:00Z',spot:1.1,spot_at:null,spot_date:'2026-10-30',window_start:'2026-10-01T12:00:00Z',window_end:'2026-10-31T12:00:00Z',rows:['97.5–100%','100–102.5%'],columns:['≤1 month','1–2 months'],cells:[
 {row:0,column:0,side:'call',count:2,gamma:10,delta:100,vega:30,notional:2000},
 {row:0,column:0,side:'put',count:3,gamma:20,delta:60,vega:20,notional:1000},
 {row:1,column:1,side:'put',count:1,gamma:0,delta:20,vega:5,notional:500}],
 metrics:Object.fromEntries(Object.keys(math.METRICS).map(key=>[key,{available:true,definition:`Estimated ${key} definition`,coverage_note:'6 of 6 loaded trades have Greek estimates.'}])),notices:['Trade activity, not open interest.','ESTIMATED GREEKS: undiscounted Black forward sensitivities.'],error:null,poll_interval_seconds:60};}

test('call/put and Greek switches recompute actual heatmap cells, totals and ratios',()=>{
 const d=data(),copy=JSON.stringify(d);
 assert.equal(math.heatmapValues(d,'gamma',true,true).total,30);
 assert.equal(math.heatmapValues(d,'gamma',true,false).total,10);
 assert.equal(math.heatmapValues(d,'gamma',false,true).total,20);
 assert.equal(math.heatmapValues(d,'delta',true,true).total,180);
 assert.equal(math.heatmapValues(d,'vega',true,true).total,55);
 assert.equal(math.heatmapValues(d,'notional',true,true).total,3500);
 assert.equal(math.heatmapValues(d,'gamma',true,true).putCallRatio,2);
 assert.equal(math.heatmapValues(d,'gamma',true,true).count,6);
 assert.equal(math.heatmapValues(d,'gamma',true,true).grid[1][1],0);
 assert.equal(JSON.stringify(d),copy);
});

test('missing Greek contributions remain unavailable, do not hide trades, and do not contaminate calls-only views',()=>{
 const d=data();d.cells[1].gamma=null;
 const both=math.heatmapValues(d,'gamma',true,true),calls=math.heatmapValues(d,'gamma',true,false);
 assert.equal(both.grid[0][0],null);assert.equal(both.total,null);assert.equal(both.putCallRatio,null);assert.equal(both.count,6);
 assert.equal(calls.grid[0][0],10);assert.equal(calls.total,10);assert.equal(calls.count,2);
 d.metrics.gamma.available=false;const missing=math.heatmapValues(d,'gamma',true,true);
 assert.equal(missing.total,null);assert.ok(missing.grid.flat().every(v=>v===null));assert.equal(missing.count,6);
 assert.equal(math.heatColor(null,10),'transparent');assert.equal(math.compactUsd(null),'—');assert.equal(math.compactUsd(0),'$0');
});

test('wrong-pair, duplicate, nonfinite and malformed buckets are rejected',()=>{
 assert.throws(()=>math.validateHeatmap(data(),'USD/JPY'));
 const duplicate=data();duplicate.cells.push({...duplicate.cells[0]});assert.throws(()=>math.validateHeatmap(duplicate,'EUR/USD'));
 const invalid=data();invalid.cells[0].gamma=NaN;assert.throws(()=>math.validateHeatmap(invalid,'EUR/USD'));
 const outside=data();outside.cells[0].row=99;assert.throws(()=>math.validateHeatmap(outside,'EUR/USD'));
 const unavailable=data();delete unavailable.metrics;assert.throws(()=>math.validateHeatmap(unavailable,'EUR/USD'));
});

function harness(){
 const window=new EventTarget(),document=new EventTarget();document.hidden=false;
 let now=1000000,cursor=0,effects=[],tick,cleared=false,tree,fail=false,latest=data();const slots=[],cleanups=[],requests=[];
 window.setInterval=(fn,ms)=>{assert.equal(ms,1000);tick=fn;return 1;};window.clearInterval=()=>{cleared=true;};window.setTimeout=()=>2;window.clearTimeout=()=>{};
 const globals={window,document,Date:class extends Date{static now(){return now;}},AbortController};
 const scheduler=compile('../src/bondsData.ts',{},globals);
 const hooks={useState(initial){const i=cursor++;if(!(i in slots))slots[i]=initial;return[slots[i],value=>slots[i]=typeof value==='function'?value(slots[i]):value];},
  useRef(initial){const i=cursor++;if(!(i in slots))slots[i]={current:initial};return slots[i];},
  useEffect(effect,deps){const i=cursor++,previous=slots[i];if(!previous||deps.some((v,j)=>v!==previous[j])){slots[i]=deps;effects.push(effect);}},
  useCallback(fn,deps){const i=cursor++,previous=slots[i];if(!previous||deps.some((v,j)=>v!==previous.deps[j]))slots[i]={fn,deps};return slots[i].fn;},useMemo(fn){return fn();}};
 const components={Badge:({children})=>jsx.jsx('span',{children}),Panel:({children,title,subtitle})=>jsx.jsxs('section',{children:[jsx.jsx('h3',{children:title}),subtitle,children]}),Metric:({label,value,foot})=>jsx.jsxs('div',{children:[label,value,foot]}),number:v=>String(v),stamp:v=>v};
 const module=compile('../src/FxOptionsHeatmap.tsx',{'react':hooks,'react/jsx-runtime':jsx,'lucide-react':{Download:'Download',RefreshCw:'RefreshCw'},'./api':{api:async path=>{requests.push(path);if(fail)throw Error('Source interruption');return structuredClone(latest);},download(){}},'./components':components,'./bondsData':scheduler,'./fxOptionsData':math,'./fxOptions.css':{}},globals);
 function render(){cursor=0;tree=module.default({refresh:0});const pending=effects;effects=[];for(const effect of pending){const cleanup=effect();if(cleanup)cleanups.push(cleanup);}}
 async function settle(){for(let i=0;i<12;i++)await Promise.resolve();render();}
 function elements(){const nodes=[];function walk(node){if(!node||typeof node!=='object')return;if(Array.isArray(node)){node.forEach(walk);return;}nodes.push(node);walk(node.props?.children);}walk(tree);return nodes;}
 const grid=()=>elements().find(node=>node.type===module.FxOptionsGrid)?.props;
 render();return {module,window,document,requests,settle,render,elements,grid,advance:ms=>now+=ms,tick:()=>{if(!cleared)tick();},change:value=>{latest=structuredClone(latest);latest.cells[0].gamma=value;latest.last_trade_at='2026-10-31T11:00:00Z';},fail:value=>fail=value,stop:()=>cleanups.forEach(fn=>fn())};
}

test('new source trades and estimated notionals update automatically after 60 seconds',async()=>{
 const f=harness();await f.settle();assert.equal(f.grid().data.cells[0].gamma,10);
 f.change(40);const count=f.requests.length;f.advance(59999);f.tick();await f.settle();assert.equal(f.requests.length,count);
 f.advance(1);f.tick();await f.settle();assert.equal(f.grid().data.cells[0].gamma,40);assert.equal(f.grid().data.last_trade_at,'2026-10-31T11:00:00Z');f.stop();
});

test('actual controls change selected measure and option sides; rendered grid is labelled and scrollable',async()=>{
 const f=harness();await f.settle();
 const delta=f.elements().find(node=>node.type==='button'&&node.props.children==='Delta notional');delta.props.onClick();f.render();assert.equal(f.grid().metric,'delta');
 const boxes=f.elements().filter(node=>node.type==='input'&&node.props.type==='checkbox');boxes[1].props.onChange({target:{checked:false}});f.render();assert.equal(f.grid().puts,false);assert.equal(f.grid().calls,true);
 const html=renderToString(jsx.jsx(f.module.FxOptionsGrid,f.grid()));assert.ok(html.includes('role="region"'));assert.ok(html.includes('scroll horizontally'));assert.ok(html.includes('Strike / spot'));assert.ok(html.includes('1–2 months'));assert.ok(html.includes('30 Oct')||html.includes('2026-10-30'));assert.ok(html.includes('title='));assert.ok(!html.includes('NaN'));f.stop();
});

test('hidden tabs catch up, source failures preserve dated values, reconnect recovers, and cleanup stops polling',async()=>{
 const f=harness();await f.settle();f.document.hidden=true;f.change(30);const before=f.requests.length;f.advance(120000);f.tick();await f.settle();assert.equal(f.requests.length,before);
 f.document.hidden=false;f.document.dispatchEvent(new Event('visibilitychange'));await f.settle();assert.equal(f.grid().data.cells[0].gamma,30);
 f.fail(true);f.advance(60000);f.tick();await f.settle();assert.equal(f.grid().data.cells[0].gamma,30);
 f.fail(false);f.change(60);f.window.dispatchEvent(new Event('online'));await f.settle();assert.equal(f.grid().data.cells[0].gamma,60);
 f.stop();const calls=f.requests.length;f.advance(120000);f.tick();f.window.dispatchEvent(new Event('online'));await f.settle();assert.equal(f.requests.length,calls);
});
