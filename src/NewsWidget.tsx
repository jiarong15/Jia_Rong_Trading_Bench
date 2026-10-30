import {useCallback,useEffect,useRef,useState} from 'react';
import type {Ref} from 'react';
import {ArrowUpRight,MessageSquareText,Radio,RefreshCw,X} from 'lucide-react';
import {api} from './api';
import {newPostCount,watchNewsRefresh} from './newsRefresh';
import type {NewsItem} from './newsRefresh';
import './news.css';

export type NewsFeed={channel:{username:string;title:string;url:string;preview_url:string};status:'ready'|'stale'|'unavailable'|'disabled';items:NewsItem[];poll_interval_seconds:number;last_checked_at:string|null;last_attempt_at:string|null;retry_after_seconds:number;error:string|null;cache_warning:string|null};
const stamp=(value:string)=>new Date(value).toLocaleString('en-SG',{timeZone:'Asia/Singapore',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
const isFresh=(feed:NewsFeed|null)=>Boolean(feed?.status==='ready'&&feed.last_checked_at&&Date.now()-Date.parse(feed.last_checked_at)<=30_000);

export function NewsWindow({feed,busy,error,onClose,onRetry,closeRef}:{feed:NewsFeed|null;busy:boolean;error:string;onClose:()=>void;onRetry:()=>void;closeRef?:Ref<HTMLButtonElement>}){
 const connected=isFresh(feed)&&!error;
 const message=error||feed?.error||'';
 return <section className="news-window" id="market-news-window" role="dialog" aria-modal="false" aria-labelledby="market-news-title">
  <header className="news-window-header"><span className="news-window-icon"><Radio size={20}/></span><div><h2 id="market-news-title">Market news</h2><a href="https://t.me/marketfeed" target="_blank" rel="noopener noreferrer">Market News Feed · @marketfeed<ArrowUpRight size={12}/></a></div><button ref={closeRef} className="news-icon-button" type="button" aria-label="Close market news" onClick={onClose}><X size={19}/></button></header>
  <div className="news-window-toolbar"><span className={`news-connection ${connected?'connected':''}`}><i/>{connected?'Live · 10s checks':busy&&!feed?.items.length?'Connecting…':feed?.items.length?'Saved news':'Not connected'}</span><button type="button" className="news-icon-button" aria-label="Refresh market news" disabled={busy} onClick={onRetry}><RefreshCw size={14} className={busy?'spin':''}/></button></div>
  {message&&<div className="news-window-error" role="status"><strong>{feed?.items.length?'Updates interrupted':'Feed unavailable'}</strong><p>{message}</p><button type="button" disabled={busy} onClick={onRetry}>{busy?'Checking…':'Retry connection'}</button></div>}
  {feed?.cache_warning&&<p className="news-cache-warning">{feed.cache_warning}</p>}
  <div className="news-post-list" aria-label="Market News Feed posts">
   {feed?.items.length?<ol>{feed.items.map(item=><li key={item.id}><a className="news-post" href={`https://t.me/marketfeed/${item.id}`} target="_blank" rel="noopener noreferrer"><div className="news-post-meta"><time dateTime={item.published_at}>{stamp(item.published_at)} SGT</time><ArrowUpRight size={13} aria-hidden="true"/></div><p>{item.text}</p><span className="news-post-source">Open post in Telegram</span></a></li>)}</ol>:busy?<div className="news-loading" role="status"><span/><span/><span/><p>Loading the latest channel posts…</p></div>:<div className="news-empty"><MessageSquareText size={29}/><h3>{message?'Waiting for a connection':'No posts available yet'}</h3><p>New posts will appear here when the channel preview is available.</p></div>}
  </div>
  <footer className="news-window-footer"><span>{feed?.last_checked_at?`Last checked ${stamp(feed.last_checked_at)} SGT`:'No successful source check yet'}</span><span>Public channel · checks every 10s</span></footer>
 </section>;
}

export default function NewsWidget(){
 const [open,setOpen]=useState(false);const [feed,setFeed]=useState<NewsFeed|null>(null);const [busy,setBusy]=useState(false);const [error,setError]=useState('');const [unread,setUnread]=useState(0);
 const openRef=useRef(false);const inFlight=useRef(false);const lastAttempt=useRef(0);const highestId=useRef<number|null>(null);const request=useRef<AbortController|null>(null);const triggerRef=useRef<HTMLButtonElement>(null);const closeRef=useRef<HTMLButtonElement>(null);
 const load=useCallback(async()=>{
  if(inFlight.current)return;
  const controller=new AbortController();request.current=controller;inFlight.current=true;lastAttempt.current=Date.now();setBusy(true);
  let timedOut=false;const timeout=window.setTimeout(()=>{timedOut=true;controller.abort();},15_000);
  try{
   const result=await api<NewsFeed>('/news/telegram',{signal:controller.signal,cache:'no-store'});
   if(request.current!==controller)return;
   if(!Array.isArray(result.items)||!['ready','stale','unavailable','disabled'].includes(result.status)||result.items.some(item=>!Number.isSafeInteger(item.id)||item.id<=0||typeof item.text!=='string'||!Number.isFinite(Date.parse(item.published_at))))throw new Error('The news server returned an invalid feed.');
   const count=newPostCount(highestId.current,result.items);
   if(!openRef.current&&count)setUnread(value=>Math.min(999,value+count));
   if(result.items.length)highestId.current=Math.max(highestId.current??0,...result.items.map(item=>item.id));
   setFeed(result);setError('');
  }catch(e){if(request.current===controller&&(!controller.signal.aborted||timedOut))setError(timedOut?'The news request timed out. The next check will retry.':e instanceof Error?e.message:'The news connection failed. The next check will retry.');}
  finally{window.clearTimeout(timeout);if(request.current===controller){inFlight.current=false;setBusy(false);}}
 },[]);
 useEffect(()=>{
  const stop=watchNewsRefresh(()=>void load(),()=>!inFlight.current,()=>lastAttempt.current);
  return()=>{stop();request.current?.abort();request.current=null;inFlight.current=false;};
 },[load]);
 const close=useCallback(()=>{openRef.current=false;setOpen(false);triggerRef.current?.focus();},[]);
 useEffect(()=>{
  if(!open)return;
  closeRef.current?.focus();
  const keys=(event:KeyboardEvent)=>{if(event.key==='Escape'&&!document.querySelector('[aria-modal="true"]')){event.preventDefault();close();}};
  window.addEventListener('keydown',keys);return()=>window.removeEventListener('keydown',keys);
 },[open,close]);
 const toggle=()=>{
  if(open){close();return;}
  openRef.current=true;setUnread(0);setOpen(true);
  if(!document.hidden&&Date.now()-lastAttempt.current>=10_000)void load();
 };
 const connected=isFresh(feed)&&!error;
 return <aside className="news-widget" aria-label="Market news">
  {open&&<NewsWindow feed={feed} busy={busy} error={error} onClose={close} onRetry={()=>void load()} closeRef={closeRef}/>}
  <button ref={triggerRef} className={`news-launcher ${open?'active':''}`} type="button" aria-controls="market-news-window" aria-expanded={open} aria-label={`${open?'Close':'Open'} market news${unread?`, ${unread} unread posts`:''}`} onClick={toggle}><MessageSquareText size={20}/><span>News</span><i className={connected?'connected':''} aria-hidden="true"/>{unread>0&&<span className="news-unread" aria-hidden="true">{unread>99?'99+':unread}</span>}</button>
  <span className="sr-only" role="status">{unread>0?`${unread} new market news ${unread===1?'post':'posts'}`:''}</span>
 </aside>;
}
