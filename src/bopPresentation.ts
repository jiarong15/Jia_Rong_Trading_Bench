export function bopPresentation(country:string,rolling:boolean){
 const periods=rolling?'four complete quarters':'one complete quarter';
 const flow=rolling?'summed over four consecutive quarters':'for the quarter';
 if(country==='CAN')return {
  amountUnit:'CAD bn',flowAdjustment:'NSA',tradeAdjustment:'NSA',nationality:'Canadian',
  currencyName:'trade-weighted Canadian dollar',indexName:'BIS nominal effective CAD index',indexKey:'currency_index',
  indexSubtitle:'BIS nominal effective CAD index',
  accountNote:`Left: Statistics Canada current account / matching unadjusted current-price expenditure GDP, using ${periods} (NSA). GDP is an actual quarterly amount; the annual-rate GDP series is not used and no division by four is applied. Right: average of ${rolling?'twelve':'three'} complete monthly BIS broad nominal CAD indices (2020 = 100). Higher means stronger CAD. This BIS basket differs from the Bank of Canada CEER basket.`,
  reserveNote:`Bars: Statistics Canada net reserve-asset acquisition, ${flow} (NSA). Positive means acquisition, negative disposal; valuation changes are excluded. Line: quarter-end CAD official international reserves including monetary gold where held / all-currency short-term external debt at market value (original maturity of one year or less). Intercompany debt without a maturity split is excluded. Stocks are never summed.`,
  tradeNote:'Statistics Canada monthly BOP goods plus services trade (NSA, CAD), combined only when both sources have observations. This source'
 };
 if(country==='CHE')return {
  amountUnit:'CHF bn',flowAdjustment:'NSA',tradeAdjustment:'SA and working-day adjusted · goods only · total 1',nationality:'Swiss',
  currencyName:'trade-weighted Swiss franc',indexName:'BIS nominal effective CHF index',indexKey:'currency_index',
  indexSubtitle:'BIS nominal effective CHF index',
  accountNote:`Left: SNB current account / matching SECO current-price GDP, using ${periods} (NSA). GDP is the actual quarter, without sports-event adjustment; it is not divided by four. Right: average of ${rolling?'twelve':'three'} complete monthly BIS broad nominal effective CHF indices (2020 = 100). Higher means stronger CHF. This BIS basket is separate from the SNB effective-franc indices.`,
  reserveNote:`Bars: SNB net reserve-asset acquisition, ${flow} (NSA). Positive means acquisition, negative disposal; valuation changes are excluded. Line: quarter-end CHF IIP reserve assets including gold / sector short-term external debt (original maturity). Debt covers government, central bank, banks and other sectors; intercompany debt has no maturity split and is excluded. Stocks are never summed.`,
  tradeNote:'FOCBS monthly business-cycle merchandise trade via SNB (total 1, seasonally and working-day adjusted, goods only). It excludes gold bars and other precious metals, coins, precious stones and gems, works of art and antiques; this differs from the broad customs comparison above. This source'
 };
 if(country==='NZL')return {
  amountUnit:'NZD bn',flowAdjustment:'NSA',tradeAdjustment:'NSA · goods only',nationality:'New Zealand',
  currencyName:'trade-weighted New Zealand dollar',indexName:'BIS nominal effective NZD index',indexKey:'currency_index',
  indexSubtitle:'BIS nominal effective NZD index',
  accountNote:`Left: Stats NZ actual current account / matching current-price expenditure GDP, using ${periods} (NSA). GDP is an actual quarterly amount; it is not divided by four. Ratios use the current GDP release; historical BOP-published ratios can use another vintage. Right: average of ${rolling?'twelve':'three'} complete monthly BIS broad nominal effective NZD indices (2020 = 100). Higher means stronger NZD. This BIS basket is separate from the RBNZ trade-weighted index.`,
  reserveNote:`Bars: Stats NZ net reserve-asset acquisition, ${flow} (NSA). Positive means acquisition, negative disposal; valuation changes are excluded. Line: quarter-end NZD IIP reserve assets / published sector short-term external debt. Debt covers government, central bank, deposit-taking corporations and other sectors. Direct-investment intercompany debt has no term split and is excluded. Stocks are never summed. Residual-maturity non-equity liabilities are not substituted.`,
  tradeNote:'Stats NZ Infoshare monthly merchandise trade (NSA, goods only, exports including re-exports FOB and imports CIF)'
 };
 if(country==='AUS')return {
  amountUnit:'AUD bn',flowAdjustment:'NSA',tradeAdjustment:'SA · goods only',nationality:'Australian',
  currencyName:'trade-weighted Australian dollar',indexName:'BIS nominal effective AUD index',indexKey:'currency_index',
  indexSubtitle:'BIS nominal effective AUD index',
  accountNote:`Left: ABS seasonally adjusted current account / matching nominal GDP, using ${periods}. GDP is an actual quarterly amount; it is not divided by four. Right: average of ${rolling?'twelve':'three'} complete monthly BIS broad nominal effective AUD indices (2020 = 100, NSA). Higher means stronger AUD. This is the BIS basket, separate from the RBA trade-weighted index.`,
  reserveNote:`Bars: ABS net reserve-asset acquisition, ${flow} (NSA). Native ABS cash-flow signs are reversed: positive means acquisition, negative disposal. Valuation changes are excluded. Line: quarter-end AUD reserve assets including gold / published sector short-term external debt. Debt covers government, monetary authorities, depository corporations, other financial corporations and other sectors; intercompany debt has no maturity split and is excluded. Stocks are never summed.`,
  tradeNote:'ABS monthly BOP goods trade (SA, goods only, including non-monetary gold)'
 };
 if(country==='SGP')return {
  amountUnit:'SGD bn',flowAdjustment:'NSA',tradeAdjustment:'NSA · goods only',nationality:'Singapore',
  currencyName:'trade-weighted Singapore dollar',indexName:'BIS nominal effective SGD index',indexKey:'currency_index',
  indexSubtitle:'BIS nominal effective SGD index',
  accountNote:`Left: SingStat current account / matching nominal GDP, using ${periods} (NSA). GDP is an actual quarterly amount; it is not divided by four. Right: average of ${rolling?'twelve':'three'} complete monthly BIS broad nominal effective SGD indices (2020 = 100). Higher means stronger SGD. This BIS basket differs from MAS' monetary-policy S$NEER.`,
  reserveNote:`Bars: DOS BOP net reserve-asset acquisition, ${flow} (NSA). Positive means acquisition, negative disposal; valuation changes are excluded. Line: MAS quarter-end SGD official foreign reserves, including gold / published sector short-term external debt. Debt covers central bank, deposit-taking corporations and other sectors; government is included only when its published total is zero. Intercompany loans lack a maturity split and are excluded. Stocks are never summed. Before May 1999, reserve stocks used book cost.`,
  tradeNote:'Enterprise Singapore monthly merchandise trade via SingStat (NSA, goods only, including re-exports)'
 };
 if(country==='GBR')return {
  amountUnit:'GBP bn',flowAdjustment:'NSA',tradeAdjustment:'SA',nationality:'UK',
  currencyName:'trade-weighted sterling',indexName:'Broad sterling index',indexKey:'currency_index',
  indexSubtitle:'broad sterling index',
  accountNote:`Left: ONS seasonally adjusted current account including precious metals / matching nominal GDP, using ${periods}. UK GDP is an actual quarterly amount; it is not divided by four. Right: ${rolling?'average of four completed quarterly averages':'quarterly average'} of the Bank of England broad effective sterling index (Jan 2005 = 100, NSA). Higher means stronger sterling.`,
  reserveNote:`Bars: ONS net reserve-asset acquisition, ${flow} (NSA), excluding valuation changes. The UK cash-flow sign is reversed: positive means acquisition, negative disposal. Line: quarter-end ONS GBP reserve assets including gold / sector short-term external debt (original maturity). Debt covers monetary authorities, banks, government and other sectors; intercompany lending has no maturity split and is excluded. Stocks are never summed.`,
  tradeNote:'ONS monthly UK goods-and-services trade, including precious metals (SA)'
 };
 if(country==='CHN')return {
  amountUnit:'USD bn',flowAdjustment:'NSA',tradeAdjustment:'NSA',nationality:'Chinese',
  currencyName:'trade-weighted Chinese renminbi',indexName:'Nominal effective renminbi index',indexKey:'currency_index',
  indexSubtitle:'nominal effective renminbi index',
  accountNote:`Left: SAFE's published renminbi current account / matching NBS nominal GDP, using ${periods} (NSA). GDP is an actual quarterly amount; it is not divided by four. Other amount charts use SAFE's published USD figures. Right: average of ${rolling?'twelve':'three'} complete monthly BIS nominal effective renminbi indices (2020 = 100). Higher means a stronger renminbi.`,
  reserveNote:`Bars: net reserve-asset acquisition from SAFE, ${flow} (NSA). SAFE's outflow sign is reversed: positive means acquisition, negative disposal. Line: quarter-end SAFE total reserve assets including gold / sector short-term external debt (original maturity). The denominator covers government, central bank, banks and other sectors; intercompany lending has no maturity split and is excluded. Both stocks are in USD and are never summed.`,
  tradeNote:'SAFE monthly BOP goods-and-services credits and debits (NSA)'
 };
 if(country==='JPN')return {
  amountUnit:'JPY bn',flowAdjustment:'NSA',tradeAdjustment:'SA',nationality:'Japanese',
  currencyName:'trade-weighted Japanese yen',indexName:'Nominal effective yen index',indexKey:'currency_index',
  indexSubtitle:'nominal effective yen index',
  accountNote:`Left: separately published BOJ seasonally adjusted current account / matching Cabinet Office nominal GDP; annual GDP rates are divided by four. Uses ${periods}. The unadjusted building-block chart can differ. Right: average of ${rolling?'twelve':'three'} complete monthly nominal effective yen indices (2020 = 100). Higher means a stronger yen; co-movement does not establish causation.`,
  reserveNote:`Bars: BOJ net reserve-asset acquisition, ${flow} (NSA), excluding valuation changes. Positive means acquisition, negative disposal. Line: quarter-end BOJ yen reserve assets / short-term external debt (original maturity) of central bank, banks, government and other sectors. Intercompany lending has no maturity split and is excluded. Stocks are never summed; this is a different debt definition from the US chart.`,
  tradeNote:'Monthly goods-and-services trade'
 };
 return {
  amountUnit:'USD bn',flowAdjustment:'SA',tradeAdjustment:'SA',nationality:'US',
  currencyName:'trade-weighted US dollar',indexName:'Broad dollar index',indexKey:'broad_dollar',
  indexSubtitle:'broad dollar index',
  accountNote:`Left: ${rolling?'four-quarter':'single-quarter'} current-account balance / GDP for the same period (SA). GDP annual rates are converted to quarterly amounts. Right: average daily broad dollar index over ${rolling?'four complete quarters':'the quarter'} (NSA; Jan 2006 = 100). A higher left value means a smaller deficit; a higher right value means a stronger dollar.`,
  reserveNote:`Bars (left): net reserve-asset acquisition, ${flow} (SA), excluding valuation changes. Positive means acquisition; negative means disposal. Line (right) remains a quarter-end stock ratio in both modes: reserves / debt principal due within one year (remaining maturity, NSA). Interest is excluded; reserves include gold at market value. Treasury scheduled-payment coverage applies.`,
  tradeNote:'Monthly goods-and-services trade'
 };
}
