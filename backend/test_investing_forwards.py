from datetime import datetime, timedelta, timezone
import json

import pytest

from .fx_carry_risk import FXCarryRiskService
from .investing_forwards import PAIRS, InvestingForwardSource, parse_page, source_url, validate_quote
from .test_fx_carry_risk import Source

NOW = datetime(2026, 10, 31, 0, 5, tzinfo=timezone.utc)


def page(pair='EUR/USD', clock='00:04:00', bid='100', ask='110', at=NOW):
    spot = '150' if pair == 'USD/JPY' else '1.1'
    return f'''<script>window.timezoneOffset = +"0";</script>
      <span class="arial_26 pid-1-last" id="last_last">{spot}</span>
      <span class="pid-1-bid">{spot}</span><span class="pid-1-ask">{spot}</span>
      <span class="pid-1-time">{clock}</span>
      <table><tr><td id="pair_name_99">{pair.replace('/', '')}&nbsp;1Y&nbsp;FWD</td>
      <td id="bid_99">{bid}</td><td id="ask_99">{ask}</td>
      <td id="time_99" data-value="{int(at.timestamp())}">00:05:00</td></tr></table>'''


def test_actual_points_are_converted_by_currency_and_carry_direction():
    eur = validate_quote(parse_page(page(), 'EUR/USD', NOW, NOW), NOW)
    assert eur['forward_12m'] == pytest.approx(1.1105)
    assert eur['usd_carry_pct'] == pytest.approx((1.1105 / 1.1 - 1) * 100)
    assert eur['pair_carry_pct'] == pytest.approx((1.1 / 1.1105 - 1) * 100)
    jpy = validate_quote(parse_page(page('USD/JPY', bid='-450', ask='-440'), 'USD/JPY', NOW, NOW), NOW)
    assert jpy['forward_12m'] == pytest.approx(145.55)
    assert jpy['usd_carry_pct'] == jpy['pair_carry_pct'] > 0
    assert eur['implied_volatility_pct'] is None and eur['iv_time'] is None
    assert eur['forward_date'] is None and eur['spot_date'] is None


def test_midnight_and_weekend_precision_does_not_fabricate_spot_times():
    midnight = parse_page(page(clock='23:59:00'), 'EUR/USD', NOW, NOW)
    assert midnight['spot_time'] == '2026-10-30T23:59:00+00:00'
    weekend = parse_page(page(clock='30/10'), 'EUR/USD', NOW, NOW)
    assert weekend['spot_time'] is None and weekend['spot_observation_date'] == '2026-10-30'
    assert validate_quote(weekend, NOW)['spot_time_precision'] == 'day'
    january = datetime(2027, 1, 1, 0, 5, tzinfo=timezone.utc)
    assert parse_page(page(clock='31/12', at=january), 'EUR/USD', january, january)['spot_observation_date'] == '2026-12-31'


@pytest.mark.parametrize('change', [
    lambda body: body.replace('1Y', '1M'),
    lambda body: body.replace('EURUSD', 'GBPUSD'),
    lambda body: body.replace('timezoneOffset = +"0"', 'timezoneOffset = +"8"'),
    lambda body: body.replace('data-value="', 'data-other="'),
    lambda body: body.replace('>100</td>', '>NaN</td>'),
    lambda body: body.replace('>100</td>', '>111</td>'),
    lambda body: body.replace('>00:04:00</span>', '>unknown</span>'),
    lambda body: body + '<td id="bid_99">100</td>',
    lambda body: '<html>Access denied</html>',
])
def test_changed_schema_wrong_tenor_units_or_invalid_prices_are_rejected(change):
    with pytest.raises((ValueError, KeyError, TypeError)):
        parse_page(change(page()), 'EUR/USD', NOW, NOW)


def test_future_quotes_and_cached_numeric_corruption_rejected():
    with pytest.raises(ValueError):
        parse_page(page(at=NOW + timedelta(hours=1)), 'EUR/USD', NOW, NOW)
    with pytest.raises(ValueError):
        parse_page(page(), 'EUR/USD', NOW - timedelta(hours=1), NOW)
    raw = parse_page(page(), 'EUR/USD', NOW, NOW)
    raw['spot_bid'] = float('nan')
    with pytest.raises(ValueError): validate_quote(raw, NOW)


def test_all_pairs_partial_outage_cache_recovery_and_regression(tmp_path):
    now = [NOW]; fail = set(); calls = []
    def transport(url):
        pair = next(pair for pair in PAIRS if source_url(pair) == url)
        calls.append(pair)
        if pair in fail: raise ValueError('private transport detail')
        return page(pair, at=now[0]), now[0]
    source = InvestingForwardSource(tmp_path / 'quotes.json', transport=transport)
    first = source.refresh(now[0])
    assert len(first['quotes']) == len(calls) == 9 and not first['errors']
    first_time = first['quotes']['EUR/USD']['quote_time']
    fail.add('EUR/USD'); now[0] += timedelta(minutes=1)
    second = source.refresh(now[0])
    assert second['quotes']['EUR/USD']['quote_time'] == first_time
    assert second['checks']['EUR/USD'] == first['checks']['EUR/USD']
    assert second['checks']['GBP/USD'] != first['checks']['GBP/USD']
    assert list(second['errors']) == ['EUR/USD']
    assert 'private' not in json.dumps(second)
    restored = InvestingForwardSource(source.path, transport=transport)
    assert restored.snapshot(now[0])['quotes'] == second['quotes']
    fail.clear(); old = now[0]; now[0] -= timedelta(minutes=2)
    assert len(source.refresh(now[0])['errors']) == 9
    now[0] = old + timedelta(minutes=1)
    assert not source.refresh(now[0])['errors']


def test_carry_service_plots_forwards_without_options_and_updates_each_minute(tmp_path, monkeypatch):
    monkeypatch.delenv('FX_CARRY_FEED_URL', raising=False)
    monkeypatch.setenv('FX_SAXO_REPORT_ENABLED', 'false')
    now = [NOW]; calls = []; forward_bid = ['100']
    def transport(url):
        pair = next(pair for pair in PAIRS if source_url(pair) == url)
        calls.append(pair)
        return page(pair, bid=forward_bid[0], ask=str(float(forward_bid[0])+10), at=now[0]), now[0]
    forward_source = InvestingForwardSource(tmp_path / 'forwards.json', transport=transport)
    service = FXCarryRiskService(tmp_path / 'risk.json', clock=lambda: now[0], transport=Source(),
                                 api_key='', forward_source=forward_source)
    assert not service.view(saved_only=True)['rows'][0]['usd_carry_pct'] and not calls
    first = service.view()
    assert first['forward_feed_configured'] and not first['feed_configured']
    assert len(calls) == 9 and first['status'] == 'partial'
    assert all(row['usd_carry_pct'] is not None and row['risk_correlation'] is not None for row in first['rows'])
    assert all(row['implied_volatility_pct'] is None for row in first['rows'])
    now[0] += timedelta(seconds=59)
    service.view(); assert len(calls) == 9
    forward_bid[0] = '200'
    now[0] += timedelta(seconds=1)
    second = service.view()
    assert len(calls) == 18 and second['rows'][0]['quote_time'] != first['rows'][0]['quote_time']
    assert second['rows'][0]['usd_carry_pct'] != first['rows'][0]['usd_carry_pct']
    assert second['rows'][0]['forward_12m'] == pytest.approx(1.1205)
    assert second['rows'][0]['forward_date'] is None
    assert 'settlement dates are not published' in second['methodology']


def test_weekend_quotes_are_stale_even_after_successful_check(tmp_path, monkeypatch):
    monkeypatch.setenv('FX_SAXO_REPORT_ENABLED', 'false')
    monkeypatch.delenv('FX_CARRY_FEED_URL', raising=False)
    source = InvestingForwardSource(tmp_path / 'forwards.json',
        transport=lambda url: (page(next(p for p in PAIRS if source_url(p) == url), clock='30/10'), NOW))
    service = FXCarryRiskService(tmp_path / 'risk.json', clock=lambda: NOW, transport=Source(), api_key='', forward_source=source)
    result = service.view()
    assert result['status'] == 'stale' and all(row['stale'] for row in result['rows'])
    assert all(row['spot_time'] is None for row in result['rows'])
