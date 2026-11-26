from datetime import datetime, timezone
from math import sqrt

import pytest

from .commodity_option_models import (
    bootstrap_term, calibrate_expiry, implied_volatility, option_premium, risk_neutral_distribution,
)
from .commodity_options import ALPACA_ROOT, CMEOptions, CommodityOptionsService, etf_option_quote


@pytest.mark.parametrize('forward,strike,vol,black', [(-1, 0, 18, False), (80, 84, 0.43, True)])
def test_inverts_premium_in_supported_coordinate_systems(forward, strike, vol, black):
    years = 0.35
    premium = option_premium(forward, strike, vol * sqrt(years), 'call', black)
    assert implied_volatility(premium, forward, strike, years, 'call', black) == pytest.approx(vol)
    if forward <= 0:
        assert implied_volatility(premium, forward, strike, years, 'call', black=True) is None


def test_quadratic_smile_and_price_diagnostics():
    forward, years = 80, 0.25
    quotes = {}
    for strike in range(65, 96):
        x = strike - forward
        variance_vol = 22 + 0.1 * x + 0.02 * x * x
        side = 'put' if x < 0 else 'call'
        quotes[strike] = {side: {'price': option_premium(forward, strike, variance_vol * sqrt(years), side)}}
    result = calibrate_expiry('2027-03-17', 'LOJ27', forward, years, quotes, 0)
    assert result['atm_normal_iv'] == pytest.approx(22)
    assert result['fit']['curvature'] == pytest.approx(0.02)
    assert result['fit']['rmse'] == pytest.approx(0, abs=1e-10)
    point = next(point for point in result['points'] if point['strike'] == 85)
    assert point['local_vol'] == pytest.approx(22 + 0.2 * 5 + 0.06 * 25)


def test_bootstrap_keeps_negative_intervals_visible():
    slices = [
        {'expiry': '2027-01-01', 'contract': 'LO', 'years': 0.1, 'days': 36.5, 'iv': 30},
        {'expiry': '2027-01-19', 'contract': 'LO', 'years': 0.15, 'days': 54.75, 'iv': 20},
    ]
    first, second = bootstrap_term(slices, 'iv')
    assert first['local_vol'] == pytest.approx(30)
    assert second['local_variance'] == pytest.approx(-600)
    assert second['negative_variance']
    assert second['local_vol'] is None


def test_unequal_strike_butterflies_preserve_covered_mass():
    points = [{'strike': x, 'call_equivalent': y} for x, y in [(0, 5), (1, 4.2), (3, 3), (4, 2.5), (6, 2.1)]]
    distribution = risk_neutral_distribution(points)
    assert distribution['covered_mass'] == pytest.approx(0.6)
    assert distribution['butterflies'][0]['density'] == pytest.approx(0.2 / 1.5)
    assert distribution['cdf'][0]['probability'] == pytest.approx(0.2)
    assert distribution['cdf'][-1]['probability'] == pytest.approx(0.8)
    points[2]['call_equivalent'] = 4.4
    repaired = risk_neutral_distribution(points)
    assert repaired['violating_butterflies'] > 0
    assert repaired['adjusted_intervals'] > 0
    assert all(point['density'] >= 0 for point in repaired['butterflies'])


def test_cme_uses_last_trade_date_and_same_month_same_session_settlement():
    requests = []

    def get(path, params=None):
        requests.append((path, params))
        if path == '/ProductCalendar/Options/190':
            return [{'calendarEntries': [{'contractMonth': 'Jan 2027', 'productCode': 'LOF27',
                                          'lastTrade': '16 Dec 2026'}]}]
        if path.endswith('/425/FUT'):
            rows = [{'month': 'JAN 27', 'settle': '80.00'}] if params['tradeDate'] == '11/24/2026' else []
        else:
            assert path.endswith('/190/OOF')
            assert params['monthYear'] == 'LOF27'
            rows = [{'strike': '80.00', 'type': 'Call', 'settle': '4.20'},
                    {'strike': '160.00', 'type': 'Call', 'settle': '0.01'}]
        return {'tradeDate': params['tradeDate'], 'reportType': 'Final', 'updateTime': 'version', 'settlements': rows}

    service = CommodityOptionsService(None, cme=CMEOptions(get),
                                      clock=lambda: datetime(2026, 11, 25, tzinfo=timezone.utc))
    result = service.view('wti')
    assert result['as_of'] == '2026-11-24'
    assert result['slices'][0]['expiry'] == '2026-12-16'
    assert result['slices'][0]['days'] == 22
    assert [point['strike'] for point in result['slices'][0]['points']] == [80]
    assert requests[-1][1]['tradeDate'] == '11/24/2026'


def test_etf_quote_rejects_adjusted_symbol_wrong_underlying_and_crossed_quote():
    stamp = '2026-11-24T20:59:59.913959117Z'
    quote = {'latestQuote': {'bp': 5, 'ap': 6, 't': stamp}}
    assert etf_option_quote('USO', 'USO1261204C00135000', quote) is None
    assert etf_option_quote('BNO', 'USO261204C00135000', quote) is None
    assert etf_option_quote('UNG', 'UNG261204C00135000',
                            {'latestQuote': {'bp': 6, 'ap': 5, 't': stamp}}) is None
    row = etf_option_quote('GLD', 'GLD261204C00135000', quote)
    assert row[1:3] == (135, 'call')
    assert row[-1]['price'] == 5.5


@pytest.mark.parametrize('market,symbol', [('uso', 'USO'), ('brent', 'BNO'), ('xau', 'GLD'), ('natgas', 'UNG')])
def test_etf_markets_calibrate_their_own_paginated_option_chain(market, symbol):
    stamp = '2026-11-25T16:00:00Z'
    underlying = 50
    requests = []

    class Alpaca:
        def get(self, host, path, params):
            assert host == ALPACA_ROOT
            requests.append((path, dict(params)))
            if path == '/v2/stocks/snapshots':
                assert params == {'symbols': symbol, 'feed': 'iex'}
                return {symbol: {'latestTrade': {'p': underlying, 't': stamp}}}
            assert path == f'/v1beta1/options/snapshots/{symbol}'
            assert params['feed'] == 'indicative'
            assert params['root_symbol'] == symbol
            second_page = 'page_token' in params
            expiry, deviation = ('270115', 4) if second_page else ('261218', 3)
            rows = {}
            for strike in range(46, 55):
                for side in ('call', 'put'):
                    premium = option_premium(underlying, strike, deviation, side)
                    contract = f'{symbol}{expiry}{side[0].upper()}{strike * 1000:08d}'
                    rows[contract] = {'latestQuote': {'bp': premium - 0.01, 'ap': premium + 0.01, 't': stamp}}
            return {'snapshots': rows, 'next_page_token': None if second_page else 'next'}

    service = CommodityOptionsService(Alpaca(), clock=lambda: datetime(2026, 11, 25, 16, 1, tzinfo=timezone.utc))
    result = service.view(market)
    assert result['status'] == 'ready'
    assert result['symbol'] == symbol
    assert result['instrument_kind'] == 'etf'
    assert result['unit'] == 'USD/share'
    assert len(result['term']['normal']) == 2
    assert len(result['term']['black']) == 2
    assert all(row['contract'] == symbol and row['forward'] == pytest.approx(underlying)
               and row['fit'] and row['distribution'] for row in result['slices'])
    assert len(requests) == 3
    assert requests[-1][1]['page_token'] == 'next'
