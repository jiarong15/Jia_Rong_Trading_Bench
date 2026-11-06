import {expect,test} from '@playwright/test';

test('Tradays iframe settings, result updates, stacking, reconnect and route persistence',async({page,context})=>{
 let loads=0,oldApiCalls=0;
 const today=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Singapore',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
 const dates=[-1,0,1].map(offset=>new Date(Date.parse(today+'T12:00:00+08:00')+offset*86400000).toISOString().slice(0,10));
 await page.route('**/api/calendar/tradays/focus',route=>route.fulfill({json:{status:'ready',today,targets:Object.fromEntries(dates.map((date,index)=>[date,`prevValue${index+100}`])),error:null}}));
 await page.route('**/api/calendar/economic',route=>{oldApiCalls++;return route.abort();});
 await page.route('https://www.tradays.com/en/economic-calendar/widget?*',route=>{
  loads++;
  const url=new URL(route.request().url());
  expect(url.searchParams.get('importance')).toBe('8');expect(url.searchParams.get('date_mode')).toBe('0');
  return route.fulfill({contentType:'text/html',body:'<!doctype html><html><body><h2>SYNTHETIC TRADAYS FRAME</h2><p id="actual">Pending</p><script>setTimeout(()=>document.getElementById("actual").textContent="TEST ACTUAL 0.3%",500);</script></body></html>'});
 });
 await page.route('**/api/news/telegram',route=>route.fulfill({json:{channel:{username:'marketfeed',title:'Market News Feed',url:'https://t.me/marketfeed'},status:'ready',items:[{id:1,text:'TEST NEWS',published_at:new Date().toISOString()}],last_checked_at:new Date().toISOString(),error:null,poll_interval_seconds:10}}));
 await page.goto('/');await page.getByRole('button',{name:'Open economic calendar'}).click();
 const calendar=page.getByRole('dialog',{name:'Economic releases'}),frame=page.frameLocator('.tradays-frame');
 await expect(calendar.getByText('Tradays · automatic release updates')).toBeVisible();
 await expect(frame.getByText('TEST ACTUAL 0.3%')).toBeVisible();
 expect(loads).toBe(1);expect(oldApiCalls).toBe(0);
 await expect(calendar.getByText('Yesterday',{exact:true})).toBeVisible();
 await expect(calendar.getByText('Tomorrow',{exact:true})).toBeVisible();
 const widget=page.locator('.tradays-frame');
 await widget.evaluate(element=>element.setAttribute('data-test-identity','original-frame'));
 const originalUrl=new URL((await widget.getAttribute('src'))!);
 for(const [index,label] of ['Yesterday','Today','Tomorrow'].entries()){
  await calendar.getByRole('button',{name:new RegExp(label)}).click();
  await expect(widget).toHaveAttribute('src',new RegExp(`#prevValue${index+100}$`));
  await expect(calendar.getByRole('button',{name:new RegExp(label)})).toHaveAttribute('aria-pressed','true');
  const focusedUrl=new URL((await widget.getAttribute('src'))!);
  expect(focusedUrl.search).toBe(originalUrl.search);
  await expect(widget).toHaveAttribute('data-test-identity','original-frame');
 }
 expect(loads).toBe(1);
 await page.getByRole('button',{name:'Open market news',exact:true}).click();
 const news=page.getByRole('dialog',{name:'Market news'});
 await expect(news).toBeVisible();
 const calendarBox=await calendar.boundingBox(),newsBox=await news.boundingBox();
 expect(calendarBox!.y+calendarBox!.height).toBeLessThan(newsBox!.y);
 await page.screenshot({path:'test-results/tradays-desktop.png'});
 await page.setViewportSize({width:390,height:844});
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
 await expect(calendar.getByRole('button',{name:'Close economic calendar'})).toBeVisible();
 await page.screenshot({path:'test-results/tradays-mobile.png'});
 await page.setViewportSize({width:1440,height:1050});
 await page.locator('nav').getByRole('link',{name:'Rates',exact:false}).click();
 await expect(calendar).toBeVisible();expect(loads).toBe(1);
 await calendar.getByRole('button',{name:'Reload Tradays calendar'}).click();
 await expect.poll(()=>loads).toBe(2);
 await context.setOffline(true);
 await expect(calendar.getByText('Calendar needs an internet connection')).toBeVisible();
 await context.setOffline(false);
 await expect.poll(()=>loads).toBe(3);
 await expect(calendar.getByText('Tradays · automatic release updates')).toBeVisible();
 await calendar.getByRole('button',{name:'Close economic calendar'}).click();
 await expect(page.locator('.tradays-frame')).toHaveCount(0);
 await expect(page.getByRole('button',{name:'Open economic calendar'})).toBeFocused();
});
