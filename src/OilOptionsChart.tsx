import {useState} from 'react';
import type {ReactNode} from 'react';
import {RotateCcw} from 'lucide-react';
import {CartesianGrid, ComposedChart, ResponsiveContainer, Tooltip, useXAxisInverseScale, useYAxisInverseScale, XAxis, YAxis} from 'recharts';
import ChartDragSelection from './ChartDragSelection';
import {number} from './components';

type Range = [number, number];
type Zoom = {x: Range; y: Range | null};
type Props = {
  title: string;
  data: {days?: number; strike?: number}[];
  xKey: 'days' | 'strike';
  xLabel: string;
  yLabel: string;
  yWidth: number;
  yDomain?: [number, number | 'auto'];
  yDigits?: number;
  yPercent?: boolean;
  formatValue: (value: number) => string;
  children: ReactNode;
};

const CHART_MARGIN = {top: 12, right: 16, bottom: 4, left: 0};
const TOOLTIP_STYLE = {border: '1px solid #dbe5e2', borderRadius: 8, fontSize: 12};

function orderedRange(a: unknown, b: unknown): Range | null {
  if (typeof a !== 'number' || typeof b !== 'number' || !Number.isFinite(a) || !Number.isFinite(b) || a === b) return null;
  return [Math.min(a, b), Math.max(a, b)];
}

function tickDigits(range: Range | null, minimum: number, multiplier = 1) {
  if (!range) return minimum;
  const tickStep = (range[1] - range[0]) * multiplier / 4;
  return Math.max(minimum, Math.min(8, Math.ceil(-Math.log10(tickStep))));
}

function NumericZoomSelection({onZoom, onDragging}: {onZoom: (zoom: Zoom) => void; onDragging: (active: boolean) => void}) {
  const invertX = useXAxisInverseScale();
  const invertY = useYAxisInverseScale();
  if (!invertX || !invertY) return null;

  return <ChartDragSelection classPrefix="oil-zoom" onDragging={onDragging} onSelect={(start, end, horizontal) => {
    const x = orderedRange(invertX(start.x), invertX(end.x));
    const y = horizontal ? null : orderedRange(invertY(start.y), invertY(end.y));
    if (x && (horizontal || y)) onZoom({x, y});
  }}/>;
}

export default function OilOptionsChart({title, data, xKey, xLabel, yLabel, yWidth, yDomain, yDigits = 0, yPercent = false, formatValue, children}: Props) {
  const [zoom, setZoom] = useState<Zoom | null>(null);
  const [dragging, setDragging] = useState(false);
  const canZoom = data.some(row => row[xKey] !== data[0]?.[xKey]);
  const reset = () => setZoom(null);
  const xDigits = tickDigits(zoom?.x ?? null, 0);
  const yMultiplier = yPercent ? 100 : 1;
  const valueDigits = tickDigits(zoom?.y ?? null, yDigits, yMultiplier);

  return <>
    {canZoom && <div className="oil-zoom-controls">
      <span>Drag to zoom</span>
      <button type="button" className="button secondary tiny" onClick={reset} disabled={!zoom} aria-label={`Reset zoom for ${title}`}>
        <RotateCcw size={12}/>Reset zoom
      </button>
    </div>}
    <div className={`oil-chart${dragging ? ' oil-chart-dragging' : ''}`} onDoubleClick={reset}>
      <ResponsiveContainer width="100%" height="100%"><ComposedChart data={data} margin={CHART_MARGIN}>
        <CartesianGrid vertical={false} stroke="#e9efee" strokeDasharray="3 4"/>
        <XAxis dataKey={xKey} type="number" domain={zoom?.x ?? ['dataMin', 'dataMax']} allowDataOverflow={Boolean(zoom)}
          height={46} tickMargin={7} label={{value: xLabel, position: 'insideBottom', offset: 2, className: 'oil-axis-label'}}
          tickFormatter={value => xKey === 'days' ? `${number(value, xDigits)}d` : `$${number(value, xDigits)}`} tickLine={false} axisLine={false}/>
        <YAxis width={yWidth + 20} domain={zoom?.y ?? yDomain} allowDataOverflow={Boolean(zoom?.y)}
          label={{value: yLabel, position: 'insideLeft', offset: 8, angle: -90, textAnchor: 'middle', className: 'oil-axis-label'}}
          tickFormatter={value => `${number(value * yMultiplier, valueDigits)}${yPercent ? '%' : ''}`} tickLine={false} axisLine={false}/>
        <Tooltip active={dragging ? false : undefined} contentStyle={TOOLTIP_STYLE}
          labelFormatter={value => xKey === 'days' ? `${number(Number(value), 1)} days` : `$${number(Number(value), 2)}`}
          formatter={value => formatValue(Number(value))}/>
        {children}
        {canZoom && <NumericZoomSelection onDragging={setDragging}
          onZoom={next => setZoom(previous => ({x: next.x, y: next.y ?? previous?.y ?? null}))}/>}
      </ComposedChart></ResponsiveContainer>
    </div>
  </>;
}
