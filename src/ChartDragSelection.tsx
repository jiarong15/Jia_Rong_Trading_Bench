import {useCallback, useEffect, useRef, useState} from 'react';
import type {PointerEvent} from 'react';
import {usePlotArea, ZIndexLayer} from 'recharts';
import {clampZoomPoint} from './bopZoom';
import type {ZoomPoint} from './bopZoom';

type Drag = {id: number; start: ZoomPoint; end: ZoomPoint};
type Props = {
  classPrefix: string;
  onSelect: (start: ZoomPoint, end: ZoomPoint, horizontal: boolean) => void;
  onDragging: (active: boolean) => void;
};

const horizontalDrag = (start: ZoomPoint, end: ZoomPoint) => Math.abs(end.y - start.y) < 10;

export default function ChartDragSelection({classPrefix, onSelect, onDragging}: Props) {
  const area = usePlotArea();
  const target = useRef<SVGRectElement>(null);
  const drag = useRef<Drag | null>(null);
  const [selection, setSelection] = useState<Drag | null>(null);

  const cancel = useCallback(() => {
    const active = drag.current;
    if (!active) return;
    drag.current = null;
    setSelection(null);
    if (target.current?.hasPointerCapture(active.id)) target.current.releasePointerCapture(active.id);
    onDragging(false);
  }, [onDragging]);

  useEffect(() => {
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') cancel(); };
    window.addEventListener('keydown', escape);
    window.addEventListener('blur', cancel);
    return () => {
      window.removeEventListener('keydown', escape);
      window.removeEventListener('blur', cancel);
    };
  }, [cancel]);

  if (!area || area.width <= 0 || area.height <= 0) return null;

  function point(event: PointerEvent<SVGRectElement>) {
    const matrix = event.currentTarget.ownerSVGElement?.getScreenCTM();
    if (!matrix || !area) return null;
    const local = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
    return clampZoomPoint(local, area);
  }

  function begin(event: PointerEvent<SVGRectElement>) {
    if (event.button !== 0 || event.pointerType === 'touch' || drag.current) return;
    const start = point(event);
    if (!start) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    const next = {id: event.pointerId, start, end: start};
    drag.current = next;
    setSelection(next);
    onDragging(true);
  }

  function move(event: PointerEvent<SVGRectElement>) {
    const active = drag.current;
    if (!active || active.id !== event.pointerId) return;
    const end = point(event);
    if (!end) return;
    const next = {...active, end};
    drag.current = next;
    setSelection(next);
  }

  function finish(event: PointerEvent<SVGRectElement>) {
    const active = drag.current;
    if (!active || active.id !== event.pointerId) return;
    const end = point(event) ?? active.end;
    cancel();
    if (Math.abs(end.x - active.start.x) >= 6) onSelect(active.start, end, horizontalDrag(active.start, end));
  }

  const horizontal = selection && horizontalDrag(selection.start, selection.end);
  return <ZIndexLayer zIndex={1000}><g className={`${classPrefix}-overlay`}>
    <rect ref={target} className={`${classPrefix}-hit`} x={area.x} y={area.y} width={area.width} height={area.height}
      fill="transparent" pointerEvents="all" onPointerDown={begin} onPointerMove={move} onPointerUp={finish}
      onPointerCancel={cancel} onLostPointerCapture={cancel}/>
    {selection && <rect className={`${classPrefix}-selection`} fill="#388675" fillOpacity={.15}
      stroke="#388675" strokeDasharray="4 3" pointerEvents="none"
      x={Math.min(selection.start.x, selection.end.x)} y={horizontal ? area.y : Math.min(selection.start.y, selection.end.y)}
      width={Math.abs(selection.end.x - selection.start.x)} height={horizontal ? area.height : Math.abs(selection.end.y - selection.start.y)}/>}
  </g></ZIndexLayer>;
}
