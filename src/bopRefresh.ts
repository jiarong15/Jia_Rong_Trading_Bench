export const BOP_REFRESH_MS=5*60*1000;

// The first source check belongs to the page load. This watcher keeps an open
// page current, pauses hidden tabs, and catches up when the user returns.
export function watchBopRefresh(refresh:()=>void,canRefresh:()=>boolean,lastAttempt:()=>number){
 const check=(force=false)=>{
  if(!document.hidden&&canRefresh()&&(force||Date.now()-lastAttempt()>=BOP_REFRESH_MS))refresh();
 };
 const tick=()=>check(true);
 const resume=()=>check();
 const online=()=>check(true);
 const timer=window.setInterval(tick,BOP_REFRESH_MS);
 document.addEventListener('visibilitychange',resume);
 window.addEventListener('focus',resume);
 window.addEventListener('online',online);
 return()=>{
  window.clearInterval(timer);
  document.removeEventListener('visibilitychange',resume);
  window.removeEventListener('focus',resume);
  window.removeEventListener('online',online);
 };
}
