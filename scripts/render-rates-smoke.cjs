const fs=require('node:fs'),assert=require('node:assert/strict'),ts=require('typescript'),React=require('react'),{renderToString}=require('react-dom/server');
require.extensions['.css']=()=>{};
for(const extension of ['.tsx','.ts'])require.extensions[extension]=(module,filename)=>module._compile(ts.transpileModule(fs.readFileSync(filename,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText,filename);
async function main(){
 const response=await fetch('http://127.0.0.1:8000/api/rates/us?saved_only=true');assert.equal(response.status,200);const data=await response.json();
 const {RatesChartPanel}=require('../src/RatesPage.tsx');
 const html=renderToString(React.createElement('main',null,...data.charts.map(chart=>React.createElement(RatesChartPanel,{key:chart.key,chart,period:'5Y'}))));
 const swap=data.charts.find(chart=>chart.key==='inflation_expectations').series.find(series=>series.key==='inflation_swap');
 assert.equal((html.match(/class="panel rates-panel/g)||[]).length,7);assert.ok(html.includes('CPI goods &amp; services'));assert.ok(html.includes('JOLTS'));assert.ok(html.includes('Indeed'));assert.ok(html.includes('Housing under construction'));assert.ok(html.includes('DTCC reported trades'));assert.ok(html.includes('Last trade'));assert.ok(swap.last_value!==null&&swap.observations.length>1);assert.ok(!html.includes('Source needed'));assert.ok(!html.includes('NaN'));assert.ok(!html.includes('SYNTHETIC'));
 fs.writeFileSync('../.runtime/us-rates-render.html',html);console.log('PASS: seven real-source panels render, including the DTCC inflation swap trade median, actual trade time and reporting-delay labels.');
}
main().catch(error=>{console.error(error.message);process.exitCode=1;});
