import { useEffect, useRef, useState } from 'react';
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { ArrowRight, Check, Download, RefreshCw, Save, ShieldCheck } from 'lucide-react';
import { api, download, post } from './api';
import { Badge, Panel, money, number, signed } from './components';
import type { PageProps } from './pages';

type Policy = { targets: Record<string, number>; band: number; pace: number; max_turnover: number; min_trade_weight: number; saved: boolean };
type Advice = { status: string; reason: string; targets: Record<string, number>; policy: Policy; monitoring: string; execution: string; limitations: string; breaches: { instrument_id: string; drift: number; boundary: number }[]; papers: { title: string; url: string; use: string }[] };
type Risk = { var_pct: number; var_amount: number; expected_shortfall_pct: number; expected_shortfall_amount: number; max_drawdown_pct: number; tracking_error_pct: number; series: { date: string; portfolio: number }[] };
type Preview = { fingerprint: string; before: Risk; after: Risk; turnover: number; policy_turnover_exceeded: boolean; fees: number; slippage: number; estimated_cost: number; nav_after: number; cash_after: number; feasible: boolean; can_execute: boolean; violations: string[]; assumptions: string; rows: { instrument_id: string; current_weight: number; target_weight: number; projected_weight: number }[]; orders: { instrument_id: string; symbol: string; side: string; quantity: number; reference_price: number; fill_price: number; notional: number; fee: number }[] };

export default function Rebalance({ data, reload, notify }: PageProps) {
  const [targets, setTargets] = useState<Record<string, number>>({});
  const [strength, setStrength] = useState(1);
  const [confidence, setConfidence] = useState(.95);
  const [lookback, setLookback] = useState(252);
  const [benchmark, setBenchmark] = useState('balanced');
  const [policy, setPolicy] = useState({ band: .02, pace: .5, max_turnover: .1 });
  const [advice, setAdvice] = useState<Advice | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [ready, setReady] = useState(false);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const initialized = useRef(false);
  const requestId = useRef('');
  useEffect(() => {
    let active = true;
    api<Advice>('/rebalance/policy').then(result => {
      if (!active) return;
      setAdvice(result);
      if (!initialized.current) {
        setTargets(result.policy.targets);
        setPolicy({ band: result.policy.band, pace: result.policy.pace, max_turnover: result.policy.max_turnover });
        initialized.current = true; setReady(true);
      }
    }).catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [data]);
  useEffect(() => {
    const timer = setInterval(() => { if (!document.hidden) reload().catch(() => setError('Market refresh failed. Refresh before approval.')); }, 30000);
    return () => clearInterval(timer);
  }, [reload]);
  useEffect(() => {
    if (!ready) return;
    const controller = new AbortController();
    setLoading(true); setPreview(null); setError('');
    requestId.current = crypto.randomUUID();
    const timer = setTimeout(() => {
      api<Preview>('/rebalance/preview', { method: 'POST', body: JSON.stringify({ targets, strength, confidence, lookback, benchmark }), signal: controller.signal })
        .then(result => { if (!controller.signal.aborted) setPreview(result); })
        .catch(e => { if (!controller.signal.aborted) setError(e.message); })
        .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    }, 250);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [targets, strength, confidence, lookback, benchmark, data, ready]);
  const total = Object.values(targets).reduce((sum, w) => sum + w, 0);
  const holdings = data.instruments.filter(i => i.paper_eligible && (i.id in targets || (data.positions[i.id] ?? 0) > 0));
  const available = data.instruments.filter(i => i.paper_eligible && !holdings.some(h => h.id === i.id));
  async function savePolicy() {
    setBusy(true);
    try {
      const result = await api<Advice>('/rebalance/policy', { method: 'PUT', body: JSON.stringify({ targets, ...policy, min_trade_weight: .0005 }) });
      setAdvice(result); await reload(); notify('Strategic targets and rebalance policy saved.');
    } catch (e) { notify(e instanceof Error ? e.message : 'Policy save failed', true); }
    finally { setBusy(false); }
  }
  async function approve() {
    if (!preview) return;
    setBusy(true);
    try {
      await post('/rebalance/execute', { targets, strength, confidence, lookback, benchmark, baseline_fingerprint: preview.fingerprint, request_id: requestId.current });
      await reload(); notify('Paper rebalance filled atomically; portfolio and risk metrics refreshed.');
    } catch (e) { notify(e instanceof Error ? e.message : 'Rebalance failed', true); }
    finally { setBusy(false); }
  }
  const comparison = preview ? preview.after.series.map((point, n) => ({ date: point.date, before: preview.before.series[n]?.portfolio, after: point.portfolio })) : [];
  return <section className="rebalance-section" aria-label="Portfolio rebalancing">
    <Panel title="Rebalance your portfolio" subtitle="Explore allocations, compare risk, then review a paper basket" actions={<Badge>Preview before approval</Badge>}>
      <div className="rebalance-layout">
        <div className="rebalance-sliders">
          <div className="rebalance-subheading"><strong>Target allocations</strong><span>Cash is the residual</span></div>
          {holdings.map(i => { const current = data.portfolio.positions.find(p => p.id === i.id)?.weight ?? 0; return <div className="rebalance-slider-row" key={i.id}>
            <div><label htmlFor={`weight-${i.id}`}><strong>{i.symbol}</strong><small>{i.name}</small></label><span>Now {number(current * 100, 1)}%</span><input aria-label={`${i.symbol} target percent`} type="number" min="0" max={data.limits.max_position * 100} step="0.1" value={Number(((targets[i.id] ?? 0) * 100).toFixed(2))} onChange={e => setTargets({ ...targets, [i.id]: Number(e.target.value) / 100 })}/><b>%</b></div>
            <input id={`weight-${i.id}`} aria-label={`${i.symbol} allocation slider`} type="range" min="0" max={data.limits.max_position * 100} step="0.25" value={(targets[i.id] ?? 0) * 100} onChange={e => setTargets({ ...targets, [i.id]: Number(e.target.value) / 100 })}/>
          </div>; })}
          {available.length > 0 && <select aria-label="Add rebalance instrument" value="" onChange={e => { if (e.target.value) setTargets({ ...targets, [e.target.value]: 0 }); }}><option value="">+ Add an eligible instrument</option>{available.map(i => <option key={i.id} value={i.id}>{i.symbol} — {i.name}</option>)}</select>}
          <div className="rebalance-cash"><span>Target cash</span><strong className={total > data.limits.max_gross ? 'negative' : ''}>{number((1 - total) * 100, 2)}%</strong><small>Gross cap {number(data.limits.max_gross * 100, 0)}% · per-position cap {number(data.limits.max_position * 100, 0)}%</small></div>
          <label className="rebalance-pace">Move toward this mix <strong>{number(strength * 100, 0)}%</strong><input aria-label="Rebalance transition strength" type="range" min="0" max="100" step="5" value={strength * 100} onChange={e => setStrength(Number(e.target.value) / 100)}/><small>0% leaves holdings unchanged; 100% targets the full draft, subject to lot sizes and costs.</small></label>
          <button className="button secondary full" onClick={() => { setTargets(Object.fromEntries(data.portfolio.positions.map(p => [p.id, p.weight]))); setStrength(1); }}>Reset to current holdings</button>
          <p className="rebalance-muted">USD cash bonds and selected equities can be rebalanced. FX, rate series and commodity contracts remain monitoring-only until their execution models are connected.</p>
        </div>
        <div className="rebalance-results">
          <div className="rebalance-risk-controls"><label>Confidence<select aria-label="Rebalance risk confidence" value={confidence} onChange={e => setConfidence(Number(e.target.value))}><option value="0.95">95%</option><option value="0.99">99%</option></select></label><label>History<select aria-label="Rebalance risk lookback" value={lookback} onChange={e => setLookback(Number(e.target.value))}>{[63,126,252].map(n => <option key={n} value={n}>{n} sessions</option>)}</select></label><label>Tracking benchmark<select aria-label="Rebalance tracking benchmark" value={benchmark} onChange={e => setBenchmark(e.target.value)}><option value="balanced">60/40 SPX / IG</option><option value="SPX">S&P 500</option><option value="IG">US IG</option></select></label></div>
          {loading && <p className="padded muted" role="status">Recalculating rounded, post-cost portfolio risk…</p>}
          {error && <p className="padded negative" role="alert">{error}</p>}
          {preview && <>
            <div className="table-scroll"><table className="risk-comparison"><thead><tr><th>RISK MEASURE</th><th className="num">CURRENT</th><th className="num">PROJECTED</th><th className="num">CHANGE</th></tr></thead><tbody>{[
              { label: `1-day VaR (${number(confidence * 100, 0)}%)`, key: 'var_pct' as const }, { label: '1-day expected shortfall', key: 'expected_shortfall_pct' as const }, { label: 'Maximum drawdown · simulated', key: 'max_drawdown_pct' as const }, { label: 'Tracking error · annualized', key: 'tracking_error_pct' as const },
            ].map(row => { const before = preview.before[row.key], after = preview.after[row.key]; return <tr key={row.key}><td>{row.label}</td><td className="num">{number(before)}%</td><td className="num"><strong>{number(after)}%</strong></td><td className="num">{signed(after - before)} pp</td></tr>; })}</tbody></table></div>
            <div className="rebalance-dollar-risk"><span>VaR: {money(preview.before.var_amount, 2)} <ArrowRight size={12}/> {money(preview.after.var_amount, 2)}</span><span>ES: {money(preview.before.expected_shortfall_amount, 2)} <ArrowRight size={12}/> {money(preview.after.expected_shortfall_amount, 2)}</span></div>
            <div className="chart-wrap rebalance-chart"><ResponsiveContainer width="100%" height="100%"><AreaChart data={comparison} margin={{ top: 12, right: 12, left: -10, bottom: 0 }}><CartesianGrid vertical={false} stroke="#e9edf2" strokeDasharray="3 4"/><XAxis dataKey="date" minTickGap={60} tickFormatter={v => v.slice(5)} axisLine={false} tickLine={false}/><YAxis domain={['auto','auto']} axisLine={false} tickLine={false}/><Tooltip formatter={(v, name) => [number(Number(v)), name === 'after' ? 'Projected' : 'Current']} contentStyle={{ borderRadius: 8 }}/><Area type="linear" dataKey="before" fill="transparent" stroke="#a1b1c6" strokeDasharray="5 5"/><Area type="linear" dataKey="after" fill="#38867512" stroke="#388675" strokeWidth={2}/></AreaChart></ResponsiveContainer></div>
            <div className="chart-footer"><span><i style={{ background: '#a1b1c6' }}/>Current<i style={{ background: '#388675', marginLeft: 12 }}/>Projected</span><span>Hypothetical indices · rebased to 100</span></div>
            <div className="rebalance-costs"><div><span>Gross traded / NAV</span><strong>{number(preview.turnover * 100, 2)}%</strong></div><div><span>Slippage + fees</span><strong>{money(preview.estimated_cost, 2)}</strong></div><div><span>Cash after trades</span><strong>{money(preview.cash_after, 2)}</strong></div></div>
            {preview.violations.map(message => <p role="alert" className="rebalance-warning" key={message}>{message}</p>)}
            {preview.policy_turnover_exceeded && <p className="rebalance-warning">This manual scenario exceeds the recommendation policy’s normal turnover budget. Approval would override that soft budget; hard mandate limits still apply.</p>}
          </>}
        </div>
      </div>
      {preview && <div className="rebalance-basket"><div className="rebalance-subheading"><strong>Rounded trade basket · sells first</strong><button className="button secondary tiny" onClick={() => download('rebalance-preview.json', preview)}><Download size={13}/>Export scenario</button></div>{preview.orders.length ? <div className="table-scroll"><table><thead><tr><th>INSTRUMENT</th><th>SIDE</th><th className="num">NATIVE QUANTITY</th><th className="num">REFERENCE</th><th className="num">SIMULATED FILL</th><th className="num">FEE</th></tr></thead><tbody>{preview.orders.map(o => <tr key={o.instrument_id}><td><strong>{o.symbol}</strong></td><td><Badge tone={o.side === 'buy' ? 'green' : 'neutral'}>{o.side}</Badge></td><td className="num">{number(o.quantity, 0)}</td><td className="num">{number(o.reference_price, 4)}</td><td className="num">{number(o.fill_price, 4)}</td><td className="num">{money(o.fee, 2)}</td></tr>)}</tbody></table></div> : <p className="padded muted">No trades at these settings after lot rounding.</p>}<details className="portfolio-risk-method"><summary>Assumptions and achieved allocations</summary><p>{preview.assumptions}</p><p>Risk uses the same confidence, benchmark and history before and after, with post-cost weights. Drawdown is a historical hypothetical measure, not a forecast or the account’s realized drawdown.</p>{preview.rows.map(row => <p key={row.instrument_id}>{row.instrument_id}: {number(row.current_weight * 100, 2)}% current → {number(row.target_weight * 100, 2)}% full target → {number(row.projected_weight * 100, 2)}% achieved at the selected transition strength.</p>)}</details><div className="rebalance-approve"><span><ShieldCheck size={15}/>Review the basket above. Approval fills this paper basket only; future rebalances remain manual.</span><button className="button primary" disabled={busy || loading || !preview.can_execute || !!error} onClick={approve}><Check size={15}/>{busy ? 'Processing…' : 'Approve paper rebalance'}</button></div></div>}
    </Panel>
    <Panel title="Rebalancing recommendation" subtitle="A saved strategic policy that responds to portfolio drift" actions={<button className="button secondary tiny" disabled={busy} onClick={() => reload().catch(e => notify(String(e), true))}><RefreshCw size={14}/>Refresh snapshot</button>}>
      <div className="rebalance-advice">
        <div className="rebalance-policy-controls"><label>Tolerance band · ± percentage points<input aria-label="Rebalance tolerance band" type="number" min="0.1" max="10" step="0.1" value={Number((policy.band * 100).toFixed(2))} onChange={e => setPolicy({ ...policy, band: Number(e.target.value) / 100 })}/></label><label>Correction toward band boundary · %<input aria-label="Rebalance correction pace" type="number" min="10" max="100" step="10" value={Number((policy.pace * 100).toFixed(0))} onChange={e => setPolicy({ ...policy, pace: Number(e.target.value) / 100 })}/></label><label>Gross turnover budget / review · %<input aria-label="Rebalance turnover budget" type="number" min="1" max="100" step="1" value={Number((policy.max_turnover * 100).toFixed(0))} onChange={e => setPolicy({ ...policy, max_turnover: Number(e.target.value) / 100 })}/></label><button className="button secondary" disabled={!ready || busy || total > data.limits.max_gross} onClick={savePolicy}><Save size={14}/>Save draft targets as policy</button></div>
        <p className="rebalance-muted">Recommendations use the saved targets and policy—not unsaved slider changes. Defaults are illustrative; calibrate them in walk-forward tests with your real costs and data.</p>
        {advice && <><div className="rebalance-advice-heading"><Badge tone={advice.status === 'hold' ? 'green' : 'amber'}>{advice.status.replaceAll('_', ' ')}</Badge><strong>{advice.reason}</strong></div>{advice.breaches.map(b => <p className="rebalance-muted" key={b.instrument_id}>{b.instrument_id}: drift {signed(b.drift * 100)} pp; nearest band boundary {number(b.boundary * 100, 2)}%.</p>)}<p>{advice.monitoring}</p><p>{advice.execution}</p><button className="button secondary" disabled={busy || advice.status === 'policy_setup' || advice.status === 'policy_review'} onClick={() => { setTargets(advice.targets); setStrength(1); }}><ArrowRight size={15}/>Preview recommended step</button><details className="portfolio-risk-method"><summary>Research basis & implementation limits</summary><p>{advice.limitations}</p>{advice.papers.map(paper => <p key={paper.url}><a href={paper.url} target="_blank" rel="noreferrer">{paper.title}</a><br/>{paper.use}</p>)}</details></>}
      </div>
    </Panel>
  </section>;
}
