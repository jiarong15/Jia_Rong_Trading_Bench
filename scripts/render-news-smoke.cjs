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
const {CalendarWindow}=require('../src/EconomicCalendar.tsx');
const {singaporeDate,tradaysWidgetUrl}=require('../src/tradaysCalendar.ts');
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
  assert.ok(html.includes('aria-label="Open economic calendar"')&&html.includes('aria-controls="economic-calendar-window"'));
  if(route==='/markets/bonds'){
   assert.ok(html.includes('Official sovereign yields')&&html.includes('bonds-quotes'));
   assert.ok(html.includes('aria-label="Bonds country"')&&html.includes('Singapore')&&html.includes('Canada'));
   for(const key of ['2s5s','2s10s','3m10s','5s30s','5s10s','10s30s','2s5s10s','2s10s30s'])assert.ok(html.includes(`<h3>${key}</h3>`));
   assert.equal((html.match(/<details class="bonds-analytics">/g)||[]).length,2,'Both chart sections start collapsed');
   assert.ok(!html.includes('Sources and calculation conventions'));
   assert.ok(!html.includes('SYNTHETIC SNAPSHOT')&&!html.includes('Source: fixture provider'));
  }
  console.log(`PASS shared news launcher: ${route}`);
 }
 const feed={channel:{username:'marketfeed',title:'Market News Feed',url:'https://t.me/marketfeed'},status:'ready',items:[{id:123,text:'<script>untrusted()</script>\nSYNTHETIC TEST HEADLINE',published_at:'2026-01-01T12:00:00Z',url:'javascript:bad()'}],poll_interval_seconds:10,last_checked_at:new Date().toISOString(),last_attempt_at:new Date().toISOString(),retry_after_seconds:10,error:null,cache_warning:null};
 const render=(data,busy=false,error='')=>renderToString(React.createElement(NewsWindow,{feed:data,busy,error,onClose:()=>{},onRetry:()=>{}}));
 const ready=render(feed);
 assert.ok(ready.includes('role="dialog"')&&ready.includes('aria-modal="false"')&&ready.includes('Live · 10s checks'));
 assert.ok(ready.includes('href="https://t.me/marketfeed/123"')&&!ready.includes('href="javascript:'));
 assert.ok(ready.includes(`href="https://www.google.com/search?q=${encodeURIComponent(feed.items[0].text.replace(/\s+/g,' ').trim())}" target="_blank" rel="noopener noreferrer"`));
 assert.ok(ready.includes('Search Google')&&ready.includes('Open post in Telegram'));
 const specialText='Bank of Japan: yen & rates +0.5%? #FX\n日本銀行';
 const special=render({...feed,items:[{...feed.items[0],text:specialText}]});
 const googleHref=special.match(/href="(https:\/\/www\.google\.com\/search\?q=[^"]+)"/)[1];
 assert.equal(new URL(googleHref).searchParams.get('q'),specialText.replace(/\s+/g,' ').trim());
 assert.ok(!/<a\b[^>]*>(?:(?!<\/a>)[\s\S])*<a\b/.test(special),'Post actions must be separate links, without nested anchors.');
 assert.ok(ready.includes('&lt;script&gt;')&&!ready.includes('<script>untrusted()'));
 assert.ok(ready.includes('dateTime="2026-01-01T12:00:00Z"')&&ready.includes('SGT')&&ready.includes('Close market news'));
 const stale=render({...feed,status:'stale',error:'Synthetic source interruption'});
 assert.ok(stale.includes('Saved news')&&stale.includes('Updates interrupted')&&!stale.includes('Live ·'));
 const failed=render({...feed,status:'unavailable',items:[],last_checked_at:null,error:'Synthetic connection failure'});
 assert.ok(failed.includes('Feed unavailable')&&failed.includes('Retry connection')&&failed.includes('No successful source check yet'));
 assert.ok(render(null,true).includes('Loading the latest channel posts'));
 assert.ok(!render(feed,false,'Synthetic backend error').includes('Live ·'));
 const archive={...feed,history_limit:1000,has_more_history:true,history_error:null,history_before:122,items:[...feed.items,{id:122,text:'Synthetic older inflation headline',published_at:'2025-12-31T00:00:00Z',url:'https://t.me/marketfeed/122'}]};
 const searching=renderToString(React.createElement(NewsWindow,{feed:archive,busy:false,error:'',query:'older inflation',onClose:()=>{},onRetry:()=>{},onQueryChange:()=>{},onLoadOlder:()=>{}}));
 const searchText=searching.replace(/<!--.*?-->/g,'');
 assert.ok(searching.includes('aria-label="Search past market news"')&&searching.includes('Clear news search')&&searching.includes('1 match')&&searchText.includes('1,000 posts loaded')&&searching.includes('Searching older news'));
 assert.ok(searching.includes('Synthetic older inflation headline')&&!searching.includes('SYNTHETIC TEST HEADLINE')&&searching.includes('Load older news'));
 const noMatches=renderToString(React.createElement(NewsWindow,{feed:{...archive,has_more_history:false},busy:false,error:'',query:'unmatched',onClose:()=>{},onRetry:()=>{}}));
 assert.ok(noMatches.includes('No matching news')&&!noMatches.includes('Waiting for a connection'));
 const full=render({...archive,has_more_history:false,items:Array.from({length:1000},(_,i)=>({...feed.items[0],id:1000-i,text:`SYNTHETIC ARCHIVE POST ${1000-i}`}))});
 assert.equal((full.match(/class="news-post"/g)||[]).length,1000);
 assert.equal((full.match(/href="https:\/\/www\.google\.com\/search\?q=/g)||[]).length,1000);
 assert.ok(full.includes('1,000-post history limit reached')&&full.includes('SYNTHETIC ARCHIVE POST 1<'));
 const historyFailed=render({...archive,history_error:'Synthetic history outage'});
 assert.ok(historyFailed.includes('Retry older news')&&historyFailed.includes('Live · 10s checks'));
 const calendarHtml=renderToString(React.createElement(CalendarWindow,{onClose:()=>{}}));
 assert.ok(calendarHtml.includes('Tradays')&&calendarHtml.includes('Yesterday')&&calendarHtml.includes('Tomorrow'));
 assert.ok(calendarHtml.includes('importance=8')&&calendarHtml.includes('date_mode=0'));
 assert.ok(calendarHtml.includes('class="tradays-frame"'));
 assert.ok(calendarHtml.includes('Opening Tradays')&&!calendarHtml.includes('Connected')&&!calendarHtml.includes('hourly source'));
 assert.ok(calendarHtml.includes('Reload Tradays calendar'));
 assert.ok(!calendarHtml.includes('ecw-copyright')&&!calendarHtml.includes('calendar-notice')&&!calendarHtml.includes('news-window-footer'));
 assert.equal((calendarHtml.match(/aria-controls="tradays-main-calendar"/g)||[]).length,3);
 assert.equal((calendarHtml.match(/aria-pressed="false"/g)||[]).length,3);
 const src=calendarHtml.match(/<iframe[^>]+src="([^"]+)"/)[1].replaceAll('&amp;','&');
 assert.equal(src,tradaysWidgetUrl(singaporeDate()));
 console.log('PASS Tradays embed: official iframe, high-impact three-day settings, attribution, loading state, timezone and reload controls.');
 console.log('PASS news window: ready, saved, unavailable, loading, safe text/links and timestamps. Browser layout and interaction are not verified by server rendering.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
