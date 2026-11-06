from datetime import datetime, timedelta, timezone
import json
from threading import Event

import pytest

from .fx_volatility import FXVolatilitySource, SaxoReportSource, parse_saxo

NOW = datetime(2026,10,30,12,30,tzinfo=timezone.utc)


def report(day='30 Oct 2026', value='8.5', tenor='1y'):
    return (f'<div class="blogTitle">{day}, 1230 GMT</div><div>ATM VOLATILITIES</div>'
            f'<table><tr><th></th><th>Pair</th><th>Spot</th><th>1w</th><th>1m</th><th>3m</th><th>6m</th><th>9m</th><th>{tenor}</th><th></th></tr>'
            f'<tr><td></td><td>EURUSD</td><td>1.1 (+1)</td><td>1 (+9)</td><td>2 (+8)</td><td>3 (+7)</td><td>4 (+6)</td><td>5 (+5)</td><td>{value} (-0.1)</td></tr></table>'
            '<div>25-DELTA RISK REVERSAL</div><table><tr><td>EURUSD</td><td>999</td></tr></table>')


def test_saxo_reads_exact_tenor_not_change_or_risk_reversal():
    quote = parse_saxo(report(),NOW)['EUR/USD']
    assert quote['implied_volatility_pct'] == 8.5
    assert quote['iv_time'] == NOW.isoformat()
    assert quote['iv_delivery'] == 'daily_report' and 'iv_backup' not in quote
    assert quote['iv_basis'] == 'OTC quoted ATM'
    assert parse_saxo(report(),NOW,'1M')['EUR/USD']['implied_volatility_pct'] == 2
    assert 'USD/SEK' not in parse_saxo(report(),NOW)


@pytest.mark.parametrize('body', [report(day='31 Oct 2026'),report(value='NaN'),report(value='0'),
                                report(value='400'),report(tenor='12m'),report().replace('GMT','SGT'),
                                report().replace('ATM VOLATILITIES','HISTORIC VOLATILITIES')])
def test_saxo_rejects_future_wrong_section_schema_and_units(body):
    with pytest.raises(ValueError): parse_saxo(body,NOW)


def test_one_minute_checks_update_values_and_preserve_publication_time(tmp_path):
    body = [report()]; calls = []
    def get(): calls.append(1); return body[0]
    source = SaxoReportSource(tmp_path/'saxo.json',transport=get)
    source.refresh(NOW); assert len(calls) == 1
    source.refresh(NOW+timedelta(seconds=59)); assert len(calls) == 1
    body[0] = report(value='9.5')
    source.refresh(NOW+timedelta(seconds=60))
    assert len(calls) == 2 and source.quotes['EUR/USD']['implied_volatility_pct'] == 9.5
    assert source.quotes['EUR/USD']['iv_time'] == NOW.isoformat()
    source.refresh(NOW+timedelta(seconds=120))
    assert source.checked == (NOW+timedelta(seconds=120)).isoformat()
    assert source.quotes['EUR/USD']['iv_time'] == NOW.isoformat()


def test_new_report_outage_regression_and_restart_keep_correct_dates(tmp_path):
    body = [report()]; now = NOW
    def get():
        if body[0] is None: raise ValueError('PRIVATE_KEY must not escape')
        return body[0]
    path = tmp_path/'saxo.json'; source = SaxoReportSource(path,transport=get)
    source.refresh(now)
    now += timedelta(days=1); body[0] = report(day='31 Oct 2026',value='10.5'); source.refresh(now)
    quote = source.quotes['EUR/USD'].copy()
    assert quote['implied_volatility_pct'] == 10.5 and quote['iv_time'] == now.isoformat()
    body[0] = report(); source.refresh(now+timedelta(seconds=60))
    assert source.quotes['EUR/USD'] == quote and source.error
    body[0] = None; source.refresh(now+timedelta(seconds=120))
    assert source.quotes['EUR/USD'] == quote and 'PRIVATE_KEY' not in json.dumps(source.snapshot(now))
    recovered = SaxoReportSource(path,transport=lambda:pytest.fail('Saved read must not fetch'))
    assert recovered.snapshot(now+timedelta(seconds=180))['quotes'] == source.quotes
    assert recovered.checked == now.isoformat()
    body[0] = report(day='31 Oct 2026',value='10.6'); source.refresh(now+timedelta(seconds=180))
    assert source.error is None and source.quotes['EUR/USD']['implied_volatility_pct'] == 10.6


def test_background_checks_receive_a_new_report_without_browser_requests(tmp_path):
    now = [NOW]; body = [report()]; waiting = Event(); release = Event(); updated = Event(); waits = []
    source = FXVolatilitySource(tmp_path/'unused.json',clock=lambda:now[0],
                                report=SaxoReportSource(tmp_path/'saxo.json',transport=lambda:body[0]))
    def scheduled_wait(seconds):
        assert seconds == 60
        waits.append(1)
        if len(waits) == 1:
            waiting.set(); assert release.wait(3)
            return False
        updated.set(); return True
    source.stop.wait = scheduled_wait
    try:
        source.start(); source.start()
        assert waiting.wait(3)
        assert source.snapshot(now[0])['quotes']['EUR/USD']['implied_volatility_pct'] == 8.5
        body[0] = report(day='31 Oct 2026',value='9.75'); now[0] += timedelta(days=1); release.set()
        assert updated.wait(3)
        snapshot = source.snapshot(now[0])
        assert snapshot['quotes']['EUR/USD']['implied_volatility_pct'] == 9.75
        assert snapshot['report_published_at'] == now[0].isoformat()
        assert 'primary' not in snapshot and snapshot['poll_interval_seconds'] == 60
    finally:
        release.set(); source.close()
    assert not source.thread.is_alive()


def test_disabled_report_does_not_start_or_fetch(tmp_path):
    source = FXVolatilitySource(tmp_path/'saxo.json',enabled=False)
    source.start(); source.refresh(NOW)
    assert source.thread is None and source.snapshot(NOW)['quotes'] == {}
    assert source.snapshot(NOW)['status'] == 'disabled'


def test_carry_service_new_report_updates_iv_without_changing_forward_inputs(tmp_path,monkeypatch):
    from .fx_carry_risk import FXCarryRiskService
    from .test_fx_carry_risk import Source
    monkeypatch.setenv('FX_INVESTING_FORWARDS_ENABLED','false')
    now = [NOW]; body = [report()]
    volatility = FXVolatilitySource(tmp_path/'unused.json',report=SaxoReportSource(tmp_path/'saxo.json',transport=lambda:body[0]))
    class Forwards:
        def refresh(self,now): pass
        def snapshot(self,now):
            return {'quotes':{'EUR/USD':{'usd_carry_pct':1,'pair_carry_pct':-1,'quote_time':NOW.isoformat(),
                                         'source':'Test forwards','spot':1.1,'forward_12m':1.111}},'checks':{},'errors':{}}
    service = FXCarryRiskService(tmp_path/'risk.json',transport=Source(),clock=lambda:now[0],api_key='',
                                 forward_source=Forwards(),volatility_source=volatility)
    first = service.view(); row = first['rows'][0]
    assert row['iv_delivery'] == 'daily_report' and row['implied_volatility_pct'] == 8.5
    assert row['source'] == 'Test forwards' and row['pair_carry_pct'] == -1 and row['spot'] == 1.1
    now[0] += timedelta(days=1); body[0] = report(day='31 Oct 2026',value='9.75')
    second = service.view(); changed = second['rows'][0]
    assert changed['implied_volatility_pct'] == 9.75 and changed['iv_time'] == now[0].isoformat()
    assert changed['pair_carry_pct'] == row['pair_carry_pct'] and changed['quote_time'] == row['quote_time']
    assert not changed['iv_stale']
    assert service.view(saved_only=True)['rows'] == second['rows']
    now[0] += timedelta(days=8)
    assert service.view(saved_only=True)['rows'][0]['iv_stale']
    assert all('tastytrade' not in json.dumps(source).lower() for source in second['sources'])
