import {useEffect, useState} from 'react';
import {Activity, ArrowUpRight, ChartNoAxesCombined, Clock3, RefreshCw} from 'lucide-react';
import {Area, Legend, Line, ReferenceLine} from 'recharts';
import {api} from './api';
import {Badge, Empty, Metric, Panel, number, signed} from './components';
import OilOptionsChart from './OilOptionsChart';
import './commodityOptions.css';

const MARKETS = {
  wti: {label: 'WTI · CME American (LO)', symbol: 'CL', kind: 'futures'},
  'wti-european': {label: 'WTI · CME European (LC)', symbol: 'CL', kind: 'futures'},
  uso: {label: 'WTI · USO ETF proxy', symbol: 'USO', kind: 'etf'},
  brent: {label: 'Brent · BNO ETF proxy', symbol: 'BNO', kind: 'etf'},
  xau: {label: 'XAU (gold) · GLD ETF proxy', symbol: 'GLD', kind: 'etf'},
  natgas: {label: 'NATGAS · UNG ETF proxy', symbol: 'UNG', kind: 'etf'},
} as const;

type Market = keyof typeof MARKETS;
type Basis = 'normal' | 'black';
type StrikePoint = {
  strike: number; side: 'call' | 'put'; premium: number; bid: number | null; ask: number | null;
  normal_iv: number; black_iv: number | null; fitted_iv: number | null; local_vol: number | null;
  model_premium: number | null; residual: number | null;
};
type Expiry = {
  expiry: string; contract: string; forward: number; days: number; atm_normal_iv: number | null;
  atm_black_iv: number | null; points: StrikePoint[];
  fit: {count: number; rmse: number | null; atm: number; slope: number; curvature: number} | null;
  distribution: {
    butterflies: {strike: number; density: number; raw_density: number; mass: number}[];
    cdf: {strike: number; probability: number}[]; covered_mass: number;
    violating_verticals: number; violating_butterflies: number; adjusted_intervals: number;
  } | null;
};
type Term = {
  expiry: string; days: number; interval_start_days: number; atm_iv: number; total_variance: number;
  local_variance: number; local_vol: number | null; negative_variance: boolean;
};
type Calibration = {
  market: Market; source: string; url: string; cadence: string; series: string; unit: string;
  exercise: string; poll_seconds: number; as_of: string | null; checked_at: string | null;
  status: 'ready' | 'stale' | 'unavailable'; reason: string | null; missing: string[];
  slices: Expiry[]; term: Record<Basis, Term[]>;
};

const TEAL = '#247663';
const AMBER = '#b57328';
const BLUE = '#5277a7';
const dateLabel = new Intl.DateTimeFormat('en-GB', {day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC'});
const timeLabel = new Intl.DateTimeFormat('en-SG', {
  day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
  timeZone: 'Asia/Singapore',
});
const dateText = (value: string) => dateLabel.format(new Date(`${value}T00:00:00Z`));
const timeText = (value: string) => `${timeLabel.format(new Date(value))} SGT`;
const display = (value: number | null | undefined, digits = 2) => value == null ? '—' : number(value, digits);
const percent = (value: number) => `${number(value * 100, 1)}%`;

export default function CommodityOptions() {
  const [market, setMarket] = useState<Market>('wti');
  const [basis, setBasis] = useState<Basis>('normal');
  const [expiry, setExpiry] = useState('');
  const [distributionView, setDistributionView] = useState<'density' | 'cdf'>('density');
  const [rateInput, setRateInput] = useState('0');
  const [rate, setRate] = useState(0);
  const [data, setData] = useState<Calibration | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const {symbol, kind} = MARKETS[market];
  const isEtf = kind === 'etf';
  const refreshMs = isEtf ? 60_000 : 300_000;

  useEffect(() => {
    const controller = new AbortController();
    let pending = false;
    async function update(force = false) {
      if (pending) return;
      pending = true;
      setLoading(true);
      try {
        const query = new URLSearchParams({market, rate_pct: String(rate), refresh: String(force)});
        const next = await api<Calibration>(`/commodities/options/calibration?${query}`, {signal: controller.signal});
        if (controller.signal.aborted) return;
        setData(next);
        setError('');
        setExpiry(current => next.slices.some(row => row.expiry === current) ? current :
          (next.slices.find(row => row.fit && row.distribution) ?? next.slices[0])?.expiry ?? '');
      } catch (err) {
        if (!controller.signal.aborted) setError(err instanceof Error ? err.message : 'Market data request failed.');
      } finally {
        pending = false;
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    const refresh = () => { void update(true); };
    const refreshWhenVisible = () => {
      if (document.visibilityState === 'visible') void update();
    };
    const interval = window.setInterval(refreshWhenVisible, refreshMs);
    window.addEventListener('jr-trading-workbench:refresh-commodities', refresh);
    document.addEventListener('visibilitychange', refreshWhenVisible);
    void update();
    return () => {
      controller.abort();
      window.clearInterval(interval);
      window.removeEventListener('jr-trading-workbench:refresh-commodities', refresh);
      document.removeEventListener('visibilitychange', refreshWhenVisible);
    };
  }, [market, rate, refreshMs]);

  const snapshot = data?.market === market ? data : null;
  const selected = snapshot?.slices.find(row => row.expiry === expiry);
  const term = snapshot?.term[basis] ?? [];
  const distribution = selected?.distribution;
  const distributionPoints = distributionView === 'density'
    ? distribution?.butterflies.map(({strike, density}) => ({strike, value: density}))
    : distribution?.cdf.map(({strike, probability}) => ({strike, value: probability}));
  const fit = selected?.fit;
  const unit = snapshot?.unit ?? (isEtf ? 'USD/share' : 'USD/bbl');
  const hasData = !!snapshot?.slices.length;
  const rateValue = Number(rateInput);
  const feedStatus = !hasData ? (loading ? 'Connecting' : 'Unavailable') :
    error || snapshot?.status === 'stale' ? 'Last successful snapshot' : snapshot?.cadence;
  const quoteOlderThanThirtyMinutes = isEtf && snapshot?.as_of &&
    Date.now() - new Date(snapshot.as_of).getTime() > 30 * 60_000;
  const residuals = selected?.points.filter(row => row.residual !== null)
    .sort((left, right) => Math.abs(right.residual!) - Math.abs(left.residual!)).slice(0, 12) ?? [];
  const formatVol = (value: number) => basis === 'black' ? percent(value) : number(value, 1);
  const normalVolLabel = `Normal volatility (${unit}/√yr)`;
  const termUnit = basis === 'black' ? 'annualized %' : `${unit}/√yr`;

  return <section className="oil-workspace" aria-labelledby="oil-calibration-title">
    <header className="oil-heading">
      <div className="oil-heading-title"><span className="oil-heading-icon"><Activity size={23}/></span><div>
        <h2 id="oil-calibration-title">Commodity options calibration</h2>
        <p>Implied volatility, risk-neutral probabilities and repricing diagnostics</p>
      </div></div>
      <div className="oil-feed-meta">
        <Badge tone={hasData && !error && snapshot?.status === 'ready' ? 'green' : 'neutral'}>{feedStatus}</Badge>
        {snapshot?.as_of && <span>{isEtf ? timeText(snapshot.as_of) : `${dateText(snapshot.as_of)} · CME trade date`}</span>}
      </div>
    </header>

    <div className="oil-controls">
      <label>Market<select value={market} onChange={event => {
        setMarket(event.target.value as Market);
        setLoading(true);
        setError('');
      }}>
        {Object.entries(MARKETS).map(([id, item]) => <option key={id} value={id}>{item.label}</option>)}
      </select></label>
      <label>Option expiry<select value={selected?.expiry ?? ''} disabled={!hasData} onChange={event => setExpiry(event.target.value)}>
        {!hasData && <option value="">Select after data loads</option>}
        {snapshot?.slices.map(row => <option value={row.expiry} key={row.expiry}>{dateText(row.expiry)} · {row.contract}</option>)}
      </select></label>
      <form className="oil-rate" onSubmit={event => {event.preventDefault(); setRate(rateValue);}}>
        <label>Discount rate (%)<input type="number" min="-5" max="25" step="0.01" value={rateInput}
          onChange={event => setRateInput(event.target.value)} /></label>
        <button className="button secondary" type="submit" disabled={rateInput === '' || !Number.isFinite(rateValue) || rateValue === rate}>Apply</button>
      </form>
      <button className="button secondary oil-refresh" disabled={loading} onClick={() => window.dispatchEvent(new Event('jr-trading-workbench:refresh-commodities'))}>
        <RefreshCw size={15} className={loading ? 'spin' : undefined}/>{loading ? 'Checking feed' : 'Refresh'}
      </button>
    </div>
    <div className="oil-source" aria-live="polite">
      <Clock3 size={15}/><span>{isEtf
        ? `Indicative ${symbol} ETF option midpoints · checks every minute${quoteOlderThanThirtyMinutes ? ' · older than 30 min' : ''}`
        : 'Final CME settlements · checks every five minutes · CME publishes daily'}</span>
      {snapshot?.checked_at && <span>Checked {timeText(snapshot.checked_at)}</span>}
      {snapshot && <a href={snapshot.url} target="_blank" rel="noreferrer">{snapshot.source}<ArrowUpRight size={13}/></a>}
    </div>
    {(error || snapshot?.reason) && <p className="oil-alert" role="status">{error || snapshot?.reason}</p>}
    {snapshot?.missing.length ? <p className="oil-alert">Matched settlements unavailable for {snapshot.missing.join(', ')}.</p> : null}

    {!hasData ? <Panel title="Calibration"><Empty title={loading ? 'Loading market prices' : 'Calibration unavailable'}
      detail={loading ? 'Matching option prices to contract expirations.' : 'Refresh the feed or choose another market.'}/></Panel> : <>
      <div className="metrics-grid oil-metrics">
        <Metric label={isEtf ? `Implied ${symbol} forward` : 'Matched CL settlement'} value={display(selected?.forward)} foot={unit} accent/>
        <Metric label="ATM normal IV" value={display(selected?.atm_normal_iv)} foot={`${unit}/√yr`}/>
        <Metric label="Fitted strikes" value={fit ? String(fit.count) : '—'} foot={fit ? `${selected?.points.length} observed strikes` : 'Insufficient strikes around forward'}/>
        <Metric label="Covered probability mass" value={distribution ? percent(distribution.covered_mass) : '—'} foot="Across observed strikes"/>
      </div>

      <Panel title="ATM variance bootstrap" subtitle={`Implied and interval local volatility · ${termUnit}`} className="oil-term"
        actions={<div className="segmented" aria-label="Volatility convention">
          <button className={basis === 'normal' ? 'active' : ''} aria-pressed={basis === 'normal'} onClick={() => setBasis('normal')}>Normal</button>
          <button className={basis === 'black' ? 'active' : ''} aria-pressed={basis === 'black'} onClick={() => setBasis('black')}>Black</button>
        </div>}>
        {term.length ? <OilOptionsChart key={`${market}:${basis}:${rate}`} title="ATM variance bootstrap" data={term}
          xKey="days" xLabel="Days to option expiry" yLabel={basis === 'black' ? 'Annualized volatility (%)' : normalVolLabel}
          yWidth={59} yDigits={1} yPercent={basis === 'black'} formatValue={formatVol}>
          <Legend iconType="plainline"/>
          <Line dataKey="atm_iv" name="ATM implied vol" stroke={TEAL} strokeWidth={2.4} dot={{r: 3}} isAnimationActive={false}/>
          <Line dataKey="local_vol" name="Interval local vol" stroke={AMBER} strokeWidth={2.1} strokeDasharray="5 4" dot={{r: 3}} isAnimationActive={false}/>
        </OilOptionsChart> : <Empty title="ATM volatility unavailable" detail="An at-the-forward strike or nearby strikes on both sides are required."/>}
        {term.some(row => row.negative_variance) && <p className="oil-note oil-warning">Negative interval variance at {term.filter(row => row.negative_variance).map(row => dateText(row.expiry)).join(', ')}. This flags a violation of the model’s time-homogeneity assumption.</p>}
        <details className="oil-details">
          <summary>Inspect variance by expiry</summary>
          <div className="table-scroll"><table><thead><tr><th>Expiry</th><th className="num">Interval (days)</th><th className="num">ATM IV</th><th className="num">Total variance</th><th className="num">Local variance</th></tr></thead>
          <tbody>{term.map(row => <tr key={row.expiry}><td>{dateText(row.expiry)}</td><td className="num">{number(row.interval_start_days, 1)}–{number(row.days, 1)}</td><td className="num">{formatVol(row.atm_iv)}</td><td className="num">{number(row.total_variance, basis === 'black' ? 5 : 2)}</td><td className={`num ${row.negative_variance ? 'negative' : ''}`}>{number(row.local_variance, basis === 'black' ? 5 : 2)}</td></tr>)}</tbody></table></div>
        </details>
      </Panel>

      {selected && <div className="oil-chart-grid">
        <Panel title="Normal IV and local smile" subtitle={dateText(selected.expiry)}>
          <OilOptionsChart key={`${market}:${selected.expiry}:${rate}`} title="Normal IV and local smile" data={selected.points}
            xKey="strike" xLabel={`Strike / price level (${unit})`} yLabel={normalVolLabel} yWidth={46} formatValue={display}>
            <ReferenceLine x={selected.forward} stroke="#9ba9ad" strokeDasharray="3 4"/>
            <Legend iconType="plainline"/>
            <Line dataKey="normal_iv" name="Market IV" stroke={TEAL} strokeWidth={1.2} dot={{r: 2}} isAnimationActive={false}/>
            <Line dataKey="fitted_iv" name="Fitted IV" stroke={BLUE} strokeWidth={2} dot={false} isAnimationActive={false}/>
            <Line dataKey="local_vol" name="Local vol" stroke={AMBER} strokeWidth={2} strokeDasharray="5 3" dot={false} isAnimationActive={false}/>
          </OilOptionsChart>
          <p className="oil-note oil-chart-caption">Read implied volatility by option strike and local volatility by underlying price level. The dashed vertical line marks {isEtf ? `the implied ${symbol} forward` : 'the matched CL futures settlement'}.</p>
          {!fit && <p className="oil-note oil-warning">Too few nearby observations to fit the quadratic smile. Market IV remains available.</p>}
        </Panel>
        <Panel title="Market-implied distribution" subtitle="Risk-neutral" actions={<div className="segmented" aria-label="Distribution view">
          <button className={distributionView === 'density' ? 'active' : ''} aria-pressed={distributionView === 'density'} onClick={() => setDistributionView('density')}>Density</button>
          <button className={distributionView === 'cdf' ? 'active' : ''} aria-pressed={distributionView === 'cdf'} onClick={() => setDistributionView('cdf')}>CDF</button>
        </div>}>
          {distribution ? <>
            <OilOptionsChart key={`${market}:${selected.expiry}:${rate}:${distributionView}`} title="Market-implied distribution" data={distributionPoints ?? []}
              xKey="strike" xLabel={`Strike price (${unit})`}
              yLabel={distributionView === 'density' ? 'Density (% per $1)' : 'Cumulative probability (%)'}
              yWidth={55} yDomain={[0, distributionView === 'cdf' ? 1 : 'auto']} yDigits={1} yPercent
              formatValue={value => `${percent(value)}${distributionView === 'density' ? ' per $1' : ''}`}>
              <ReferenceLine x={selected.forward} stroke="#9ba9ad" strokeDasharray="3 4"/>
              <Area type={distributionView === 'cdf' ? 'stepAfter' : 'linear'} dataKey="value"
                name={distributionView === 'cdf' ? 'Cumulative probability' : 'Risk-neutral density'} fill="#dcece7" stroke={TEAL} strokeWidth={2} isAnimationActive={false}/>
            </OilOptionsChart>
            <p className="oil-note oil-chart-caption">{distributionView === 'density'
              ? 'The area under the curve between two strikes estimates the option-implied probability of finishing in that price range at expiry.'
              : 'Each point gives the option-implied probability of finishing at or below that strike at expiry.'}</p>
            <p className="oil-note">{distribution.violating_butterflies} raw butterfly violations · {distribution.violating_verticals} vertical violations · {distribution.adjusted_intervals} adjusted intervals</p>
          </> : <Empty title="Distribution unavailable" detail="The butterfly estimate requires at least five usable strikes."/>}
        </Panel>
      </div>}

      {selected && <Panel title="Calibration diagnostics" subtitle="Largest absolute market-minus-model residuals"
        actions={fit?.rmse != null && <Badge>RMSE {display(fit.rmse, 3)} {unit}</Badge>}>
        {residuals.length ? <div className="table-scroll oil-diagnostics"><table><thead><tr><th>Strike</th><th>OTM side</th><th className="num">Market premium</th><th className="num">Model premium</th><th className="num">Residual ({unit})</th></tr></thead>
          <tbody>{residuals.map(row => <tr key={row.strike}><td>{display(row.strike)}</td><td className="oil-side">{row.side}</td><td className="num">{display(row.premium, 3)}</td><td className="num">{display(row.model_premium, 3)}</td><td className={`num ${row.residual! < 0 ? 'negative' : 'positive'}`}>{signed(row.residual!, 3)}</td></tr>)}</tbody></table></div>
          : <Empty title="Repricing unavailable" detail="Residuals are shown when the smile can be fitted around the forward."/>}
      </Panel>}
      <details className="oil-method">
        <summary><ChartNoAxesCombined size={16}/>Model and data assumptions</summary>
        <p>Chapter 12 of <em>Virtual Barrels</em>. Invert out-of-the-money premiums with Bachelier and Black-76. Normal volatility uses price units per square root of a year. Black volatility uses annualized percentages and requires positive forwards and strikes.</p>
        <p>The bootstrap differences total ATM variance between expiries under a common time-to-expiry assumption. {isEtf ? `Each expiry models the same ETF, ${symbol}.` : 'WTI maturities reference different CL futures.'} The strike view inverts a fitted quadratic normal smile using the leading-order approximation in §12.4. Fit residuals measure model disagreement with observed premiums.</p>
        <p>Butterflies use unequal strike spacing and call-equivalent premiums. The distribution projects vertical spreads onto the monotone convex bounds and reports only the probability mass inside quoted strikes. Discounting uses the entered flat annual rate, initially 0%.</p>
        {snapshot?.exercise === 'American' && <p>American options are treated as European using out-of-the-money premiums. Early exercise can affect the estimates. {isEtf ? `Each ${symbol} forward is inferred from near-the-money call–put pairs. Strikes and risk-neutral probabilities refer to the ETF share price.` : 'Each option month is matched to the same CL settlement and the exchange’s last trade date.'}</p>}
        {market === 'wti-european' && <p>CME’s European chain can have sparse coverage. Expiries with insufficient strikes remain visible.</p>}
      </details>
    </>}
  </section>;
}
