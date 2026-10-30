"""Current-weight historical risk, distinct from the realized account track record."""
from math import ceil, isfinite, sqrt
from statistics import stdev
from .data import AS_OF
from .engine import summarize

BENCHMARKS = {'balanced': '60% S&P 500 / 40% US IG', 'SPX': 'S&P 500', 'IG': 'US investment grade index'}

def tail_loss(losses, confidence):
    """Empirical inverse-CDF VaR and integrated upper-tail ES, including fractional mass."""
    ordered = sorted(losses)
    n = len(ordered)
    var = ordered[ceil(confidence * n) - 1]
    mass = (1 - confidence) * n
    remaining = mass
    total = 0.0
    for loss in reversed(ordered):
        weight = min(1.0, remaining)
        total += weight * loss
        remaining -= weight
        if remaining <= 1e-12:
            break
    return max(0.0, var), max(0.0, total / mass)

def portfolio_risk(state, provider, confidence=.95, lookback=252, benchmark='balanced', alignment_ids=None):
    if confidence not in (.95, .99) or lookback not in (63, 126, 252) or benchmark not in BENCHMARKS:
        raise ValueError('Unsupported risk settings.')
    book = summarize(state, provider)
    if not isfinite(book['nav']) or book['nav'] <= 0:
        raise ValueError('Positive finite portfolio NAV is required for risk analysis.')
    weights = {p['id']: p['value'] / book['nav'] for p in book['positions']}
    benchmark_weights = {'SPX': .6, 'IG': .4} if benchmark == 'balanced' else {benchmark: 1.0}
    required = set(weights) | set(benchmark_weights) | set(alignment_ids or [])
    histories = {}
    for key in required:
        bars = provider.series(key)
        dates = [b['date'] for b in bars if b['date'] <= AS_OF[:10]]
        if len(dates) != len(set(dates)):
            raise ValueError(f'Duplicate historical dates for {key}.')
        history = {b['date']: b['close'] for b in bars if b['date'] <= AS_OF[:10]}
        if not history or any(not isfinite(v) or v <= 0 for v in history.values()):
            raise ValueError(f'Valid positive historical marks are required for {key}.')
        histories[key] = history
    # Use adjacent dates on the union calendar only when EVERY required series
    # has both endpoints. Missing observations never become zero returns or
    # accidentally turn a multi-session move into a one-day observation.
    calendar = sorted(set().union(*(set(h) for h in histories.values())))
    observations = []
    for previous, current in zip(calendar, calendar[1:]):
        if not all(previous in h and current in h for h in histories.values()):
            continue
        returns = {key: h[current] / h[previous] - 1 for key, h in histories.items()}
        observations.append((previous, current,
            sum(weight * returns[key] for key, weight in weights.items()),
            sum(weight * returns[key] for key, weight in benchmark_weights.items())))
    if len(observations) < lookback:
        raise ValueError(f'Insufficient aligned history: {len(observations)} daily observations available; {lookback} required.')
    observations = observations[-lookback:]
    portfolio_returns = [row[2] for row in observations]
    active = [row[2] - row[3] for row in observations]
    var, es = tail_loss([-r for r in portfolio_returns], confidence)
    nav = benchmark_nav = peak = 100.0
    series = [{'date': observations[0][0], 'portfolio': 100.0, 'benchmark': 100.0, 'drawdown_pct': 0.0}]
    maximum_drawdown = 0.0
    for _, current, r, b in observations:
        if r <= -1 or b <= -1:
            raise ValueError('Historical returns imply a nonpositive portfolio index.')
        nav *= 1 + r
        benchmark_nav *= 1 + b
        peak = max(peak, nav)
        drawdown = nav / peak - 1
        maximum_drawdown = min(maximum_drawdown, drawdown)
        series.append({'date': current, 'portfolio': nav, 'benchmark': benchmark_nav, 'drawdown_pct': drawdown * 100})
    return {
        'confidence': confidence, 'lookback': lookback, 'observations': len(observations),
        'horizon_days': 1, 'annualization': 252, 'benchmark_id': benchmark,
        'benchmark_name': BENCHMARKS[benchmark], 'nav': book['nav'], 'cash_weight': book['cash'] / book['nav'],
        'var_pct': var * 100, 'var_amount': var * book['nav'],
        'expected_shortfall_pct': es * 100, 'expected_shortfall_amount': es * book['nav'],
        'max_drawdown_pct': maximum_drawdown * 100, 'current_drawdown_pct': series[-1]['drawdown_pct'],
        'tracking_error_pct': stdev(active) * sqrt(252) * 100,
        'start_date': series[0]['date'], 'end_date': series[-1]['date'],
        'tail_observations': (1 - confidence) * len(observations),
        'weights': weights, 'series': series, 'snapshot': AS_OF, 'quality': 'synthetic',
        'methodology': 'Historical simulation of current portfolio weights, held constant daily; cash earns zero. Empirical inverse-CDF VaR; expected shortfall integrates the worst (1-confidence) probability mass with fractional boundary weighting. Tracking error is sample standard deviation of daily active returns, annualized by sqrt(252). Drawdowns are measured from the compounded hypothetical index high-water mark.',
        'limitations': 'Synthetic price returns, not realized account performance. No coupons, dividends, financing, fees or FX conversion. Daily constant weights imply hypothetical rebalancing without costs. Missing dates are excluded jointly, never filled with zero. Limited tail observations make extreme-loss estimates uncertain.',
    }
