export const TRADAYS_WIDGET='https://www.tradays.com/en/economic-calendar/widget';
export const singaporeDate=(now=new Date())=>new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Singapore',year:'numeric',month:'2-digit',day:'2-digit'}).format(now);

export const calendarDay=(today:string,offset:number)=>new Date(Date.parse(today+'T12:00:00+08:00')+offset*86_400_000).toISOString().slice(0,10);

export function tradaysWidgetUrl(today=singaporeDate(),anchor=''){
 const noon=Date.parse(today+'T12:00:00+08:00');
 if(!Number.isFinite(noon))throw new Error('Invalid calendar date.');
 const day=(offset:number)=>new Date(noon+offset*86_400_000).toISOString().slice(0,10);
 // Provider-supported settings checked against the live widget. Keep vendor
 // rendering, result polling and attribution intact. Dates use its selected zone.
 const params=new URLSearchParams({mode:'2',date_mode:'0',importance:'8',from:day(-1)+'T00:00:00',to:day(1)+'T23:59:59',dateFormat:'DMY',utm_source:'jr-trading-workbench'});
 if(anchor&&!/^prevValue[1-9]\d{0,11}$/.test(anchor))throw new Error('Invalid calendar anchor.');
 return `${TRADAYS_WIDGET}?${params}${anchor?'#'+anchor:''}`;
}

// Reconcile schedule revisions and midnight rollover. Tradays' own release
// updater handles actual results between document reloads.
export function watchTradaysRefresh(reload:()=>void){
 let day=singaporeDate(),last=Date.now(),hiddenAt:number|null=document.hidden?Date.now():null,needsReconnect=false;
 const check=(force=false)=>{
  if(document.hidden)return;
  const current=singaporeDate(),now=Date.now();
  if(force||needsReconnect||current!==day||now-last>=15*60_000){day=current;last=now;needsReconnect=false;reload();}
 };
 const tick=()=>check();
 const visibility=()=>{if(document.hidden){hiddenAt=Date.now();return;}const overdue=hiddenAt!==null&&Date.now()-hiddenAt>=5*60_000;hiddenAt=null;check(overdue);};
 const online=()=>{needsReconnect=true;check();};
 const timer=window.setInterval(tick,1000);
 document.addEventListener('visibilitychange',visibility);window.addEventListener('focus',tick);window.addEventListener('online',online);
 return()=>{window.clearInterval(timer);document.removeEventListener('visibilitychange',visibility);window.removeEventListener('focus',tick);window.removeEventListener('online',online);};
}
