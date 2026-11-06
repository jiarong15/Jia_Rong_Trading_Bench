import {useCallback,useEffect,useRef,useState} from 'react';
import type {Ref} from 'react';
import {ArrowUpRight,CalendarDays,RefreshCw,X} from 'lucide-react';
import {calendarDay,singaporeDate,tradaysWidgetUrl,watchTradaysRefresh} from './tradaysCalendar';
import {api} from './api';
import './calendar.css';

export function CalendarWindow({onClose,closeRef}:{onClose:()=>void;closeRef?:Ref<HTMLButtonElement>}){
 const [today,setToday]=useState(()=>singaporeDate()),[generation,setGeneration]=useState(0);
 const [state,setState]=useState<'loading'|'embedded'|'slow'|'offline'>(()=>typeof navigator==='undefined'||navigator.onLine!==false?'loading':'offline');
 const [selected,setSelected]=useState<number|null>(null),[anchor,setAnchor]=useState(''),[focusBusy,setFocusBusy]=useState<number|null>(null),[focusMessage,setFocusMessage]=useState('');
 const focusRequest=useRef<AbortController|null>(null);
 const frame=useRef<HTMLIFrameElement>(null);
 const reload=useCallback(()=>{focusRequest.current?.abort();focusRequest.current=null;setFocusBusy(null);setFocusMessage('');setSelected(null);setAnchor('');setToday(singaporeDate());setState(navigator.onLine?'loading':'offline');setGeneration(value=>value+1);},[]);
 useEffect(()=>()=>{focusRequest.current?.abort();focusRequest.current=null;},[]);
 useEffect(()=>watchTradaysRefresh(reload),[reload]);
 useEffect(()=>{const offline=()=>setState('offline');window.addEventListener('offline',offline);return()=>window.removeEventListener('offline',offline);},[]);
 useEffect(()=>{if(state!=='loading')return;const timer=window.setTimeout(()=>setState('slow'),20_000);return()=>window.clearTimeout(timer);},[state,generation]);
 const days=[-1,0,1].map(offset=>{const date=new Date(Date.parse(today+'T12:00:00+08:00')+offset*86_400_000);return date.toLocaleDateString('en-SG',{timeZone:'Asia/Singapore',weekday:'short',day:'numeric',month:'short'});});
 async function focusDay(index:number){
  focusRequest.current?.abort();const controller=new AbortController();focusRequest.current=controller;setFocusBusy(index);setFocusMessage('');
  const timer=window.setTimeout(()=>controller.abort(),12_000);
  try{
   const result=await api<{status:string;today:string;targets:Record<string,string|null>;error:string|null}>('/calendar/tradays/focus',{signal:controller.signal,cache:'no-store'});
   if(focusRequest.current!==controller)return;
   if(result.status!=='ready')throw new Error(result.error||'Date navigation is temporarily unavailable.');
   if(result.today!==today){reload();return;}
   const target=result.targets[calendarDay(today,index-1)];
   if(target===null){setSelected(index);setFocusMessage(`No high-impact releases on ${days[index]}.`);return;}
   if(typeof target!=='string'||!/^prevValue[1-9]\d{0,11}$/.test(target))throw new Error('Tradays did not supply a valid date position.');
   setSelected(index);
   if(anchor===target&&frame.current)frame.current.src=tradaysWidgetUrl(today,target);
   else setAnchor(target);
  }catch(error){if(focusRequest.current===controller)setFocusMessage(controller.signal.aborted?'Date navigation timed out. Try the date button again.':error instanceof Error?error.message:'Could not focus this date.');}
  finally{window.clearTimeout(timer);if(focusRequest.current===controller){focusRequest.current=null;setFocusBusy(null);}}
 }
 return <section className="calendar-window" id="economic-calendar-window" role="dialog" aria-modal="false" aria-labelledby="economic-calendar-title">
  <header className="news-window-header"><span className="news-window-icon"><CalendarDays size={20}/></span><div><h2 id="economic-calendar-title">Economic releases</h2><a href="https://www.tradays.com/en/economic-calendar" target="_blank" rel="noopener noreferrer">Tradays · MetaQuotes<ArrowUpRight size={12}/></a></div><button ref={closeRef} className="news-icon-button" type="button" aria-label="Close economic calendar" onClick={onClose}><X size={19}/></button></header>
  <div className="news-window-toolbar"><span className="calendar-provider-status" role="status">{state==='offline'?'Offline · reconnecting automatically':state==='loading'?'Opening Tradays…':state==='slow'?'Tradays is taking longer to load':'Tradays · automatic release updates'}</span><span className="calendar-impact"><i/>High impact</span><button className="news-icon-button" type="button" aria-label="Reload Tradays calendar" onClick={reload}><RefreshCw size={14} className={state==='loading'?'spin':''}/></button></div>
  <div className="calendar-day-range" aria-label="Focus calendar date">{['Yesterday','Today','Tomorrow'].map((label,index)=><button type="button" key={label} className={selected===index?'active':''} aria-pressed={selected===index} aria-controls="tradays-main-calendar" aria-busy={focusBusy===index} onClick={()=>void focusDay(index)}><strong>{label}{focusBusy===index?'…':''}</strong><small>{days[index]}</small></button>)}</div>
  {focusMessage&&<p className="calendar-focus-message" role="status">{focusMessage}</p>}
  {(state==='slow'||state==='offline')&&<div className="news-window-error" role="status"><strong>{state==='offline'?'Calendar needs an internet connection':'Calendar has not confirmed loading'}</strong><p>{state==='offline'?'Tradays will reload when connectivity returns.':'If the frame is blank or shows an access error, reload it or open Tradays directly.'}</p><a href={tradaysWidgetUrl(today)} target="_blank" rel="noopener noreferrer">Open Tradays calendar<ArrowUpRight size={12}/></a></div>}
  <div className="tradays-viewport">
   <iframe ref={frame} id="tradays-main-calendar" key={`${today}:${generation}`} className="tradays-frame" src={tradaysWidgetUrl(today,anchor)} title="Tradays live high-impact economic calendar — yesterday, today and tomorrow" onLoad={()=>{if(navigator.onLine)setState('embedded');}} onError={()=>setState(navigator.onLine?'slow':'offline')}/>
  </div>
 </section>;
}
