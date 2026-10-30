import {expect,test} from '@playwright/test';

test('company search loads a stock beyond the default comparison, then removes it',async({page})=>{
 const names:Record<string,string>={AAPL:'Apple Inc.',PLTR:'Palantir Technologies Inc.'};
 await page.route('**/api/equities/financials/*',route=>route.fulfill({json:{document:null}}));
 await page.route('**/api/equities/sec/*/sync*',route=>route.fulfill({json:{document:null,origin:null,sec_report:{status:'unavailable',message:'No SEC test filing',document:null}}}));
 await page.route('**/api/equities/market?*',route=>{
  const symbols=new URL(route.request().url()).searchParams.get('symbols')!.split(',');
  return route.fulfill({json:{source:'Alpaca Market Data',feed:'iex',coverage:'IEX only',fetched_at:'2026-10-23T20:00:00Z',
   clock:{is_open:false,next_open:'2026-10-26T13:30:00Z'},history:{},papers:[],blend_note:'Comparison subset',history_note:'Completed bars',
   rows:symbols.map(symbol=>({symbol,name:names[symbol]??symbol,kind:'Stock',price:110,change_pct:1,trade_time:'2026-10-23T20:00:00Z',stale:false,
    momentum_12_1:20,momentum_6_1:10,volatility_annual_pct:25,value_momentum_percentile:null,
    financials:{score:null,coverage:0,tests:[],book_to_market:null}}))}});
 });
 await page.route('**/api/equities/search?*',route=>{
  const q=new URL(route.request().url()).searchParams.get('q')??'';
  const results=/palantir|pltr/i.test(q)?[{symbol:'PLTR',name:names.PLTR,exchange:'NASDAQ',tradable:true}]:[];
  return route.fulfill({json:{results,total:results.length,universe_count:14000,offset:0,limit:25}});
 });
 await page.goto('/markets/equities');
 const search=page.getByRole('textbox',{name:'Search Alpaca stock universe'});
 await search.fill('Palantir');
 await page.getByRole('button',{name:'Load PLTR Palantir Technologies Inc.'}).click();
 await expect(page.getByRole('heading',{name:'PLTR · research workbench'})).toBeVisible();
 await expect(page.getByRole('button',{name:'Remove PLTR from comparison'})).toBeVisible();
 await expect(search).toHaveValue('');
 await page.getByRole('button',{name:'Remove PLTR from comparison'}).click();
 await expect(page.getByRole('button',{name:'Remove PLTR from comparison'})).toHaveCount(0);
 await search.fill('nonexistent');
 await expect(page.getByText('No Alpaca matches.',{exact:false})).toBeVisible();
});

test('search errors are visible and retry uses Alpaca search again',async({page})=>{
 let attempts=0;
 await page.route('**/api/equities/financials/*',route=>route.fulfill({json:{document:null}}));
 await page.route('**/api/equities/sec/*/sync*',route=>route.fulfill({json:{document:null,origin:null,sec_report:{status:'unavailable',message:'No SEC test filing',document:null}}}));
 await page.route('**/api/equities/market?*',route=>route.fulfill({json:{feed:'iex',coverage:'IEX only',fetched_at:'2026-10-23T20:00:00Z',
  clock:{is_open:false,next_open:'2026-10-26T13:30:00Z'},rows:[],history:{},papers:[]}}));
 await page.route('**/api/equities/search?*',route=>{
  attempts++;
  return attempts===1?route.fulfill({status:503,json:{detail:'Alpaca directory unavailable'}}):route.fulfill({json:{results:[],total:0,universe_count:14000,offset:0,limit:25}});
 });
 await page.goto('/markets/equities');
 await page.getByRole('textbox',{name:'Search Alpaca stock universe'}).fill('PLTR');
 await expect(page.getByRole('alert')).toContainText('Alpaca directory unavailable');
 await page.getByRole('button',{name:'Retry search'}).click();
 await expect(page.getByText('No Alpaca matches.',{exact:false})).toBeVisible();
 expect(attempts).toBe(2);
});
