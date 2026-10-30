import {useCallback,useEffect,useRef,useState} from 'react';
import type {PointerEvent} from 'react';
import {usePlotArea,useXAxisInverseScale,useYAxisInverseScale,ZIndexLayer} from 'recharts';
import {clampZoomPoint,selectedZoom} from './bopZoom';
import type {ZoomBounds,ZoomPoint} from './bopZoom';

type Drag={id:number;start:ZoomPoint;end:ZoomPoint};

export default function BopZoomOverlay({periods,onZoom,onDragging,rightAxis=false}:{periods:string[];onZoom:(bounds:ZoomBounds)=>void;onDragging:(active:boolean)=>void;rightAxis?:boolean}){
 const area=usePlotArea();const invertX=useXAxisInverseScale();const invertY=useYAxisInverseScale();
 const invertRightY=useYAxisInverseScale('right');
 const target=useRef<SVGRectElement|null>(null);const drag=useRef<Drag|null>(null);
 const [selection,setSelection]=useState<Drag|null>(null);
 const cancel=useCallback(()=>{
  const active=drag.current;drag.current=null;setSelection(null);
  if(active){if(target.current?.hasPointerCapture(active.id))target.current.releasePointerCapture(active.id);onDragging(false);}
 },[onDragging]);
 useEffect(()=>{
  const escape=(event:KeyboardEvent)=>{if(event.key==='Escape')cancel();};
  window.addEventListener('keydown',escape);window.addEventListener('blur',cancel);
  return()=>{window.removeEventListener('keydown',escape);window.removeEventListener('blur',cancel);};
 },[cancel]);
 if(!area||!invertX||!invertY||area.width<=0||area.height<=0||periods.length<2)return null;
 function point(event:PointerEvent<SVGRectElement>):ZoomPoint|null{
  const matrix=event.currentTarget.ownerSVGElement?.getScreenCTM();
  if(!matrix||!area)return null;
  const local=new DOMPoint(event.clientX,event.clientY).matrixTransform(matrix.inverse());
  return clampZoomPoint(local,area);
 }
 function begin(event:PointerEvent<SVGRectElement>){
  if(event.button!==0||event.pointerType==='touch'||drag.current)return;
  const start=point(event);if(!start)return;
  event.preventDefault();event.currentTarget.setPointerCapture(event.pointerId);
  const next={id:event.pointerId,start,end:start};drag.current=next;setSelection(next);onDragging(true);
 }
 function move(event:PointerEvent<SVGRectElement>){
  const active=drag.current;if(!active||active.id!==event.pointerId)return;
  const end=point(event);if(!end)return;
  const next={...active,end};drag.current=next;setSelection(next);
 }
 function finish(event:PointerEvent<SVGRectElement>){
  const active=drag.current;if(!active||active.id!==event.pointerId||!invertX||!invertY)return;
  const end=point(event)??active.end;
  const bounds=selectedZoom(active.start,end,periods,invertX,invertY,rightAxis?invertRightY:undefined);
  cancel();if(bounds)onZoom(bounds);
 }
 return <ZIndexLayer zIndex={1000}><g className="bop-zoom-overlay">
  <rect ref={target} className="bop-zoom-hit" x={area.x} y={area.y} width={area.width} height={area.height} fill="transparent" pointerEvents="all" onPointerDown={begin} onPointerMove={move} onPointerUp={finish} onPointerCancel={cancel} onLostPointerCapture={cancel}/>
  {selection&&<rect className="bop-zoom-selection" x={Math.min(selection.start.x,selection.end.x)} y={Math.abs(selection.end.y-selection.start.y)<10?area.y:Math.min(selection.start.y,selection.end.y)} width={Math.abs(selection.end.x-selection.start.x)} height={Math.abs(selection.end.y-selection.start.y)<10?area.height:Math.abs(selection.end.y-selection.start.y)} fill="#388675" fillOpacity={.15} stroke="#388675" strokeDasharray="4 3" pointerEvents="none"/>}
 </g></ZIndexLayer>;
}
