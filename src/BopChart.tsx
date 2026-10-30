import {useCallback,useEffect,useMemo,useState} from 'react';
import {Bar,CartesianGrid,ComposedChart,Legend,Line,ReferenceLine,ResponsiveContainer,Tooltip,XAxis,YAxis} from 'recharts';
import {RotateCcw} from 'lucide-react';
import {Panel,number} from './components';
import BopZoomOverlay from './BopZoomOverlay';
import type {ZoomBounds} from './bopZoom';
import {normalizeRows} from './bopNormalize';
import type {NormalizableRow} from './bopNormalize';

export type ChartRow=NormalizableRow;
export type Plot={key:string;name:string;color:string;bar?:boolean;dash?:boolean;stack?:string;axis?:'left'|'right'};
export type DateZoom={from:string;to:string}|null;
type AxisSpec={label:string;unit:string;digits:number;color:string};
type Props={title:string;subtitle:string;rows:ChartRow[];plots:Plot[];note:string;stacked?:boolean;unit?:string;digits?:number;fitValues?:boolean;linkedZoom?:DateZoom;onDateZoom?:(value:DateZoom)=>void;leftAxisLabel?:string;leftAxisColor?:string;rightAxis?:AxisSpec;normalizable?:boolean};

export default function BopChart({title,subtitle,rows,plots,note,stacked=false,unit='USD bn',digits=0,fitValues=false,linkedZoom,onDateZoom,leftAxisLabel,leftAxisColor,rightAxis,normalizable=false}:Props){
 const [normalize,setNormalize]=useState(false);
 const normalized=normalizable&&normalize;
 const chartRows=useMemo(()=>normalized?normalizeRows(rows,plots.map(plot=>plot.key)):rows,[normalized,rows,plots]);
 const rawByDate=useMemo(()=>new Map(rows.map(row=>[row.date,row])),[rows]);
 const chartPlots=normalized?plots.map(plot=>({...plot,axis:'left' as const,name:plot.name.replace(/\s+\((left|right)\)$/,'')})):plots;
 const visibleRightAxis=normalized?undefined:rightAxis;
 const axisLabel=normalized?'Standard deviations':leftAxisLabel;
 const axisColor=normalized?undefined:leftAxisColor;
 const axisDigits=normalized?1:digits;
 const basePeriod=rows.length?`${rows[0].period} to ${rows[rows.length-1].period}`:'the selected date range';
 const chartSubtitle=normalized?`Normalized (z-score) · shared axis · reference: ${basePeriod}`:subtitle;
 const chartNote=normalized?`Z-score = (value − series mean) / population standard deviation, using available observations from ${basePeriod}. Zero is each series' average. Bars above/below zero mean above/below average, rather than the original sign. Zoom keeps this reference range. Tooltips include original values; constant series show 0.`:note;
 const dataset=subtitle+':'+rows.map(row=>row.date).join(',');
 const [savedZoom,setSavedZoom]=useState<{dataset:string;bounds:ZoomBounds}|null>(null);
 const [dragging,setDragging]=useState(false);
 const local=savedZoom?.dataset===dataset?savedZoom.bounds:null;
 const dates=onDateZoom?linkedZoom:local;
 const matching=local?.from===dates?.from&&local?.to===dates?.to?local:null;
 const zoom=dates?{...dates,y:matching?.y??null,rightY:matching?.rightY??null}:null;
 const first=zoom?rows.findIndex(row=>row.period===zoom.from):0;
 const last=zoom?rows.findIndex(row=>row.period===zoom.to):rows.length-1;
 const shown=zoom&&first>=0&&last>=first?chartRows.slice(first,last+1):chartRows;
 const normalizedExtent=normalized?shown.reduce((extent,row)=>Math.max(extent,...plots.map(plot=>typeof row[plot.key]==='number'?Math.abs(row[plot.key] as number):0)),1):1;
 const usable=shown.some(row=>plots.some(plot=>typeof row[plot.key]==='number'));
 const ySpan=zoom?.y?zoom.y[1]-zoom.y[0]:null;
 const yDigits=ySpan&&ySpan<10?Math.min(6,Math.max(axisDigits,2-Math.floor(Math.log10(ySpan)))):axisDigits;
 const rightSpan=zoom?.rightY?zoom.rightY[1]-zoom.rightY[0]:null;
 const rightDigits=rightSpan&&rightSpan<10?Math.min(6,Math.max(rightAxis?.digits??0,2-Math.floor(Math.log10(rightSpan)))):rightAxis?.digits??0;
 const reset=useCallback(()=>{setSavedZoom(null);setDragging(false);onDateZoom?.(null);},[onDateZoom]);
 function toggleNormalization(){
  setNormalize(value=>!value);setDragging(false);
  // Preserve dates but discard value bounds, which use different units.
  setSavedZoom(previous=>previous?{...previous,bounds:{from:previous.bounds.from,to:previous.bounds.to,y:null,rightY:null}}:null);
 }
 useEffect(()=>{setDragging(false);},[dataset]);
 useEffect(()=>{if(onDateZoom&&!linkedZoom)setSavedZoom(null);},[linkedZoom,onDateZoom]);
 return <Panel title={title} subtitle={chartSubtitle} className="bop-chart-panel" actions={<div className="bop-chart-actions">{normalizable&&<label className="bop-normalize-control" title="Z-score: each series minus its mean, divided by its standard deviation across the selected date range."><input type="checkbox" checked={normalized} onChange={toggleNormalization} aria-label={`Normalize data for ${title}`}/>Normalize</label>}<button className="button secondary tiny bop-reset-zoom" disabled={!zoom} onClick={reset} aria-label={`Reset zoom for ${title}`}><RotateCcw size={12}/>Reset zoom</button></div>}>
  <div className="bop-zoom-hint"><span>{zoom?`${shown[0]?.period} – ${shown[shown.length-1]?.period}`:'Drag across dates or draw a box to zoom'}</span>{zoom&&<span>Drag again to zoom further · Double-click to reset</span>}</div>
  <div className={`bop-chart${dragging?' bop-chart-dragging':''}`} role="img" aria-label={`${title}. ${chartSubtitle}. ${chartNote} Drag to zoom. Use Reset zoom to restore the full view.`} onDoubleClick={reset}>
   {usable?<ResponsiveContainer width="100%" height="100%"><ComposedChart data={shown} stackOffset={stacked?'sign':'none'} margin={{top:10,right:18,left:4,bottom:2}} accessibilityLayer>
    <CartesianGrid vertical={false} stroke="#e9edf2" strokeDasharray="3 4"/>
    <XAxis dataKey="period" minTickGap={45} axisLine={false} tickLine={false} tickFormatter={value=>String(value).replace(' Q',' / Q')}/>
    <YAxis width={axisLabel?80:64} axisLine={false} tickLine={false} tick={axisColor?{fill:axisColor}:undefined} label={axisLabel?{value:axisLabel,angle:-90,position:'insideLeft',offset:12,style:{fill:axisColor,fontSize:10,textAnchor:'middle'}}:undefined} tickFormatter={value=>number(Number(value),yDigits)} domain={zoom?.y??(normalized?[-normalizedExtent,normalizedExtent]:zoom||fitValues?['auto','auto']:[0,'auto'])} allowDataOverflow={Boolean(zoom?.y)}/>
    {visibleRightAxis&&<YAxis yAxisId="right" orientation="right" width={80} axisLine={false} tickLine={false} tick={{fill:visibleRightAxis.color}} label={{value:visibleRightAxis.label,angle:90,position:'insideRight',offset:12,style:{fill:visibleRightAxis.color,fontSize:10,textAnchor:'middle'}}} tickFormatter={value=>number(Number(value),rightDigits)} domain={zoom?.rightY??['auto','auto']} allowDataOverflow={Boolean(zoom?.rightY)}/>}
    <Tooltip active={dragging?false:undefined} formatter={(value,name,item)=>{
     const key=String(item.dataKey);const axis=plots.find(plot=>plot.key===key)?.axis==='right'?rightAxis:undefined;
     const original=rawByDate.get(String(item.payload?.date))?.[key];
     const rawUnit=axis?.unit??unit;const rawDigits=Math.max(rawUnit==='USD bn'?3:2,axis?.digits??digits);
     return [normalized?`${number(Number(value),2)} SD${typeof original==='number'?` · original: ${number(original,rawDigits)} ${rawUnit}`:''}`:`${number(Number(value),Math.max(2,axis?.digits??digits))} ${rawUnit}`,name];
    }} labelFormatter={label=>`${label} · ${chartSubtitle}`} contentStyle={{border:'1px solid #e3e8ee',borderRadius:8,fontSize:11}}/>
    <Legend wrapperStyle={{fontSize:10,paddingTop:12}} iconSize={12}/>
    {(normalized||!fitValues)&&<ReferenceLine y={0} stroke="#aeb8c4"/>}
    {chartPlots.map(plot=>plot.bar?<Bar key={plot.key} dataKey={plot.key} yAxisId={plot.axis==='right'?'right':0} name={plot.name} fill={plot.color} stackId={plot.stack} maxBarSize={18} isAnimationActive={false}/>:<Line key={plot.key} type="linear" dataKey={plot.key} yAxisId={plot.axis==='right'?'right':0} name={plot.name} stroke={plot.color} strokeWidth={plot.key==='current_account'?2.6:2} strokeDasharray={plot.dash?'5 4':undefined} dot={false} activeDot={{r:3}} connectNulls={false} isAnimationActive={false}/>)}
    <BopZoomOverlay key={dataset+':'+normalized+':'+JSON.stringify(zoom)} rightAxis={Boolean(visibleRightAxis)} periods={shown.map(row=>row.period)} onZoom={bounds=>{setSavedZoom({dataset,bounds});onDateZoom?.({from:bounds.from,to:bounds.to});}} onDragging={setDragging}/>
   </ComposedChart></ResponsiveContainer>:<div className="bop-chart-empty">No complete observations for this chart in the selected range.</div>}
  </div>
  <p className="bop-chart-note">{chartNote}</p>
 </Panel>;
}
