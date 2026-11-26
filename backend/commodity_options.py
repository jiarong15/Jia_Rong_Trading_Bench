"""Free market-data connectors for the commodity option calibration workspace."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
from math import exp, isfinite
import re
from statistics import median
from threading import Lock
from time import monotonic
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .commodity_option_models import bootstrap_term, calibrate_expiry


CME_ROOT = 'https://www.cmegroup.com/CmeWS/mvc'
CME_PAGE = 'https://www.cmegroup.com/markets/energy/crude-oil/light-sweet-crude.settlements.options.html'
ALPACA_ROOT = 'https://data.alpaca.markets'
ALPACA_DOCS = 'https://docs.alpaca.markets/us/reference/optionchain'
MONTHS = dict(zip('JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC'.split(), range(1, 13)))
ETF_CONTRACT = re.compile(r'([A-Z]+)(\d{6})([CP])(\d{8})')


def etf_feed(symbol):
    return {'source': 'Alpaca Market Data', 'cadence': 'Intraday indicative', 'unit': 'USD/share',
            'url': ALPACA_DOCS, 'poll_seconds': 60, 'exercise': 'American', 'series': f'{symbol} ETF',
            'symbol': symbol, 'instrument_kind': 'etf'}


FEEDS = {
    'wti': {'source': 'CME Group', 'cadence': 'Daily settlement', 'unit': 'USD/bbl', 'url': CME_PAGE,
            'poll_seconds': 300, 'exercise': 'American', 'series': 'LO / CL',
            'symbol': 'CL', 'instrument_kind': 'futures'},
    'wti-european': {'source': 'CME Group', 'cadence': 'Daily settlement', 'unit': 'USD/bbl', 'url': CME_PAGE,
                     'poll_seconds': 300, 'exercise': 'European', 'series': 'LC / CL',
                     'symbol': 'CL', 'instrument_kind': 'futures'},
    'uso': etf_feed('USO'),
    'brent': etf_feed('BNO'),
    'xau': etf_feed('GLD'),
    'natgas': etf_feed('UNG'),
}


def number(value):
    try:
        parsed = float(str(value).replace(',', ''))
    except (ValueError, TypeError):
        return None
    return parsed if isfinite(parsed) else None


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def month_key(value):
    parts = str(value).upper().split()
    if len(parts) != 2 or parts[0] not in MONTHS or not parts[1].isdigit():
        return None
    return int(parts[1]) % 100, MONTHS[parts[0]]


def cme_json(path, params=None):
    query = '?' + urlencode(params, safe='/') if params else ''
    request = Request(CME_ROOT + path + query, headers={
        'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json, */*',
    })
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def cme_chain(payload):
    quotes = defaultdict(dict)
    for row in payload.get('settlements') or []:
        strike, premium = number(row.get('strike')), number(row.get('settle'))
        side = str(row.get('type')).lower()
        # One-tick settlements are censored by the exchange tick size in the wings.
        if strike is not None and premium is not None and premium > 0.01 and side in ('call', 'put'):
            quotes[strike][side] = {'price': premium}
    return dict(quotes)


class CMEOptions:
    def __init__(self, get=cme_json):
        self.get = get
        self.calendars = {}
        self.lock = Lock()

    def calendar(self, product):
        with self.lock:
            cached = self.calendars.get(product)
            if cached and monotonic() - cached[0] < 86400:
                return cached[1]
            payload = self.get(f'/ProductCalendar/Options/{product}')
            if not isinstance(payload, list) or not payload:
                raise ValueError('CME did not publish the option contract calendar.')
            records = []
            for group in payload:
                for item in group.get('calendarEntries') or []:
                    month = month_key(item.get('contractMonth'))
                    if not month or not item.get('lastTrade') or not item.get('productCode'):
                        continue
                    expiry = datetime.strptime(item['lastTrade'], '%d %b %Y').date()
                    records.append((expiry, month, item['productCode']))
            if not records:
                raise ValueError('CME did not publish any dated option contracts.')
            self.calendars[product] = monotonic(), sorted(records)
            return self.calendars[product][1]

    def load(self, market, now, previous=None):
        product = 190 if market == 'wti' else 1353
        future_months = {}
        for offset in range(8):
            session = now.date() - timedelta(days=offset)
            if session.weekday() >= 5:
                continue
            query_date = session.strftime('%m/%d/%Y')
            payload = self.get('/Settlements/Futures/Settlements/425/FUT', {'tradeDate': query_date, 'pageSize': 100})
            if payload.get('tradeDate') != query_date or payload.get('reportType') != 'Final':
                continue
            future_months = {month_key(row.get('month')): number(row.get('settle'))
                             for row in payload.get('settlements') or []}
            if any(month and price is not None for month, price in future_months.items()):
                break
        else:
            raise ValueError('CME did not return a recent final WTI futures settlement.')
        version = payload.get('updateTime')
        if (previous and version and previous['as_of'] == session.isoformat()
                and previous.get('provider_version') == version
                and all(item['expiry'] > now.date().isoformat() for item in previous['slices'])):
            return previous
        contracts = [(expiry, month, code) for expiry, month, code in self.calendar(product)
                     if expiry > max(session, now.date()) and future_months.get(month) is not None][:8]
        if not contracts:
            raise ValueError('CME did not return any expiries matched to WTI futures settlements.')

        def chain(item):
            expiry, month, code = item
            settle_code = ('LO' if product == 190 else 'LC') + code[-3:]
            params = {'tradeDate': query_date, 'monthYear': settle_code, 'strategy': 'DEFAULT', 'pageSize': 1000}
            try:
                result = self.get(f'/Settlements/Options/Settlements/{product}/OOF', params)
            except (OSError, ValueError) as exc:
                return None, str(exc)
            if result.get('tradeDate') != query_date or result.get('reportType') != 'Final':
                return None, 'No matched final option settlement for this expiry.'
            quotes = cme_chain(result)
            if not quotes:
                return None, 'No eligible settlement premiums for this expiry.'
            return {
                'expiry': expiry.isoformat(), 'contract': f'{settle_code} / CL{settle_code[-3:]}',
                'years': (expiry - session).days / 365, 'forward': future_months[month], 'quotes': quotes,
            }, None

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(chain, contracts))
        slices = [item for item, _ in results if item]
        missing = [code for (_, _, code), (item, _) in zip(contracts, results) if item is None]
        if not slices:
            error = next((issue for _, issue in results if issue), 'Missing option settlements.')
            raise ValueError(f'CME did not return eligible WTI option settlements. {error}')
        return {'as_of': session.isoformat(), 'provider_version': version, 'slices': slices, 'missing': missing}


def etf_option_quote(symbol, contract, row):
    match = ETF_CONTRACT.fullmatch(contract)
    if not match or match[1] != symbol:
        return None
    _, expiry, code, strike = match.groups()
    quote = row.get('latestQuote') or {}
    bid, ask, stamp = number(quote.get('bp')), number(quote.get('ap')), timestamp(quote.get('t'))
    if stamp is None or bid is None or ask is None or not 0 < bid <= ask:
        return None
    mid = (bid + ask) / 2
    if ask - bid > max(0.10, mid * 0.55):
        return None
    return (datetime.strptime(expiry, '%y%m%d').date(), int(strike) / 1000,
            'call' if code == 'C' else 'put', stamp,
            {'price': mid, 'bid': bid, 'ask': ask, 'quote_time': stamp.isoformat()})


def etf_expiry_time(expiry):
    march, november = date(expiry.year, 3, 1), date(expiry.year, 11, 1)
    start = march + timedelta(days=(6 - march.weekday()) % 7 + 7)
    end = november + timedelta(days=(6 - november.weekday()) % 7)
    hour = 20 if start <= expiry < end else 21
    return datetime(expiry.year, expiry.month, expiry.day, hour, tzinfo=timezone.utc)


def load_etf_options(alpaca, symbol, now):
    stock = alpaca.get(ALPACA_ROOT, '/v2/stocks/snapshots', {'symbols': symbol, 'feed': 'iex'}).get(symbol) or {}
    trade = stock.get('latestTrade') or {}
    spot = number(trade.get('p')) or number((stock.get('dailyBar') or {}).get('c'))
    if spot is None or spot <= 0:
        raise ValueError(f'Alpaca did not return a usable {symbol} reference price.')
    params = {
        'feed': 'indicative', 'root_symbol': symbol, 'limit': 1000,
        'expiration_date_gte': now.date().isoformat(),
        'expiration_date_lte': (now.date() + timedelta(days=240)).isoformat(),
        'strike_price_gte': round(spot * 0.52, 2), 'strike_price_lte': round(spot * 1.60, 2),
    }
    quotes, seen = [], set()
    for _ in range(10):
        result = alpaca.get(ALPACA_ROOT, f'/v1beta1/options/snapshots/{symbol}', params)
        snapshots = result.get('snapshots') or {}
        quotes.extend(quote for contract, row in snapshots.items()
                      if (quote := etf_option_quote(symbol, contract, row)))
        token = result.get('next_page_token')
        if not token:
            break
        if token in seen:
            raise ValueError(f'Alpaca repeated a {symbol} option chain page.')
        seen.add(token)
        params['page_token'] = token
    else:
        raise ValueError(f'Alpaca returned more {symbol} option chain pages than expected.')
    quotes = [quote for quote in quotes if quote[3] <= now + timedelta(minutes=1)]
    if not quotes:
        raise ValueError(f'Alpaca did not return eligible two-sided {symbol} option quotes.')

    as_of = max(quote[3] for quote in quotes)
    cutoff = as_of - timedelta(minutes=45)
    grouped = defaultdict(lambda: defaultdict(dict))
    for expiry, strike, side, stamp, quote in quotes:
        if cutoff <= stamp and stamp.date() == as_of.date():
            grouped[expiry][strike][side] = quote
    slices = []
    for expiry, strikes in sorted(grouped.items()):
        expiry_at = etf_expiry_time(expiry)
        if expiry_at <= now:
            continue
        years = (expiry_at - as_of).total_seconds() / (365 * 86400)
        pairs = [strike for strike, sides in strikes.items() if 'call' in sides and 'put' in sides
                 and abs(strike - spot) <= spot * 0.12]
        pairs.sort(key=lambda strike: abs(strike - spot))
        if len(pairs) >= 3 and years > 0:
            slices.append({
                'expiry': expiry.isoformat(), 'contract': symbol, 'years': years,
                'quotes': dict(strikes), 'parity_strikes': pairs[:7],
            })
    if not slices:
        raise ValueError(f'{symbol} quotes are too sparse to infer a forward from matched strikes.')
    return {'as_of': as_of.isoformat(), 'slices': slices, 'missing': [], 'reference_price': spot,
            'reference_time': trade.get('t')}


class CommodityOptionsService:
    def __init__(self, alpaca, enabled=True, cme=None, clock=None):
        self.alpaca = alpaca
        self.enabled = enabled
        self.cme = cme or CMEOptions()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.cache = {}
        self.checked = {}
        self.locks = {market: Lock() for market in FEEDS}

    def view(self, market, rate_pct=0, refresh=False):
        feed = FEEDS[market]
        is_etf = feed['instrument_kind'] == 'etf'
        now = self.clock()
        with self.locks[market]:
            checked, last_checked, error = self.checked.get(market, (-float('inf'), None, None))
            age = monotonic() - checked
            period = feed['poll_seconds'] if error is None else min(feed['poll_seconds'], 60)
            if self.enabled and (age >= period or (refresh and age >= 15)):
                try:
                    previous = None if refresh or error else self.cache.get(market)
                    self.cache[market] = (load_etf_options(self.alpaca, feed['symbol'], now) if is_etf
                                          else self.cme.load(market, now, previous))
                    error = None
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    error = f'{feed["source"]} refresh failed. {exc}'
                last_checked = self.clock().isoformat()
                self.checked[market] = monotonic(), last_checked, error
            snapshot = self.cache.get(market)
        result = {**feed, 'market': market, 'rate_pct': rate_pct, 'checked_at': last_checked,
                  'as_of': None, 'status': 'unavailable', 'reason': error, 'slices': [],
                  'term': {'normal': [], 'black': []}, 'missing': []}
        if snapshot is None:
            if not self.enabled:
                result['reason'] = 'Commodity market-data access is disabled for this app instance.'
            return result
        rate = rate_pct / 100
        slices = []
        for item in snapshot['slices']:
            forward = item.get('forward')
            if is_etf:
                if etf_expiry_time(date.fromisoformat(item['expiry'])) <= now:
                    continue
                discount_inverse = exp(rate * item['years'])
                forward = median(strike + discount_inverse * (
                    item['quotes'][strike]['call']['price'] - item['quotes'][strike]['put']['price']
                ) for strike in item['parity_strikes'])
            row = calibrate_expiry(item['expiry'], item['contract'], forward, item['years'], item['quotes'], rate)
            if row['points']:
                slices.append(row)
        result.update({
            'as_of': snapshot['as_of'], 'status': 'stale' if error else 'ready',
            'slices': slices, 'missing': snapshot['missing'],
            'reference_price': snapshot.get('reference_price'), 'reference_time': snapshot.get('reference_time'),
            'term': {'normal': bootstrap_term(slices, 'atm_normal_iv'), 'black': bootstrap_term(slices, 'atm_black_iv')},
        })
        if not slices:
            result.update(status='unavailable', reason=error or 'No eligible out-of-the-money options remain.')
        elif not error and not is_etf and (now.date() - date.fromisoformat(snapshot['as_of'])).days > 4:
            result.update(status='stale', reason='The latest CME settlement is more than four calendar days old.')
        return result
