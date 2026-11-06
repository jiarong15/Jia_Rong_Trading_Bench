"""Auto-updating Saxo public OTC ATM volatility report.

Fetching a report never changes its publication timestamp. Saved reports are
reparsed on startup, and a failed/older response cannot overwrite newer data.
"""
from copy import deepcopy
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import ssl
from threading import Event, RLock, Thread
from urllib.request import HTTPSHandler, Request, build_opener

from .fx_exact_feed import NoRedirect, PAIRS, timestamp

SAXO_URL = 'https://fxowebtools.saxobank.com/otc.html'
REPORT_POLL = 60
TENORS = {'1W': '1w', '1M': '1m', '3M': '3m', '6M': '6m', '12M': '1y'}


def tls_context():
    context = ssl.create_default_context()
    certificate = os.environ.get('FX_ANALYTICS_CA_FILE', '').strip()
    if certificate:
        path = Path(certificate).expanduser()
        if not path.is_absolute(): path = Path(__file__).resolve().parents[1] / path
        context.load_verify_locations(cafile=path)
    return context


def public_report():
    opener = build_opener(NoRedirect(), HTTPSHandler(context=tls_context()))
    request = Request(SAXO_URL, headers={'User-Agent': 'MeridianLocalDashboard/1.0', 'Cache-Control': 'no-cache'})
    with opener.open(request, timeout=18) as response: body = response.read(1_000_001)
    if len(body) > 1_000_000: raise ValueError('Saxo report exceeds size limit.')
    return body.decode('utf-8-sig')


class ReportParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ''; self.in_title = False; self.tables = []; self.table = None
        self.row = None; self.cell = None; self.text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'div' and 'blogTitle' in attrs.get('class', '').split(): self.in_title = True
        if tag == 'table': self.table = []
        if self.table is not None and tag == 'tr': self.row = []
        if self.row is not None and tag in ('td', 'th'): self.cell = ''

    def handle_data(self, data):
        if self.in_title: self.title += data
        if self.cell is not None: self.cell += data
        self.text.append(data)

    def handle_endtag(self, tag):
        if tag == 'div': self.in_title = False
        if tag in ('td', 'th') and self.cell is not None:
            self.row.append(' '.join(self.cell.split())); self.cell = None
        if tag == 'tr' and self.row is not None:
            self.table.append(self.row); self.row = None
        if tag == 'table' and self.table is not None:
            self.tables.append(self.table); self.table = None


def parse_saxo(body, now, tenor='12M'):
    if not isinstance(body, str) or len(body) > 1_000_000 or tenor not in TENORS:
        raise ValueError('Invalid Saxo report.')
    parser = ReportParser(); parser.feed(body)
    if not parser.tables or 'ATM VOLATILITIES' not in ' '.join(parser.text):
        raise ValueError('Missing ATM volatility section.')
    # Parse English month names without depending on the machine locale.
    match = re.fullmatch(r'\s*(\d{1,2}) ([A-Za-z]{3}) (\d{4}), (\d{2})(\d{2}) GMT\s*', parser.title)
    if not match: raise ValueError('Missing report publication date.')
    day, month, year, hour, minute = match.groups()
    month_number = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'].index(month) + 1
    at = datetime(int(year), month_number, int(day), int(hour), int(minute), tzinfo=timezone.utc)
    timestamp(at.isoformat(), now)
    table = parser.tables[0]
    expected = ['', 'Pair', 'Spot', '1w', '1m', '3m', '6m', '9m', '1y', '']
    if not table or table[0] != expected: raise ValueError('Changed Saxo ATM table schema.')
    column = expected.index(TENORS[tenor]); quotes = {}
    for cells in table[1:]:
        if len(cells) not in (9, 10): raise ValueError('Incomplete Saxo ATM row.')
        symbol = cells[1]; pair = symbol[:3] + '/' + symbol[3:]
        if pair not in PAIRS: continue
        if pair in quotes: raise ValueError('Duplicate Saxo pair.')
        match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*\([+\-]?\d+(?:\.\d+)?\)', cells[column])
        if not match: raise ValueError('Invalid Saxo volatility value.')
        value = float(match[1])
        if not math.isfinite(value) or not 0 < value <= 300: raise ValueError('Invalid Saxo volatility units.')
        quotes[pair] = {'implied_volatility_pct': value, 'iv_time': at.isoformat(), 'iv_tenor': tenor,
                        'iv_delta': 'ATM', 'iv_source': 'Saxo public OTC report', 'iv_source_url': SAXO_URL,
                        'iv_delivery': 'daily_report', 'iv_basis': 'OTC quoted ATM'}
    if not quotes: raise ValueError('No supported ATM quotes.')
    return quotes


class SaxoReportSource:
    def __init__(self, path, enabled=True, transport=None, tenor='12M'):
        self.path = Path(path); self.enabled = enabled; self.transport = transport or public_report; self.tenor = tenor
        self.quotes = {}; self.checked = None; self.attempt = None; self.error = None; self.raw = None
        self.lock = RLock(); self.inflight = False

    def load_saved(self, now):
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            checked = timestamp(saved['checked_at'], now)
            quotes = parse_saxo(saved['report'], now, self.tenor)
            self.quotes = quotes; self.raw = saved['report']; self.checked = checked.isoformat()
        except (OSError, ValueError, KeyError, TypeError, AttributeError): pass

    def refresh(self, now, force=False):
        with self.lock:
            if not self.enabled or self.inflight: return
            if not self.quotes and self.attempt is None: self.load_saved(now)
            if not force and self.attempt and (now - self.attempt).total_seconds() < REPORT_POLL: return
            self.attempt = now; self.inflight = True
        try:
            raw = self.transport(); quotes = parse_saxo(raw, now, self.tenor)
            with self.lock:
                if self.quotes and min(q['iv_time'] for q in quotes.values()) < max(q['iv_time'] for q in self.quotes.values()):
                    raise ValueError('Regressed Saxo publication date.')
                self.quotes = quotes; self.raw = raw; self.checked = now.isoformat(); self.error = None
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix('.tmp')
                temporary.write_text(json.dumps({'report': raw, 'checked_at': self.checked}), encoding='utf-8')
                temporary.replace(self.path)
        except Exception:
            with self.lock: self.error = 'Saxo report check failed; saved report values retain their publication time.'
        finally:
            with self.lock: self.inflight = False

    def snapshot(self, now):
        with self.lock:
            if not self.quotes and self.attempt is None and self.enabled: self.load_saved(now)
            return {'quotes': deepcopy(self.quotes), 'checked_at': self.checked, 'error': self.error}


class FXVolatilitySource:
    def __init__(self, path, enabled=True, tenor='12M', report=None, clock=None):
        self.report = report or SaxoReportSource(path, enabled=enabled and os.environ.get('FX_SAXO_REPORT_ENABLED','true').lower() == 'true', tenor=tenor)
        self.clock = clock or (lambda:datetime.now(timezone.utc))
        self.stop = Event(); self.thread = None; self.lock = RLock()

    def refresh(self, now, force=False):
        self.report.refresh(now, force=force)

    def snapshot(self, now):
        report = self.report.snapshot(now)
        return {'quotes': report['quotes'], 'provider':'Saxo public OTC report', 'enabled':self.report.enabled,
                'status':'disabled' if not self.report.enabled else 'daily_report' if report['quotes'] else 'unavailable',
                'report_checked_at':report['checked_at'],
                'report_published_at':max((q['iv_time'] for q in report['quotes'].values()),default=None),
                'poll_interval_seconds':REPORT_POLL, 'errors':[report['error']] if report['error'] else []}

    def _run(self):
        while not self.stop.is_set():
            self.refresh(self.clock())
            if self.stop.wait(REPORT_POLL): break

    def start(self):
        """Keep the report fresh while the app runs, including with hidden tabs."""
        if not self.report.enabled: return
        with self.lock:
            if self.thread and self.thread.is_alive(): return
            self.stop.clear()
            self.thread = Thread(target=self._run,name='fx-saxo-report',daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread: self.thread.join(timeout=4)
