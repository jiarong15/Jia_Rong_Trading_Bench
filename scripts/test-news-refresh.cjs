const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const test=require('node:test');
const ts=require('typescript');
function fixture(hidden=false){
 const window=new EventTarget(),document=new EventTarget();document.hidden=hidden;
 let now=1000000,last=0,busy=false,calls=0,tick,cleared=false;
 window.setInterval=(callback,ms)=>{assert.equal(ms,10000);tick=callback;return 42;};
 window.clearInterval=id=>{assert.equal(id,42);cleared=true;};
 const exports={};const source=fs.readFileSync(require.resolve('../src/newsRefresh.ts'),'utf8');
 vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{exports,window,document,Date:{now:()=>now}});
 const stop=exports.watchNewsRefresh(()=>{calls++;last=now;busy=true;},()=>!busy,()=>last);
 return {window,document,stop,exports,tick:()=>{if(!cleared)tick();},advance:ms=>{now+=ms;},finish:()=>{busy=false;},calls:()=>calls,cleared:()=>cleared};
}
test('the app shell checks immediately and every ten seconds without opening the window',()=>{
 const f=fixture();assert.equal(f.calls(),1);f.finish();f.advance(9999);f.tick();assert.equal(f.calls(),1);f.advance(1);f.tick();assert.equal(f.calls(),2);f.finish();f.advance(10000);f.tick();assert.equal(f.calls(),3);f.stop();
});
test('poll, focus and online never overlap an in-flight request',()=>{
 const f=fixture();f.advance(20000);f.tick();f.window.dispatchEvent(new Event('focus'));f.window.dispatchEvent(new Event('online'));assert.equal(f.calls(),1);f.finish();f.tick();assert.equal(f.calls(),2);f.stop();
});
test('hidden tabs pause polls and refresh when returning overdue',()=>{
 const f=fixture(true);assert.equal(f.calls(),0);f.advance(30000);f.tick();assert.equal(f.calls(),0);f.document.hidden=false;f.document.dispatchEvent(new Event('visibilitychange'));assert.equal(f.calls(),1);f.stop();
});
test('focus does not repeatedly check fresh data, and restored connectivity retries',()=>{
 const f=fixture();f.finish();f.window.dispatchEvent(new Event('focus'));assert.equal(f.calls(),1);f.window.dispatchEvent(new Event('online'));assert.equal(f.calls(),2);f.finish();f.document.hidden=true;f.window.dispatchEvent(new Event('online'));assert.equal(f.calls(),2);f.stop();
});
test('unmount removes the interval and every event listener',()=>{
 const f=fixture();f.finish();f.stop();assert.equal(f.cleared(),true);f.advance(30000);f.tick();f.window.dispatchEvent(new Event('online'));f.window.dispatchEvent(new Event('focus'));f.document.dispatchEvent(new Event('visibilitychange'));assert.equal(f.calls(),1);
});
test('initial history and edited existing posts do not create unread notifications',()=>{
 const f=fixture();const count=f.exports.newPostCount;
 assert.equal(count(null,[{id:100}]),0);assert.equal(count(100,[{id:100,text:'edited'},{id:99}]),0);assert.equal(count(100,[{id:102},{id:101},{id:100}]),2);assert.equal(count(100,[]),0);f.stop();
});

test('search matches all keywords without regard to case and preserves source order',()=>{
 const f=fixture();const items=[{id:3,text:'Fed raises interest rates'},{id:2,text:'Bank of Japan keeps rates unchanged'},{id:1,text:'FED officials discuss lower rates'}];
 const before=JSON.stringify(items);
 assert.deepEqual(Array.from(f.exports.filterNewsItems(items,'  FED   rates ').map(item=>item.id)),[3,1]);
 assert.equal(f.exports.filterNewsItems(items,'japan')[0].id,2);
 assert.equal(f.exports.filterNewsItems(items,'unmatched').length,0);
 assert.equal(JSON.stringify(items),before);assert.equal(f.exports.filterNewsItems(items,'   '),items);f.stop();
});

test('search reaches the oldest of 1000 posts and includes newly loaded history',()=>{
 const f=fixture();const items=Array.from({length:1000},(_,i)=>({id:1000-i,text:i===999?'SYNTHETIC oldest inflation headline':'Other news'}));
 assert.equal(f.exports.filterNewsItems(items,'oldest inflation')[0].id,1);
 assert.equal(f.exports.filterNewsItems(items.slice(0,20),'oldest').length,0);
 assert.equal(f.exports.filterNewsItems(items,'[').length,0);f.stop();
});
