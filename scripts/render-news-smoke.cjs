// Render the actual shared shell on every route without a managed-browser
// debugging session. Live source availability is checked separately via API.
const fs=require('node:fs');
const assert=require('node:assert/strict');
const ts=require('typescript');
const React=require('react');
const {renderToString}=require('react-dom/server');
const {MemoryRouter}=require('react-router-dom');
require.extensions['.css']=()=>{};
for(const extension of ['.tsx','.ts'])require.extensions[extension]=function(module,filename){
 module._compile(ts.transpileModule(fs.readFileSync(filename,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true},fileName:filename}).outputText,filename);
};
const App=require('../src/App.tsx').default;
const {NewsWindow}=require('../src/NewsWidget.tsx');
async function main(){
 const response=await fetch('http://127.0.0.1:8000/api/workspace');assert.ok(response.ok);const workspace=await response.json();
 const useState=React.useState;
 for(const route of ['/','/markets/equities','/markets/bonds','/markets/rates','/markets/fx','/markets/commodities','/research','/portfolio','/backtests','/data','/settings']){
  let first=true;
  React.useState=function(initial){if(first){first=false;return useState(workspace);}return useState(initial);};
  let html;
  try{html=renderToString(React.createElement(MemoryRouter,{initialEntries:[route]},React.createElement(App)));}finally{React.useState=useState;}
  assert.equal((html.match(/class="news-widget"/g)||[]).length,1,`News launcher missing or duplicated on ${route}`);
  assert.ok(html.includes('aria-label="Open market news"')&&html.includes('aria-expanded="false"'));
  console.log(`PASS shared news launcher: ${route}`);
 }
 const feed={channel:{username:'marketfeed',title:'Market News Feed',url:'https://t.me/marketfeed'},status:'ready',items:[{id:123,text:'<script>untrusted()</script>\nSYNTHETIC TEST HEADLINE',published_at:'2026-01-01T12:00:00Z',url:'javascript:bad()'}],poll_interval_seconds:10,last_checked_at:new Date().toISOString(),last_attempt_at:new Date().toISOString(),retry_after_seconds:10,error:null,cache_warning:null};
 const render=(data,busy=false,error='')=>renderToString(React.createElement(NewsWindow,{feed:data,busy,error,onClose:()=>{},onRetry:()=>{}}));
 const ready=render(feed);
 assert.ok(ready.includes('role="dialog"')&&ready.includes('aria-modal="false"')&&ready.includes('Live · 10s checks'));
 assert.ok(ready.includes('href="https://t.me/marketfeed/123"')&&!ready.includes('href="javascript:'));
 assert.ok(ready.includes('&lt;script&gt;')&&!ready.includes('<script>untrusted()'));
 assert.ok(ready.includes('dateTime="2026-01-01T12:00:00Z"')&&ready.includes('SGT')&&ready.includes('Close market news'));
 const stale=render({...feed,status:'stale',error:'Synthetic source interruption'});
 assert.ok(stale.includes('Saved news')&&stale.includes('Updates interrupted')&&!stale.includes('Live ·'));
 const failed=render({...feed,status:'unavailable',items:[],last_checked_at:null,error:'Synthetic connection failure'});
 assert.ok(failed.includes('Feed unavailable')&&failed.includes('Retry connection')&&failed.includes('No successful source check yet'));
 assert.ok(render(null,true).includes('Loading the latest channel posts'));
 assert.ok(!render(feed,false,'Synthetic backend error').includes('Live ·'));
 console.log('PASS news window: ready, saved, unavailable, loading, safe text/links and timestamps. Browser layout and interaction are not verified by server rendering.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
