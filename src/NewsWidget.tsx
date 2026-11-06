import {useCallback,useEffect,useMemo,useRef,useState} from 'react';
import type {Ref} from 'react';
import {ArrowUpRight,CalendarDays,MessageSquareText,Radio,RefreshCw,Search,X} from 'lucide-react';
import {CalendarWindow} from './EconomicCalendar';
import {api} from './api';
import {filterNewsItems,newPostCount,watchNewsRefresh} from './newsRefresh';
import type {NewsItem} from './newsRefresh';
import './news.css';

export type NewsFeed={channel:{username:string;title:string;url:string;preview_url:string};status:'ready'|'stale'|'unavailable'|'disabled';items:NewsItem[];poll_interval_seconds:number;last_checked_at:string|null;last_attempt_at:string|null;retry_after_seconds:number;error:string|null;cache_warning:string|null;history_limit?:number;has_more_history?:boolean;history_exhausted?:boolean;history_retry_after_seconds?:number;history_error?:string|null;history_before?:number|null;oldest_id?:number|null};
const stamp=(value:string)=>new Date(value).toLocaleString('en-SG',{timeZone:'Asia/Singapore',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
const isFresh=(feed:NewsFeed|null)=>Boolean(feed?.status==='ready'&&feed.last_checked_at&&Date.now()-Date.parse(feed.last_checked_at)<=30_000);

export function NewsWindow({feed,busy,error,onClose,onRetry,closeRef,query='',onQueryChange,onLoadOlder,historyBusy=false,historyError=''}:{feed:NewsFeed|null;busy:boolean;error:string;onClose:()=>void;onRetry:()=>void;closeRef?:Ref<HTMLButtonElement>;query?:string;onQueryChange?:(value:string)=>void;onLoadOlder?:()=>void;historyBusy?:boolean;historyError?:string}){
 const connected=isFresh(feed)&&!error;
 const message=error||feed?.error||'';
 const historyMessage=historyError||feed?.history_error||'';
 const listRef=useRef<HTMLDivElement>(null);
 const shown=useMemo(()=>filterNewsItems(feed?.items??[],query),[feed?.items,query]);
 const hasMore=feed?.has_more_history??false;
 useEffect(()=>{if(listRef.current)listRef.current.scrollTop=0;},[query]);
 return <section className="news-window" id="market-news-window" role="dialog" aria-modal="false" aria-labelledby="market-news-title">
  <header className="news-window-header"><span className="news-window-icon"><Radio size={20}/></span><div><h2 id="market-news-title">Market news</h2><a href="https://t.me/marketfeed" target="_blank" rel="noopener noreferrer">Market News Feed · @marketfeed<ArrowUpRight size={12}/></a></div><button ref={closeRef} className="news-icon-button" type="button" aria-label="Close market news" onClick={onClose}><X size={19}/></button></header>
  <div className="news-window-toolbar"><span className={`news-connection ${connected?'connected':''}`}><i/>{connected?'Live · 10s checks':busy&&!feed?.items.length?'Connecting…':feed?.items.length?'Saved news':'Not connected'}</span><button type="button" className="news-icon-button" aria-label="Refresh market news" disabled={busy} onClick={onRetry}><RefreshCw size={14} className={busy?'spin':''}/></button></div>
  {message&&<div className="news-window-error" role="status"><strong>{feed?.items.length?'Updates interrupted':'Feed unavailable'}</strong><p>{message}</p><button type="button" disabled={busy} onClick={onRetry}>{busy?'Checking…':'Retry connection'}</button></div>}
  {feed?.cache_warning&&<p className="news-cache-warning">{feed.cache_warning}</p>}
  <div className="news-search"><Search size={15} aria-hidden="true"/><input type="search" aria-label="Search past market news" placeholder="Search past news…" maxLength={200} value={query} onChange={event=>onQueryChange?.(event.target.value)}/>{query&&<button type="button" className="news-icon-button" aria-label="Clear news search" onClick={()=>onQueryChange?.('')}><X size={14}/></button>}</div>
  <div className="news-search-count" role="status">{query.trim()?`${shown.length} ${shown.length===1?'match':'matches'} · `:''}{feed?.items.length??0} / {(feed?.history_limit??1000).toLocaleString('en-SG')} posts loaded{query.trim()&&hasMore&&!historyMessage&&<span>Searching older news…</span>}</div>
  <div ref={listRef} className="news-post-list" aria-label="Market News Feed posts" onScroll={event=>{const list=event.currentTarget;if(hasMore&&!historyMessage&&!historyBusy&&list.scrollHeight-list.scrollTop-list.clientHeight<140)onLoadOlder?.();}}>
   {shown.length?<ol>{shown.map(item=><li key={item.id}><article className="news-post"><a className="news-post-link" href={`https://t.me/marketfeed/${item.id}`} target="_blank" rel="noopener noreferrer"><div className="news-post-meta"><time dateTime={item.published_at}>{stamp(item.published_at)} SGT</time><ArrowUpRight size={13} aria-hidden="true"/></div><p>{item.text}</p></a><div className="news-post-actions"><a className="news-post-source" href={`https://t.me/marketfeed/${item.id}`} target="_blank" rel="noopener noreferrer">Open post in Telegram<ArrowUpRight size={12} aria-hidden="true"/></a><a className="news-post-source" href={`https://www.google.com/search?q=${encodeURIComponent(item.text.replace(/\s+/g,' ').trim())}`} target="_blank" rel="noopener noreferrer"><Search size={12} aria-hidden="true"/>Search Google<ArrowUpRight size={12} aria-hidden="true"/></a></div></article></li>)}</ol>:query.trim()&&!!feed?.items.length?<div className="news-empty news-search-empty"><Search size={27}/><h3>No matching news</h3><p>{hasMore?'Searching older posts expands these results.':'Try another keyword or clear your search.'}</p></div>:busy?<div className="news-loading" role="status"><span/><span/><span/><p>Loading the latest channel posts…</p></div>:<div className="news-empty"><MessageSquareText size={29}/><h3>{message?'Waiting for a connection':'No posts available yet'}</h3><p>New posts will appear here when the channel preview is available.</p></div>}
   {!!feed?.items.length&&<div className="news-history-controls">{historyMessage&&<p role="status">Older news: {historyMessage}</p>}{hasMore?<button type="button" disabled={historyBusy||busy} onClick={onLoadOlder}>{historyBusy?'Loading older news…':historyMessage?'Retry older news':'Load older news'}</button>:<span>{feed.items.length>=(feed.history_limit??1000)?'1,000-post history limit reached.':feed.history_exhausted?'All available history loaded.':''}</span>}</div>}
  </div>
  <footer className="news-window-footer"><span>{feed?.last_checked_at?`Last checked ${stamp(feed.last_checked_at)} SGT`:'No successful source check yet'}</span><span>Public channel · checks every 10s</span></footer>
 </section>;
}

export default function NewsWidget(){
 const [calendarOpen,setCalendarOpen]=useState(false);const calendarTrigger=useRef<HTMLButtonElement>(null);const calendarClose=useRef<HTMLButtonElement>(null);
 const closeCalendar=useCallback(()=>{setCalendarOpen(false);calendarTrigger.current?.focus();},[]);
 useEffect(()=>{if(!calendarOpen)return;calendarClose.current?.focus();const keys=(event:KeyboardEvent)=>{if(event.key==='Escape'&&!event.defaultPrevented&&!document.querySelector('[aria-modal="true"]')&&document.getElementById('economic-calendar-window')?.contains(document.activeElement)){event.preventDefault();closeCalendar();}};window.addEventListener('keydown',keys);return()=>window.removeEventListener('keydown',keys);},[calendarOpen,closeCalendar]);
 const [open,setOpen]=useState(false);const [feed,setFeed]=useState<NewsFeed|null>(null);const [busy,setBusy]=useState(false);const [error,setError]=useState('');const [unread,setUnread]=useState(0);
 const [query,setQuery]=useState('');const [historyBusy,setHistoryBusy]=useState(false);const [historyError,setHistoryError]=useState('');const [olderRequested,setOlderRequested]=useState(false);
 const feedRef=useRef<NewsFeed|null>(null);const nextHistoryAt=useRef(0);
 const openRef=useRef(false);const inFlight=useRef(false);const lastAttempt=useRef(0);const highestId=useRef<number|null>(null);const request=useRef<AbortController|null>(null);const triggerRef=useRef<HTMLButtonElement>(null);const closeRef=useRef<HTMLButtonElement>(null);
 const load=useCallback(async(older=false)=>{
  if(inFlight.current)return;
  const previous=feedRef.current;
  if(older&&(!previous?.has_more_history||!previous.items.length||Date.now()<nextHistoryAt.current))return;
  const controller=new AbortController();request.current=controller;inFlight.current=true;
  if(older){setHistoryBusy(true);setHistoryError('');}else{lastAttempt.current=Date.now();setBusy(true);}
  let timedOut=false;const timeout=window.setTimeout(()=>{timedOut=true;controller.abort();},15_000);
  try{
   const path=older?`/news/telegram/history?before=${previous!.history_before??previous!.items.at(-1)!.id}`:'/news/telegram';
   const result=await api<NewsFeed>(path,{signal:controller.signal,cache:'no-store'});
   if(request.current!==controller)return;
   if(!Array.isArray(result.items)||!['ready','stale','unavailable','disabled'].includes(result.status)||result.items.some(item=>!Number.isSafeInteger(item.id)||item.id<=0||typeof item.text!=='string'||!Number.isFinite(Date.parse(item.published_at))))throw new Error('The news server returned an invalid feed.');
   const count=newPostCount(highestId.current,result.items);
   if(!openRef.current&&count)setUnread(value=>Math.min(999,value+count));
   if(result.items.length)highestId.current=Math.max(highestId.current??0,...result.items.map(item=>item.id));
   feedRef.current=result;nextHistoryAt.current=Date.now()+(result.history_retry_after_seconds??0)*1000;setFeed(result);if(!older)setError('');
  }catch(e){if(request.current===controller&&(!controller.signal.aborted||timedOut)){
   const message=timedOut?'The news request timed out. Please retry.':e instanceof Error?e.message:'The news connection failed. Please retry.';
   if(older){setHistoryError(message);nextHistoryAt.current=Date.now()+10_000;}else setError(message);
  }}
  finally{window.clearTimeout(timeout);if(request.current===controller){inFlight.current=false;if(older){setHistoryBusy(false);setOlderRequested(false);}else setBusy(false);}}
 },[]);
 useEffect(()=>{
  const stop=watchNewsRefresh(()=>void load(),()=>!inFlight.current,()=>lastAttempt.current);
  return()=>{stop();request.current?.abort();request.current=null;inFlight.current=false;};
 },[load]);
 useEffect(()=>{
  if(!open||!feed?.has_more_history||(!olderRequested&&!query.trim()))return;
  if(!olderRequested&&(historyError||feed.history_error))return;
  let timer:number|undefined;
  const schedule=()=>{
   window.clearTimeout(timer);
   if(document.hidden||inFlight.current)return;
   timer=window.setTimeout(()=>{if(!document.hidden){if(Date.now()-lastAttempt.current>=10_000)void load();else void load(true);}},Math.max(200,nextHistoryAt.current-Date.now()));
  };
  schedule();document.addEventListener('visibilitychange',schedule);
  return()=>{window.clearTimeout(timer);document.removeEventListener('visibilitychange',schedule);};
 },[open,query,feed,olderRequested,historyBusy,busy,historyError,load]);
 const close=useCallback(()=>{openRef.current=false;setOpen(false);triggerRef.current?.focus();},[]);
 useEffect(()=>{
  if(!open)return;
  closeRef.current?.focus();
  const keys=(event:KeyboardEvent)=>{if(event.key==='Escape'&&!event.defaultPrevented&&!document.querySelector('[aria-modal="true"]')&&!document.getElementById('economic-calendar-window')?.contains(document.activeElement)){event.preventDefault();close();}};
  window.addEventListener('keydown',keys);return()=>window.removeEventListener('keydown',keys);
 },[open,close]);
 const toggle=()=>{
  if(open){close();return;}
  openRef.current=true;setUnread(0);setOpen(true);
  if(!document.hidden&&Date.now()-lastAttempt.current>=10_000)void load();
 };
 const connected=isFresh(feed)&&!error;
 return <aside className="news-widget" aria-label="Market news">
  <div className="feed-windows">
  {calendarOpen&&<CalendarWindow onClose={closeCalendar} closeRef={calendarClose}/>}
  {open&&<NewsWindow feed={feed} busy={busy} error={error} onClose={close} onRetry={()=>void load()} closeRef={closeRef} query={query} onQueryChange={setQuery} historyBusy={historyBusy||olderRequested} historyError={historyError} onLoadOlder={()=>setOlderRequested(true)}/>}
  </div>
  <button ref={calendarTrigger} className={`news-launcher calendar-launcher ${calendarOpen?'active':''}`} type="button" aria-controls="economic-calendar-window" aria-expanded={calendarOpen} aria-label={`${calendarOpen?'Close':'Open'} economic calendar`} onClick={()=>{if(calendarOpen)closeCalendar();else setCalendarOpen(true);}}><CalendarDays size={20}/><span>Economic calendar</span><small className="calendar-provider-label">TRADAYS</small></button>
  <button ref={triggerRef} className={`news-launcher ${open?'active':''}`} type="button" aria-controls="market-news-window" aria-expanded={open} aria-label={`${open?'Close':'Open'} market news${unread?`, ${unread} unread posts`:''}`} onClick={toggle}><MessageSquareText size={20}/><span>News</span><i className={connected?'connected':''} aria-hidden="true"/>{unread>0&&<span className="news-unread" aria-hidden="true">{unread>99?'99+':unread}</span>}</button>
  <span className="sr-only" role="status">{unread>0?`${unread} new market news ${unread===1?'post':'posts'}`:''}</span>
 </aside>;
}
