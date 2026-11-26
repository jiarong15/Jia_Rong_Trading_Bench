import {expect,test,type Page} from '@playwright/test';

const doc={source:'SEC EDGAR 10-K: https://www.sec.gov/Archives/test.htm',period_end:'2025-12-31',filed_at:'2026-02-19',prior_period_end:'2024-12-31',prior_filed_at:'2026-02-19',current:{net_income:3333.751},prior:{net_income:1579.776}};
const report={status:'loaded',message:'SEC 10-K retrieved.',company:'AppLovin Corp',cik:'0001751008',fetched_at:'2026-10-24T20:00:00Z',
 filing:{form:'10-K',accession:'0001751008-26-000010',filed_at:'2026-02-19',period_end:'2025-12-31',url:'https://www.sec.gov/Archives/test.htm'},
 document:doc,download_available:true,warnings:['Issuance evidence requires review.'],provenance:{'current.net_income':{value:3333.751,tag:'us-gaap:NetIncomeLoss',end:'2025-12-31',accession:'0001751008-26-000010',url:'https://www.sec.gov/Archives/test.htm'}}};
async function mockMarket(page:Page){
 await page.addInitScript(()=>localStorage.setItem('jr-trading-workbench-equity-symbols',JSON.stringify(['APP'])));
 await page.route('**/api/equities/financials/*',route=>route.fulfill({json:{document:null,origin:null}}));
 await page.route('**/api/equities/market?*',route=>route.fulfill({json:{feed:'iex',coverage:'IEX only',fetched_at:'2026-10-24T20:00:00Z',
  clock:{is_open:false,next_open:'2026-10-26T13:30:00Z'},history:{},papers:[],rows:[{symbol:'APP',name:'AppLovin',kind:'Stock',price:196.74,
   change_pct:1,trade_time:'2026-10-23T20:00:00Z',stale:false,financials:{score:null,coverage:0,tests:[],book_to_market:null},value_momentum_percentile:null}]}}));
}

test('selected company imports SEC annual inputs and exposes its original filing and provenance',async({page})=>{
 await mockMarket(page);
 await page.route('**/api/equities/reports/*/sync*',route=>route.fulfill({json:{document:doc,origin:'sec',sec_report:report}}));
 await page.goto('/markets/equities');
 await expect(page.getByText('SEC 10-K loaded',{exact:true})).toBeVisible();
 await expect(page.getByRole('spinbutton',{name:'current Net income before extraordinary items'})).toHaveValue('3333.751');
 await expect(page.getByRole('link',{name:'Open original 10-K'})).toHaveAttribute('href',report.filing.url);
 await expect(page.getByRole('link',{name:'Download 10-K'})).toHaveAttribute('href','/api/equities/sec/APP/document');
 await page.getByText('Imported figures and source tags (1)',{exact:true}).click();
 await expect(page.getByText('us-gaap:NetIncomeLoss',{exact:true})).toBeVisible();
});

test('a late SEC response preserves unsaved worksheet edits',async({page})=>{
 await mockMarket(page);
 let release!:()=>void;
 const ready=new Promise<void>(resolve=>{release=resolve;});
 await page.route('**/api/equities/reports/*/sync*',async route=>{await ready;await route.fulfill({json:{document:doc,origin:'sec',sec_report:report}});});
 await page.goto('/markets/equities');
 const source=page.getByRole('textbox',{name:'Source / filing reference'});
 await source.fill('My reviewed annual report');
 release();
 await expect(page.getByRole('link',{name:'Open original 10-K'})).toBeVisible();
 await expect(source).toHaveValue('My reviewed annual report');
 await expect(page.getByText('Your unsaved worksheet edits are retained;', {exact:false})).toBeVisible();
});

test('missing SEC report exposes Nasdaq subscription failure and an explicit retry',async({page})=>{
 await mockMarket(page);
 await page.route('**/api/equities/reports/*/sync*',route=>route.fulfill({json:{document:null,origin:null,
  sec_report:{status:'unavailable',document:null,message:'No SEC company CIK mapping was found for APP.'},
  nasdaq_report:{status:'subscription_required',code:'QEPx04',annual_tables:[],message:'Nasdaq NDAQ/RD: subscription required (HTTP 403, QEPx04).'}}}));
 await page.goto('/markets/equities');
 await expect(page.getByText('Nasdaq Data Link fallback',{exact:true})).toBeVisible();
 await expect(page.getByRole('status').filter({hasText:'QEPx04'})).toBeVisible();
 await expect(page.getByRole('button',{name:'Reload annual reports'})).toBeEnabled();
 await expect(page.getByRole('link',{name:'Download 10-K'})).toHaveCount(0);
});
