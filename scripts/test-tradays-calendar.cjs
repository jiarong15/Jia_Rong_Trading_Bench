const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const test=require('node:test');
const ts=require('typescript');
function fixture(at='2026-10-30T10:00:00Z',hidden=false){
 const window=new EventTarget(),document=new EventTarget();document.hidden=hidden;
 let now=Date.parse(at),calls=0,tick,cleared=false;
 class Clock extends Date{constructor(...args){super(...(args.length?args:[now]));}static now(){return now;}}
 window.setInterval=(callback,ms)=>{assert.equal(ms,1000);tick=callback;return 42;};
 window.clearInterval=id=>{assert.equal(id,42);cleared=true;};
 const exports={};
 const source=fs.readFileSync(require.resolve('../src/tradaysCalendar.ts'),'utf8');
 vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{exports,window,document,Date:Clock,Intl,URLSearchParams});
 const stop=exports.watchTradaysRefresh(()=>calls++);
 return {window,document,exports,stop,tick:()=>{if(!cleared)tick();},advance:ms=>now+=ms,calls:()=>calls,cleared:()=>cleared};
}
test('widget URL selects high impact and three days over month, year and leap-day boundaries',()=>{
 const f=fixture();
 for(const [date,before,after] of [['2026-10-30','2026-10-29','2026-10-31'],['2027-01-01','2026-12-31','2027-01-02'],['2028-03-01','2028-02-29','2028-03-02']]){
  const url=new URL(f.exports.tradaysWidgetUrl(date));
  assert.equal(url.origin,'https://www.tradays.com');assert.equal(url.pathname,'/en/economic-calendar/widget');
  assert.equal(url.searchParams.get('importance'),'8');assert.equal(url.searchParams.get('date_mode'),'0');
  assert.equal(url.searchParams.get('from'),before+'T00:00:00');assert.equal(url.searchParams.get('to'),after+'T23:59:59');
 }
 assert.equal(f.exports.singaporeDate(new Date('2026-10-30T16:00:00Z')),'2026-10-31');f.stop();
});
test('normal ticks do not interrupt the vendor result updater, schedule reconciles at fifteen minutes',()=>{
 const f=fixture();f.advance(899000);f.tick();assert.equal(f.calls(),0);f.window.dispatchEvent(new Event('focus'));assert.equal(f.calls(),0);
 f.advance(1000);f.tick();assert.equal(f.calls(),1);f.tick();assert.equal(f.calls(),1);f.stop();
});
test('date navigation changes only the event anchor and retains the full three-day view',()=>{
 const f=fixture();const base=new URL(f.exports.tradaysWidgetUrl('2026-10-30'));
 for(const anchor of ['prevValue10','prevValue20','prevValue30']){
  const focused=new URL(f.exports.tradaysWidgetUrl('2026-10-30',anchor));
  assert.equal(focused.hash,'#'+anchor);assert.equal(focused.search,base.search);
 }
 assert.throws(()=>f.exports.tradaysWidgetUrl('2026-10-30','javascript:bad'));
 assert.equal(f.exports.calendarDay('2027-01-01',-1),'2026-12-31');f.stop();
});
test('Singapore midnight rolls dates without waiting for reconciliation',()=>{
 const f=fixture('2026-10-30T15:59:59Z');f.advance(1000);f.tick();assert.equal(f.calls(),1);f.tick();assert.equal(f.calls(),1);f.stop();
});
test('hidden pages defer refresh and resume with new dates',()=>{
 const f=fixture('2026-10-30T15:59:59Z',true);f.advance(1000);f.tick();assert.equal(f.calls(),0);
 f.document.hidden=false;f.document.dispatchEvent(new Event('visibilitychange'));assert.equal(f.calls(),1);f.stop();
});
test('returning after five minutes refreshes and a brief switch preserves the frame',()=>{
 const f=fixture();f.document.hidden=true;f.document.dispatchEvent(new Event('visibilitychange'));f.advance(10000);
 f.document.hidden=false;f.document.dispatchEvent(new Event('visibilitychange'));assert.equal(f.calls(),0);
 f.document.hidden=true;f.document.dispatchEvent(new Event('visibilitychange'));f.advance(300000);
 f.document.hidden=false;f.document.dispatchEvent(new Event('visibilitychange'));assert.equal(f.calls(),1);f.stop();
});
test('reconnection while hidden is remembered and refreshes on return',()=>{
 const f=fixture();f.window.dispatchEvent(new Event('online'));assert.equal(f.calls(),1);
 f.document.hidden=true;f.document.dispatchEvent(new Event('visibilitychange'));f.window.dispatchEvent(new Event('online'));assert.equal(f.calls(),1);
 f.document.hidden=false;f.document.dispatchEvent(new Event('visibilitychange'));assert.equal(f.calls(),2);f.stop();
});
test('closing removes refresh timers and reconnect listeners',()=>{
 const f=fixture();f.stop();assert.equal(f.cleared(),true);f.advance(900000);f.tick();
 f.window.dispatchEvent(new Event('online'));f.window.dispatchEvent(new Event('focus'));f.document.dispatchEvent(new Event('visibilitychange'));assert.equal(f.calls(),0);
});
