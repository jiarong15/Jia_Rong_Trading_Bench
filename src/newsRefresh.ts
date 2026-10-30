export const NEWS_POLL_MS=10_000;

// One watcher belongs to the app shell, so changing routes keeps the feed alive.
export function watchNewsRefresh(refresh:()=>void,canRefresh:()=>boolean,lastAttempt:()=>number){
 const check=(force=false)=>{
  if(!document.hidden&&canRefresh()&&(force||Date.now()-lastAttempt()>=NEWS_POLL_MS))refresh();
 };
 const tick=()=>check();
 const resume=()=>check();
 const online=()=>check(true);
 const timer=window.setInterval(tick,NEWS_POLL_MS);
 document.addEventListener('visibilitychange',resume);
 window.addEventListener('focus',resume);
 window.addEventListener('online',online);
 check(true);
 return()=>{
  window.clearInterval(timer);
  document.removeEventListener('visibilitychange',resume);
  window.removeEventListener('focus',resume);
  window.removeEventListener('online',online);
 };
}

export type NewsItem={id:number;text:string;published_at:string;url:string};
export function newPostCount(previousHighest:number|null,items:NewsItem[]){
 return previousHighest===null?0:items.filter(item=>item.id>previousHighest).length;
}
