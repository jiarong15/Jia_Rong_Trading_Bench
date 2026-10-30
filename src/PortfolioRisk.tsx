import { useEffect, useState } from 'react';
import { Area, AreaChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { Download, ShieldCheck } from 'lucide-react';
import { api, download } from './api';
import { Badge, Metric, Panel, money, number } from './components';
import type { Workspace } from './types';

type Risk = {
  confidence: number; observations: number; horizon_days: number; nav: number;
  var_pct: number; var_amount: number; expected_shortfall_pct: number; expected_shortfall_amount: number;
  max_drawdown_pct: number; current_drawdown_pct: number; tracking_error_pct: number;
  benchmark_name: string; start_date: string; end_date: string; tail_observations: number;
  methodology: string; limitations: string;
  series: { date: string; portfolio: number; benchmark: number; drawdown_pct: number }[];
};

export default function PortfolioRisk({ data }: { data: Workspace }) {
  const [confidence, setConfidence] = useState('0.95');
  const [lookback, setLookback] = useState('252');
  const [benchmark, setBenchmark] = useState('balanced');
  const [risk, setRisk] = useState<Risk | null>(null);
  const [error, setError] = useState('');
  const [view, setView] = useState('drawdown');
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(''); setRisk(null);
    api<Risk>(`/portfolio/risk?confidence=${confidence}&lookback=${lookback}&benchmark=${benchmark}`, { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setRisk(result); })
      .catch(e => { if (!controller.signal.aborted) setError(e.message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [confidence, lookback, benchmark, data]);
  return <Panel title="Portfolio risk analytics" subtitle="Historical risk of your current holdings · hypothetical constant weights" actions={<Badge tone="amber">Synthetic history</Badge>}>
    <div className="portfolio-risk-controls">
      <label>CONFIDENCE<select aria-label="Risk confidence level" value={confidence} onChange={e => setConfidence(e.target.value)}><option value="0.95">95%</option><option value="0.99">99%</option></select></label>
      <label>LOOKBACK<select aria-label="Risk lookback" value={lookback} onChange={e => setLookback(e.target.value)}><option value="63">3 months · 63 sessions</option><option value="126">6 months · 126 sessions</option><option value="252">1 year · 252 sessions</option></select></label>
      <label>TRACKING BENCHMARK<select aria-label="Risk benchmark" value={benchmark} onChange={e => setBenchmark(e.target.value)}><option value="balanced">60% S&P 500 / 40% US IG</option><option value="SPX">S&P 500</option><option value="IG">US investment grade index</option></select></label>
      <button className="button secondary tiny" disabled={!risk || loading} onClick={() => download('portfolio-risk.json', risk)}><Download size={14}/>Export risk</button>
    </div>
    {loading ? <p className="padded muted" role="status">Calculating portfolio risk…</p> : error ? <p className="padded negative" role="alert">Risk analytics unavailable: {error}</p> : risk && <>
      <div className="metrics-grid portfolio-risk-metrics">
        <Metric label={`Value at Risk · ${number(risk.confidence * 100, 0)}%`} value={`${number(risk.var_pct)}%`} foot={`${money(risk.var_amount, 2)} · one-day loss threshold`} accent/>
        <Metric label="Expected shortfall" value={`${number(risk.expected_shortfall_pct)}%`} foot={`${money(risk.expected_shortfall_amount, 2)} · average tail loss`}/>
        <Metric label="Maximum drawdown" value={`${number(risk.max_drawdown_pct)}%`} foot={`Latest drawdown: ${number(risk.current_drawdown_pct)}%`}/>
        <Metric label="Tracking error · annualized" value={`${number(risk.tracking_error_pct)}%`} foot="Sample volatility of daily active returns"/>
      </div>
      <div className="portfolio-risk-chart-heading"><div><strong>{view === 'drawdown' ? 'Drawdown from peak' : 'Current-weight portfolio vs benchmark'}</strong><p>{risk.start_date} – {risk.end_date} · {risk.observations} daily observations</p></div><div className="segmented"><button className={view === 'drawdown' ? 'active' : ''} onClick={() => setView('drawdown')}>Drawdown</button><button className={view === 'performance' ? 'active' : ''} onClick={() => setView('performance')}>Relative performance</button></div></div>
      <div className="chart-wrap">
        <ResponsiveContainer width="100%" height="100%">{view === 'drawdown' ? <AreaChart data={risk.series} margin={{ top: 12, right: 20, left: 0, bottom: 0 }}><CartesianGrid vertical={false} stroke="#e9edf2" strokeDasharray="3 4"/><XAxis dataKey="date" minTickGap={65} tickFormatter={v => v.slice(5)} axisLine={false} tickLine={false}/><YAxis tickFormatter={v => `${number(v, 1)}%`} axisLine={false} tickLine={false}/><Tooltip formatter={v => [`${number(Number(v))}%`, 'Drawdown']} contentStyle={{ borderRadius: 8 }}/><Area type="linear" dataKey="drawdown_pct" stroke="#b97973" fill="#f4e8e5" strokeWidth={2}/></AreaChart> : <LineChart data={risk.series} margin={{ top: 12, right: 20, left: 0, bottom: 0 }}><CartesianGrid vertical={false} stroke="#e9edf2" strokeDasharray="3 4"/><XAxis dataKey="date" minTickGap={65} tickFormatter={v => v.slice(5)} axisLine={false} tickLine={false}/><YAxis domain={['auto','auto']} axisLine={false} tickLine={false}/><Tooltip formatter={(v, name) => [number(Number(v)), name === 'portfolio' ? 'Current-weight portfolio' : risk.benchmark_name]} contentStyle={{ borderRadius: 8 }}/><Line type="linear" dataKey="portfolio" stroke="#388675" dot={false} strokeWidth={2}/><Line type="linear" dataKey="benchmark" stroke="#9eafc5" dot={false} strokeDasharray="5 5" strokeWidth={2}/></LineChart>}</ResponsiveContainer>
      </div>
      <div className="chart-footer"><span><i style={{ background: view === 'drawdown' ? '#b97973' : '#388675' }}/>{view === 'drawdown' ? 'Hypothetical drawdown' : 'Portfolio · starting index 100'}</span><span>Benchmark: {risk.benchmark_name}</span></div>
      <details className="portfolio-risk-method"><summary><ShieldCheck size={15}/>Methodology & coverage</summary><p>{risk.methodology}</p><p>{risk.limitations}</p><p>Tail sample mass: {number(risk.tail_observations, 2)} observations. VaR is a loss threshold, not the maximum possible loss. A balanced benchmark is rebalanced daily at 60/40 weights.</p><p>Definitions: <a href="https://www.cfainstitute.org/insights/professional-learning/refresher-readings/2026/measuring-managing-market-risk" target="_blank" rel="noreferrer">CFA Institute — market risk</a> · <a href="https://se.mathworks.com/help/finance/tracking-error.html" target="_blank" rel="noreferrer">MathWorks — tracking error</a></p></details>
    </>}
  </Panel>;
}
