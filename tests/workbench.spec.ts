import {expect,test} from '@playwright/test';

test('all asset routes, instrument provenance, watchlists and responsive overview',async({page})=>{
 await page.goto('/');await expect(page.getByRole('heading',{name:'Your market, in perspective.'})).toBeVisible();
 await page.screenshot({path:'test-results/overview-desktop.png',fullPage:false});
 for(const asset of ['Bonds','Rates','Commodities']){
   await page.locator('nav').getByRole('link',{name:new RegExp('^'+asset)}).click();
   await expect(page.getByRole('heading',{name:asset+'.',exact:true})).toBeVisible();
   await expect(page.getByRole('heading',{name:asset+' universe'})).toBeVisible();
 }
 await page.getByRole('textbox',{name:'Search demo workspace instruments'}).fill('EURUSD');
 await page.locator('.search-results').getByRole('button').first().click();
 await expect(page.getByRole('dialog')).toBeVisible();await expect(page.getByText('Quote convention',{exact:true})).toBeVisible();
 await page.getByRole('button',{name:'Close instrument details'}).click();
 await page.locator('nav').getByRole('link',{name:'Overview',exact:true}).click();
 await page.getByRole('button',{name:/Watch JPM|Unwatch JPM/}).click();
 await page.reload();await expect(page.getByRole('button',{name:/Watch JPM|Unwatch JPM/})).toBeVisible();
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:'test-results/overview-mobile.png',fullPage:false});
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
 await page.getByRole('button',{name:'Open navigation'}).click();await page.locator('nav').getByRole('link',{name:'Rates',exact:false}).click();await expect(page.getByRole('heading',{name:'Rates.',exact:true})).toBeVisible();
});

test('research can become a reviewed paper fill',async({page})=>{
 await page.goto('/research?asset=equities&instrument=AAPL');
 await page.getByRole('button',{name:'Run research',exact:true}).click();
 await expect(page.getByRole('heading',{name:'Evidence-backed signals'})).toBeVisible();
 await page.getByRole('button',{name:'Propose paper order'}).first().click();
 await expect(page.getByRole('heading',{name:'Portfolio & risk.'})).toBeVisible();
 await page.getByRole('button',{name:/^Orders/}).click();
 await page.getByRole('button',{name:'Approve paper fill'}).first().click();
 await expect(page.getByRole('status')).toContainText('Paper fill recorded');
 await expect(page.locator('.order-card .badge').filter({hasText:'filled'}).first()).toBeVisible();
 await page.getByRole('button',{name:'Halt execution'}).click();await expect(page.getByText('Paper execution halted',{exact:true})).toBeVisible();
 await page.getByRole('button',{name:'Resume paper'}).click();await expect(page.getByText('Paper mandate active',{exact:true})).toBeVisible();
});

test('backtest, validation, exports and mandate save',async({page})=>{
 await page.goto('/backtests');await page.getByRole('button',{name:'Run backtest'}).click();
 await expect(page.getByRole('heading',{name:'AAPL · historical replay'})).toBeVisible();
 await expect(page.getByText('Maximum drawdown',{exact:true})).toBeVisible();
 const [download]=await Promise.all([page.waitForEvent('download'),page.getByRole('button',{name:'Export backtest'}).click()]);
 expect(download.suggestedFilename()).toMatch(/^backtest-.*\.json$/);
 await page.goto('/data');await page.getByRole('button',{name:'Validate data'}).click();await expect(page.locator('.validation-success')).toContainText('30 unique identifiers');
 await page.goto('/settings');await page.getByRole('spinbutton',{name:'Maximum position weight'}).fill('30');await page.getByRole('button',{name:'Save mandate'}).click();await expect(page.getByRole('status')).toContainText('Mandate limits saved');
});
