"""Dated, indicative 1Y FX forward quotes from Investing.com's public tables.

The site publishes forward *points*, not outright prices or option volatility.
Never manufacture settlement dates or replace source quote times with fetch time.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import ssl
from urllib.request import Request, HTTPSHandler, build_opener

from .fx_exact_feed import NoRedirect, timestamp

PAIRS = ('EUR/USD', 'GBP/USD', 'USD/JPY', 'USD/CHF', 'USD/CAD', 'AUD/USD', 'NZD/USD', 'USD/SEK', 'USD/NOK')
ROOT = 'https://uk.investing.com/currencies/'


def source_url(pair):
    if pair not in PAIRS:
        raise ValueError('Unsupported forward pair.')
    return ROOT + pair.lower().replace('/', '-') + '-forward-rates'


class Cells(HTMLParser):
    """Read the site's explicitly named quote cells, ignoring script content."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells = {}; self.active = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        identifier = attrs.get('id', '')
        classes = attrs.get('class', '').split()
        if tag == 'td' and re.fullmatch(r'(?:pair_name|bid|ask|time)_\d+', identifier):
            key = identifier
        elif tag == 'span' and any(re.fullmatch(r'pid-\d+-(?:bid|ask|time)', value) for value in classes):
            key = next(value for value in classes if re.fullmatch(r'pid-\d+-(?:bid|ask|time)', value))
        else:
            return
        if key in self.cells:
            raise ValueError('Duplicate public quote cell.')
        self.cells[key] = {'text': '', 'epoch': attrs.get('data-value')}
        self.active = (tag, key)

    def handle_data(self, data):
        if self.active:
            self.cells[self.active[1]]['text'] += data

    def handle_endtag(self, tag):
        if self.active and self.active[0] == tag:
            self.active = None


def numeric(value):
    text = value.strip().replace(',', '').replace('\u2212', '-')
    if not re.fullmatch(r'-?\d+(?:\.\d+)?', text):
        raise ValueError('Invalid public quote number.')
    number = float(text)
    if not math.isfinite(number):
        raise ValueError('Non-finite quote.')
    return number


def parse_page(body, pair, server_at, now):
    if pair not in PAIRS or not isinstance(body, str) or len(body) > 2_000_000:
        raise ValueError('Invalid forward page.')
    if server_at.tzinfo is None or abs((now - server_at).total_seconds()) > 300:
        raise ValueError('Missing or stale HTTP source date.')
    # The public UK page explicitly uses UTC. Reject a locale change instead of
    # silently treating local display clocks as UTC.
    if not re.search(r'window\.timezoneOffset\s*=\s*\+?["\']0["\']', body):
        raise ValueError('Unrecognized source timezone.')
    parser = Cells(); parser.feed(body)
    cells = parser.cells
    symbol = pair.replace('/', '')
    matching = [key.removeprefix('pair_name_') for key, cell in cells.items()
                if key.startswith('pair_name_') and ' '.join(cell['text'].split()) == symbol + ' 1Y FWD']
    if len(matching) != 1:
        raise ValueError('Missing or duplicate exact 1Y forward row.')
    identifier = matching[0]
    forward_at = datetime.fromtimestamp(int(cells['time_' + identifier]['epoch']), timezone.utc)
    timestamp(forward_at.isoformat(), now)
    # Identify the main spot instrument from the page's main last-price element.
    spot_ids = re.findall(r'<span\b[^>]*class=["\'][^"\']*\bpid-(\d+)-last\b[^"\']*["\'][^>]*id=["\']last_last["\']', body)
    if len(spot_ids) != 1:
        raise ValueError('Missing spot identity.')
    prefix = 'pid-' + spot_ids[0] + '-'
    spot_label = cells[prefix + 'time']['text'].strip()
    spot_at = None
    if re.fullmatch(r'\d{1,2}:\d{2}:\d{2}', spot_label):
        hour, minute, second = map(int, spot_label.split(':'))
        spot_at = server_at.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if spot_at > server_at + timedelta(seconds=120):
            spot_at -= timedelta(days=1)
        spot_day = spot_at.date()
    elif re.fullmatch(r'\d{2}/\d{2}', spot_label):
        day, month = map(int, spot_label.split('/'))
        spot_day = date(server_at.year, month, day)
        if spot_day > server_at.date():
            spot_day = date(server_at.year - 1, month, day)
    else:
        raise ValueError('Unknown spot date precision.')
    raw = {'pair': pair, 'spot_bid': numeric(cells[prefix + 'bid']['text']),
           'spot_ask': numeric(cells[prefix + 'ask']['text']),
           'spot_observation_date': spot_day.isoformat(),
           'spot_time': spot_at.isoformat() if spot_at else None,
           'forward_bid_points': numeric(cells['bid_' + identifier]['text']),
           'forward_ask_points': numeric(cells['ask_' + identifier]['text']),
           'forward_time': forward_at.isoformat()}
    validate_quote(raw, now)
    return raw


def validate_quote(raw, now):
    pair = raw['pair']; source_url(pair)
    keys = ('spot_bid', 'spot_ask', 'forward_bid_points', 'forward_ask_points')
    if any(type(raw[key]) not in (int, float) or not math.isfinite(raw[key]) for key in keys):
        raise ValueError('Invalid cached forward prices.')
    if not 0 < raw['spot_bid'] <= raw['spot_ask'] or raw['forward_bid_points'] > raw['forward_ask_points']:
        raise ValueError('Crossed or invalid bid/ask.')
    forward_at = timestamp(raw['forward_time'], now)
    spot_day = date.fromisoformat(raw['spot_observation_date'])
    if spot_day > now.date() or abs((forward_at.date() - spot_day).days) > 7:
        raise ValueError('Inconsistent spot and forward dates.')
    if raw['spot_time']:
        if timestamp(raw['spot_time'], now).date() != spot_day:
            raise ValueError('Spot date mismatch.')
    spot = (raw['spot_bid'] + raw['spot_ask']) / 2
    pip = .01 if pair.endswith('/JPY') else .0001
    forward_points = (raw['forward_bid_points'] + raw['forward_ask_points']) / 2
    forward = spot + forward_points * pip
    if forward <= 0 or abs(forward / spot - 1) > .5:
        raise ValueError('Invalid 1Y forward conversion.')
    base_carry = (spot / forward - 1) * 100
    return {'pair': pair, 'spot': spot, 'forward_12m': forward,
            'pair_carry_pct': base_carry,
            'usd_carry_pct': base_carry if pair.startswith('USD/') else (forward / spot - 1) * 100,
            'quote_time': forward_at.isoformat(), 'spot_time': raw['spot_time'],
            'spot_observation_date': spot_day.isoformat(),
            'spot_time_precision': 'second' if raw['spot_time'] else 'day',
            'spot_bid': raw['spot_bid'], 'spot_ask': raw['spot_ask'],
            'forward_bid_points': raw['forward_bid_points'], 'forward_ask_points': raw['forward_ask_points'],
            'forward_points': forward_points, 'pip_size': pip, 'source': 'Investing.com public forward table',
            'source_url': source_url(pair), 'carry_basis': 'quoted_1Y_tenor',
            'spot_date': None, 'forward_date': None,
            'iv_time': None, 'iv_tenor': None, 'implied_volatility_pct': None}


def fetch_page(url):
    handlers = [NoRedirect()]
    certificate = os.environ.get('FX_ANALYTICS_CA_FILE', '').strip()
    if certificate:
        # Match Python's normal HTTPS handshake, including HTTP/1.1 ALPN and
        # TLS 1.3 post-handshake authentication, while adding the trusted CA.
        context = ssl.create_default_context()
        context.set_alpn_protocols(['http/1.1'])
        if context.post_handshake_auth is not None: context.post_handshake_auth = True
        path = Path(certificate).expanduser()
        if not path.is_absolute(): path = Path(__file__).resolve().parents[1] / path
        context.load_verify_locations(cafile=path)
        handlers.append(HTTPSHandler(context=context))
    opener = build_opener(*handlers)
    request = Request(url, headers={'User-Agent': 'JRTradingWorkbench/1.0', 'Cache-Control': 'no-cache'})
    with opener.open(request, timeout=12) as response:
        body = response.read(2_000_001)
        server_at = parsedate_to_datetime(response.headers['Date'])
    if len(body) > 2_000_000:
        raise ValueError('Oversized forward source.')
    return body.decode('utf-8-sig'), server_at


class InvestingForwardSource:
    def __init__(self, path, transport=None):
        self.path = Path(path); self.transport = transport or fetch_page
        self.raw = {}; self.checks = {}; self.errors = {}
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            if saved['schema'] != 1: raise ValueError('Invalid forward cache.')
            now = datetime.now(timezone.utc)
            for pair, raw in saved['quotes'].items():
                if pair != raw['pair']: raise ValueError('Cached pair mismatch.')
                validate_quote(raw, now)
                timestamp(saved['checks'][pair], now)
            self.raw = saved['quotes']; self.checks = saved['checks']
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    def refresh(self, now):
        def one(pair):
            try:
                body, server_at = self.transport(source_url(pair))
                raw = parse_page(body, pair, server_at, now)
                previous = self.raw.get(pair)
                if previous and (raw['forward_time'] < previous['forward_time']
                                 or raw['spot_observation_date'] < previous['spot_observation_date']
                                 or raw['spot_time'] and previous['spot_time'] and raw['spot_time'] < previous['spot_time']):
                    raise ValueError('Regressed source time.')
                return pair, raw
            except Exception:
                return pair, None
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(one, PAIRS))
        for pair, raw in results:
            if raw is None:
                self.errors[pair] = pair + ': forward check failed; previous source dates are retained.'
            else:
                self.raw[pair] = raw; self.checks[pair] = now.isoformat(); self.errors.pop(pair, None)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'schema': 1, 'quotes': self.raw, 'checks': self.checks}), encoding='utf-8')
            temporary.replace(self.path); self.errors.pop('cache', None)
        except OSError:
            self.errors['cache'] = 'The forward quote snapshot could not be saved.'
        return self.snapshot(now)

    def snapshot(self, now):
        return {'quotes': {pair: validate_quote(raw, now) for pair, raw in self.raw.items()},
                'checks': dict(self.checks), 'errors': dict(self.errors)}
