const fs=require('node:fs'),assert=require('node:assert/strict'),ts=require('typescript'),React=require('react'),{renderToString}=require('react-dom/server');
require.extensions['.css']=()=>{};
for(const extension of ['.tsx','.ts'])require.extensions[extension]=(module,filename)=>module._compile(ts.transpileModule(fs.readFileSync(filename,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText,filename);
async function main(){
 const {FxOptionsGrid}=require('../src/FxOptionsHeatmap.tsx');
 const {validateHeatmap,heatmapValues}=require('../src/fxOptionsData.ts');
 const pairs=['EUR/USD','USD/JPY','GBP/USD','AUD/USD','USD/CAD','USD/CHF','NZD/USD','USD/SEK','USD/NOK','USD/CNY'];
 const fragments=[];let rendered=0;
 for(const pair of pairs){
  const response=await fetch(`http://127.0.0.1:8000/api/fx/options/heatmap?pair=${encodeURIComponent(pair)}&saved_only=true`);
  assert.equal(response.status,200);assert.equal(response.headers.get('cache-control'),'no-store');
  const d=validateHeatmap(await response.json(),pair);
  assert.equal(d.basis,'reported_trades');assert.ok(d.checked_at);assert.ok(d.included_trades>0);assert.ok(d.cells.length>0);
  assert.ok(d.notices.some(n=>n.includes('ESTIMATED GREEKS')));assert.ok(d.notices.some(n=>n.includes('not open interest')));
  if(['NZD/USD','USD/SEK','USD/NOK'].includes(pair))assert.equal(d.metrics.gamma.available,false);
  else assert.ok(d.modelled_trades>0);
  if(pair==='USD/CNY'){
   assert.equal(d.market_source,'CFETS / ChinaMoney');assert.ok(d.market_checked_at);assert.ok(d.pricing_checked_at);
   assert.ok(d.volatility_curve.some(node=>node.tenor==='3Y'));assert.ok(d.notices.some(note=>note.includes('not a continuous tick feed')));
  }
  for(const metric of ['gamma','delta','vega','notional'])for(const [calls,puts] of [[true,true],[true,false],[false,true]]){
   const html=renderToString(React.createElement(FxOptionsGrid,{data:d,metric,calls,puts}));
   assert.ok(html.includes('fx-options-table-scroll'));assert.ok(html.includes('Strike / spot'));assert.ok(html.includes('aria-label='));assert.ok(html.includes('Reported in the selected window'));assert.ok(!html.includes('NaN'));
   const count=d.cells.filter(c=>c.side==='call'?calls:puts).reduce((sum,c)=>sum+c.count,0);
   assert.equal(heatmapValues(d,metric,calls,puts).count,count);
   if(metric==='notional')assert.ok(heatmapValues(d,metric,calls,puts).total>0);
   if(metric==='gamma'&&calls&&puts)fragments.push(`<h1>${pair}: estimated gamma</h1>${html}`);
   rendered++;
  }
  console.log('PASS',pair,`${d.included_trades} trades; ${d.modelled_trades} modelled; ${d.status}`);
 }
 fs.writeFileSync('../.runtime/fx-options-render.html',`<!doctype html><meta charset="utf-8"><style>${fs.readFileSync('src/fxOptions.css','utf8')}</style>${fragments.join('')}`);
 console.log(`PASS: ${rendered} real-source heatmap renders across ${pairs.length} pairs, four measures and three side filters.`);
}
main().catch(error=>{console.error(error);process.exitCode=1;});
