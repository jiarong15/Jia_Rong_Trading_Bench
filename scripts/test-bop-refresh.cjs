const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const test=require('node:test');
const ts=require('typescript');

function fixture(){
 const window=new EventTarget(),document=new EventTarget();
 document.hidden=false;
 let now=1000000,last=now,busy=false,calls=0,tick,cleared=false;
 window.setInterval=(callback,ms)=>{assert.equal(ms,300000);tick=callback;return 42;};
 window.clearInterval=id=>{assert.equal(id,42);cleared=true;};
 const exports={};
 const source=fs.readFileSync(require.resolve('../src/bopRefresh.ts'),'utf8');
 const output=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 vm.runInNewContext(output,{exports,window,document,Date:{now:()=>now}});
 const stop=exports.watchBopRefresh(()=>{calls++;last=now;busy=true;},()=>!busy,()=>last);
 return {window,document,stop,tick:()=>{if(!cleared)tick();},advance:ms=>{now+=ms;},finish:()=>{busy=false;},block:()=>{busy=true;},calls:()=>calls,cleared:()=>cleared};
}

test('an open visible page checks sources every five minutes without reloading',()=>{
 const f=fixture();assert.equal(f.calls(),0);
 f.advance(300000);f.tick();assert.equal(f.calls(),1);
 f.finish();f.advance(300000);f.tick();assert.equal(f.calls(),2);f.stop();
});

test('hidden tabs pause requests and catch up immediately when visible again',()=>{
 const f=fixture();f.document.hidden=true;
 f.advance(900000);f.tick();assert.equal(f.calls(),0);
 f.document.hidden=false;f.document.dispatchEvent(new Event('visibilitychange'));
 assert.equal(f.calls(),1);f.stop();
});

test('focus checks an overdue page but does not repeatedly download fresh data',()=>{
 const f=fixture();f.window.dispatchEvent(new Event('focus'));assert.equal(f.calls(),0);
 f.advance(300000);f.window.dispatchEvent(new Event('focus'));assert.equal(f.calls(),1);
 f.finish();f.window.dispatchEvent(new Event('focus'));assert.equal(f.calls(),1);f.stop();
});

test('poll, focus and connectivity events never overlap an in-flight request',()=>{
 const f=fixture();f.block();f.advance(300000);f.tick();
 f.window.dispatchEvent(new Event('focus'));f.window.dispatchEvent(new Event('online'));
 assert.equal(f.calls(),0);
 f.finish();f.window.dispatchEvent(new Event('online'));assert.equal(f.calls(),1);
 f.tick();assert.equal(f.calls(),1);f.stop();
});

test('restored connectivity retries promptly and still respects a hidden tab',()=>{
 const f=fixture();f.window.dispatchEvent(new Event('online'));assert.equal(f.calls(),1);
 f.finish();f.document.hidden=true;f.window.dispatchEvent(new Event('online'));
 assert.equal(f.calls(),1);f.stop();
});

test('unmount removes timers and all event listeners',()=>{
 const f=fixture();f.stop();assert.equal(f.cleared(),true);f.advance(300000);
 f.tick();f.window.dispatchEvent(new Event('focus'));f.window.dispatchEvent(new Event('online'));
 f.document.dispatchEvent(new Event('visibilitychange'));assert.equal(f.calls(),0);
});
