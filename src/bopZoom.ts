export type ZoomPoint={x:number;y:number};
export type ZoomArea={x:number;y:number;width:number;height:number};
export type ZoomBounds={from:string;to:string;y:[number,number]|null;rightY?:[number,number]|null};
type InverseScale=(pixel:number)=>unknown;

export function clampZoomPoint(point:ZoomPoint,area:ZoomArea):ZoomPoint{
 return {x:Math.max(area.x,Math.min(area.x+area.width,point.x)),y:Math.max(area.y,Math.min(area.y+area.height,point.y))};
}

export function selectedZoom(start:ZoomPoint,end:ZoomPoint,periods:string[],invertX:InverseScale,invertY:InverseScale,invertRightY?:InverseScale):ZoomBounds|null{
 if(![start.x,start.y,end.x,end.y].every(Number.isFinite)||Math.abs(end.x-start.x)<6)return null;
 const left=periods.indexOf(String(invertX(Math.min(start.x,end.x))));
 const right=periods.indexOf(String(invertX(Math.max(start.x,end.x))));
 if(left<0||right<0||left===right)return null;
 const [first,last]=[left,right].sort((a,b)=>a-b);
 let y:[number,number]|null=null;
 let rightY:[number,number]|null=null;
 // A horizontal drag selects dates and lets the chart fit the values. A box
 // also selects a numeric Y domain, using the chart's actual inverse scale.
 if(Math.abs(end.y-start.y)>=10){
  const a=invertY(start.y),b=invertY(end.y);
  if(typeof a!=='number'||typeof b!=='number'||!Number.isFinite(a)||!Number.isFinite(b)||a===b)return null;
  y=[Math.min(a,b),Math.max(a,b)];
  if(invertRightY){
   const aRight=invertRightY(start.y),bRight=invertRightY(end.y);
   if(typeof aRight!=='number'||typeof bRight!=='number'||!Number.isFinite(aRight)||!Number.isFinite(bRight)||aRight===bRight)return null;
   rightY=[Math.min(aRight,bRight),Math.max(aRight,bRight)];
  }
 }
 return {from:periods[first],to:periods[last],y,...(invertRightY?{rightY}:{})};
}
