import {useXAxisInverseScale,useYAxisInverseScale} from 'recharts';
import ChartDragSelection from './ChartDragSelection';
import {selectedZoom} from './bopZoom';
import type {ZoomBounds} from './bopZoom';

export default function BopZoomOverlay({periods,onZoom,onDragging,rightAxis=false}:{periods:string[];onZoom:(bounds:ZoomBounds)=>void;onDragging:(active:boolean)=>void;rightAxis?:boolean}){
 const invertX=useXAxisInverseScale();const invertY=useYAxisInverseScale();
 const invertRightY=useYAxisInverseScale('right');
 if(!invertX||!invertY||periods.length<2)return null;
 return <ChartDragSelection classPrefix="bop-zoom" onDragging={onDragging} onSelect={(start,end)=>{
  const bounds=selectedZoom(start,end,periods,invertX,invertY,rightAxis?invertRightY:undefined);
  if(bounds)onZoom(bounds);
 }}/>;
}
