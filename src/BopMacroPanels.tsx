import {useState} from 'react';
import BopChart from './BopChart';
import type {ChartRow,DateZoom} from './BopChart';
import {filterBopRange} from './bopFilters';
import type {BopFrequency} from './bopFilters';
import {bopPresentation} from './bopPresentation';

export type MacroData={status:'ready'|'unavailable';country_code?:string;currency?:string;portfolio_split?:'combined';trade_scope?:'merchandise'|'bop_goods';frequency:BopFrequency;quarterly:ChartRow[];trade:ChartRow[];monthly:ChartRow[];warnings:string[];sources:{group:string;checked_at:string;url:string;latest_observation:string}[]};
const green='#388675',navy='#233a57',orange='#cf805f',purple='#8c78b1',blue='#6979b4';

export default function BopMacroPanels({data,range,frequency,endDate}:{data?:MacroData;range:string;frequency:BopFrequency;endDate?:string}){
 const [tradeZoom,setTradeZoom]=useState<DateZoom>(null);
 const quarters=filterBopRange(data?.quarterly??[],range,endDate);
 const trade=filterBopRange(data?.trade??[],range,endDate);
 const rolling=frequency==='trailing4q';
 const periodLabel=rolling?'Rolling 4Q':'Quarterly';
 const flowPeriod=rolling?'rolling 4-quarter sum':'quarterly flow';
 const context=bopPresentation(data?.country_code??'USA',rolling);
 const {amountUnit,flowAdjustment,currencyName,indexName}=context;
 const combinedPortfolio=data?.portfolio_split==='combined';
 const sectorDebt=['CHN','GBR','SGP','AUS','NZL','CHE'].includes(data?.country_code??'');
 const debtLabel=sectorDebt?'Reserves / sector short-term debt (%)':'Reserves / short-term debt (%)';
 const tradeLink={linkedZoom:tradeZoom,onDateZoom:setTradeZoom};
 const refreshWarnings=(data?.warnings??[]).filter(warning=>!warning.startsWith("Treasury's table linked for "));
 return <div className="bop-macro-panels">
  {(!data||data.status==='unavailable')&&<p className="bop-message error" role="status">Companion data is unavailable. Refresh data to check the official sources.</p>}
  {refreshWarnings.length? <div className="bop-message error" role="status">{refreshWarnings.map(warning=><p key={warning}>{warning}</p>)}</div>:null}
  <div className="bop-chart-grid bop-macro-grid">
   <BopChart normalizable title={`Current account / GDP & ${currencyName}`} subtitle={`${periodLabel} · left: current account / GDP (%) · right: ${context.indexSubtitle}`} rows={quarters} unit="%" digits={1} fitValues leftAxisLabel="Current account / GDP (%)" leftAxisColor={navy} rightAxis={{label:indexName,unit:'index points',digits:1,color:green}} plots={[
    {key:'current_account_gdp_pct',name:'Current account / GDP (left)',color:navy},
    {key:context.indexKey,name:`${indexName} (right)`,color:green,axis:'right'},
   ]} note={context.accountNote}/>
   <BopChart title="Net inward investment" subtitle={`${amountUnit} · ${flowPeriod} · ${flowAdjustment} · positive = net inward`} unit={amountUnit} rows={quarters} plots={combinedPortfolio?[{key:'net_direct',name:'Direct investment',color:green,bar:true},{key:'net_portfolio',name:'Portfolio investment (combined)',color:blue,bar:true},{key:'net_other',name:'Other investment / banking',color:orange,bar:true}]:[
    {key:'net_direct',name:'Direct investment',color:green,bar:true},{key:'net_equity',name:'Portfolio equity',color:purple,bar:true},
    {key:'net_debt',name:'Portfolio debt',color:blue,bar:true},{key:'net_other',name:'Other investment / banking',color:orange,bar:true},
   ]} note={`Each bar = ${context.nationality} liabilities incurred minus ${context.nationality} acquisition of foreign assets, ${rolling?'summed over four consecutive quarters':'for that quarter'}. ${combinedPortfolio?'Singapore publishes portfolio equity and debt combined quarterly; the equity/debt split is annual and is not interpolated.':'Portfolio equity includes investment fund shares;'} Other investment includes banking flows. ${data?.country_code==='CAN'?'Reserves are excluded. Statistics Canada does not separately identify derivatives in this table; the published other-investment totals are used.':'Reserves and derivatives are excluded.'}`}/>
   <BopChart normalizable title={sectorDebt?'Reserve-asset transactions & reserves / sector short-term debt':'Reserve-asset transactions & reserves / short-term debt'} subtitle={`${periodLabel} · left: reserve transactions (${amountUnit}) · right: ${debtLabel.toLowerCase()}`} unit={amountUnit} rows={quarters} digits={1} leftAxisLabel={`Reserve transactions (${amountUnit})`} leftAxisColor={blue} rightAxis={{label:debtLabel,unit:'%',digits:1,color:green}} plots={[
    {key:'reserve_transactions',name:'Reserve-asset transactions (left)',color:blue,bar:true},
    {key:'reserves_debt_pct',name:sectorDebt?'Reserves / sector short-term debt (right)':'Reserves / short-term debt (right)',color:green,axis:'right'},
   ]} note={context.reserveNote}/>
    <BopChart normalizable title="Exports, imports & trade balance" subtitle={`${amountUnit} · ${flowPeriod} · ${context.tradeAdjustment} · solid lines = 3Q average · bars = trade balance`} unit={amountUnit} rows={trade} leftAxisLabel={amountUnit} {...tradeLink} plots={[
     {key:'trade_balance',name:'Trade balance',color:navy,bar:true},
     {key:'exports',name:'Exports',color:green,dash:true},{key:'imports',name:'Imports',color:orange,dash:true},
     {key:'exports_ma3',name:'Exports · 3Q average',color:green},{key:'imports_ma3',name:'Imports · 3Q average',color:orange},
    ]} note={`${context.tradeNote} is aggregated into complete calendar quarters${rolling?', then summed over four consecutive quarters':''}. Dashed lines show ${rolling?'rolling four-quarter':'quarterly'} totals; solid lines average three consecutive plotted quarters. Bars show exports minus imports. Partial quarters are excluded; missing inputs remain gaps.`}/>
  </div>
  <div className="bop-aligned-panels bop-macro-growth">
   <BopChart title="Trade growth: year on year" subtitle={`${periodLabel} trade · % change from the same quarter a year earlier · ${context.tradeAdjustment} levels`} rows={trade} unit="%" digits={1} {...tradeLink} plots={[{key:'exports_yoy',name:'Exports YoY',color:green},{key:'imports_yoy',name:'Imports YoY',color:orange}]} note={`100 × (${rolling?'this rolling four-quarter total / the rolling four-quarter total ending a year earlier':'this quarter / the same quarter last year'} − 1). Growth is recalculated from the selected totals; monthly growth rates are never added or averaged.`}/>
  </div>
 </div>;
}
