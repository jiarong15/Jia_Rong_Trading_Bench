import {expect,test,type Page} from '@playwright/test';

const quota={plan:'basic',minute_limit:8,minute_remaining:3,minute_used:5,daily_limit:800,daily_used_observed:10,daily_remaining_observed:790,retry_after_seconds:0,note:'Observed account usage.'};
async function mockFx(page:Page,partial=false){
 await page.addInitScript(()=>localStorage.setItem('jr-trading-workbench-fx-pairs',JSON.stringify(['EUR/USD'])));
 await page.route('**/api/fx/market?*',route=>{
  const pairs=new URL(route.request().url()).searchParams.get('pairs')!.split(',');
  return route.fulfill({json:{source:'Twelve Data',checked_at:'2026-10-24T17:00:00Z',quota,coverage:'Indicative reference rates',portfolio_note:'Independent demo portfolio',
   rows:pairs.map(symbol=>({symbol,name:symbol,base:symbol.slice(0,3),quote:symbol.slice(4),price:partial?null:1.25,change_pct:.5,high:1.26,low:1.24,quote_time:'2026-10-24T17:00:00Z',market_open:true,stale:false,error:partial?'Twelve Data credit budget reached. Retry after reset.':null}))}});
 });
 await page.route('**/api/fx/history?*',route=>{
  if(partial)return route.fulfill({status:429,json:{detail:'Twelve Data credit budget reached. Retry after reset.'}});
  const params=new URL(route.request().url()).searchParams;
  return route.fulfill({json:{source:'Twelve Data',symbol:params.get('pair'),interval:params.get('interval'),bars:[],quota,fetched_at:'2026-10-24T17:00:00Z',analytics_as_of:'2026-10-22',methodology:'Completed daily bars',analytics:{completed_observations:100,return_1m_pct:1.5,volatility_20d_pct:5,mean_50d:1.2,distance_50d_pct:2}}});
 });
 await page.route('**/api/fx/search?*',route=>route.fulfill({json:{total:1,universe_count:1399,results:[{symbol:'EUR/SGD',base:'EUR',quote:'SGD',name:'Euro / Singapore Dollar',group:'Exotic-Cross'}],offset:0,limit:25}}));
}

test('FX pair discovery loads a new live pair and converter supports both directions',async({page})=>{
 await mockFx(page);await page.goto('/markets/fx');
 await expect(page.getByRole('heading',{name:'FX universe'})).toBeVisible();
 await page.getByRole('textbox',{name:'Search Twelve Data FX pairs'}).fill('Singapore');
 await page.getByRole('button',{name:'Load EUR/SGD Euro / Singapore Dollar'}).click();
 await expect(page.getByRole('heading',{name:'EUR/SGD · exchange-rate history'})).toBeVisible();
 await page.getByRole('spinbutton',{name:'FX conversion amount'}).fill('1000');
 await expect(page.locator('.fx-converted strong')).toContainText('1,250.00');
 await page.getByRole('button',{name:'Swap currencies'}).click();
 await expect(page.locator('.fx-converted strong')).toContainText('800.00');
 await page.getByRole('button',{name:'1D',exact:true}).click();
 await expect(page.getByText('UTC intraday candles',{exact:false})).toBeVisible();
});

test('FX quota failures remain explicit without synthetic quotes',async({page})=>{
 await mockFx(page,true);await page.goto('/markets/fx');
 await expect(page.getByText('History unavailable',{exact:true})).toBeVisible();
 await expect(page.getByText('Twelve Data credit budget reached.',{exact:false}).first()).toBeVisible();
 await expect(page.locator('.fx-converted strong')).toContainText('Unavailable');
 await expect(page.locator('.metric-value').first()).toContainText('Unavailable');
 await expect(page.getByRole('checkbox',{name:'Auto-refresh'})).toBeChecked();
 await page.getByRole('checkbox',{name:'Auto-refresh'}).uncheck();
 await expect(page.getByText('Indicative reference rates · manual refresh',{exact:true})).toBeVisible();
});

test('FX identifies Yahoo quotes and whole-series Twelve Data fallback',async({page})=>{
 await mockFx(page);
 await page.route('**/api/fx/market?*',route=>route.fulfill({json:{source:'Yahoo Finance via yfinance',primary_provider:'yahoo',checked_at:'2026-10-24T17:00:00Z',
  quota:{...quota,initialized:false},providers:[{id:'yahoo',name:'Yahoo Finance via yfinance',role:'primary',state:'ready',last_error:null,retry_after_seconds:0}],
  coverage:'Yahoo may be delayed. Each row identifies its source.',portfolio_note:'Independent demo portfolio',
  rows:[{symbol:'EUR/USD',name:'EUR/USD',base:'EUR',quote:'USD',price:1.25,change_pct:.5,quote_time:'2026-10-24T17:00:00Z',market_open:false,stale:false,
   source:'Yahoo Finance via yfinance',provider:'yahoo',error:null,fallback:false}]}}));
 await page.route('**/api/fx/history?*',route=>route.fulfill({json:{source:'Twelve Data',provider:'twelvedata',fallback:true,fallback_reason:'Yahoo returned invalid FX candle prices.',
  symbol:'EUR/USD',interval:'1day',bars:[],quota,analytics:null,analytics_as_of:null,fetched_at:'2026-10-24T17:00:00Z',methodology:'Whole provider series.'}}));
 await page.goto('/markets/fx');
 await expect(page.locator('.equity-source-banner strong')).toHaveText('Yahoo Finance via yfinance');
 await expect(page.getByText('Using backup history from Twelve Data.',{exact:false})).toBeVisible();
 await expect(page.locator('.fx-fallback-note')).toContainText('invalid FX candle prices');
 await expect(page.getByRole('cell',{name:'Yahoo Finance via yfinance',exact:false})).toBeVisible();
});
