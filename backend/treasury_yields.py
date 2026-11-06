"""Official FRED/H.15 daily benchmarks; same-date spreads, no filled observations."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import csv
from datetime import date, datetime, timezone
from http.client import HTTPException
import io
import json
import math
import os
from pathlib import Path
import ssl
from threading import RLock
from urllib.parse import urlencode
from urllib.request import Request, urlopen

START = '2000-01-01'


def poll_interval(now):
    """Cover the 16:15 Eastern H.15 release and publication lag in either DST season."""
    utc = now.astimezone(timezone.utc)
    return 60 if utc.weekday() < 5 and 20 <= utc.hour < 23 else 300


TENORS = [('m3', '3M', 'DGS3MO', .25), ('y2', '2Y', 'DGS2', 2), ('y5', '5Y', 'DGS5', 5),
          ('y10', '10Y', 'DGS10', 10), ('y30', '30Y', 'DGS30', 30)]
METRICS = [
    ('2s10s', 'spread', '10Y − 2Y', {'y10': 1, 'y2': -1}),
    ('3m10s', 'spread', '10Y − 3M', {'y10': 1, 'm3': -1}),
    ('5s30s', 'spread', '30Y − 5Y', {'y30': 1, 'y5': -1}),
    ('5s10s', 'spread', '10Y − 5Y', {'y10': 1, 'y5': -1}),
    ('10s30s', 'spread', '30Y − 10Y', {'y30': 1, 'y10': -1}),
    ('2s5s10s', 'butterfly', '2 × 5Y − 2Y − 10Y', {'y5': 2, 'y2': -1, 'y10': -1}),
    ('2s10s30s', 'butterfly', '2 × 10Y − 2Y − 30Y', {'y10': 2, 'y2': -1, 'y30': -1}),
]


def fred_url(ids):
    return 'https://fred.stlouisfed.org/graph/fredgraph.csv?'+urlencode({'id': ','.join(ids), 'cosd': START})


def fetch_text(url):
    context = ssl.create_default_context()
    configured = os.environ.get('BONDS_CA_FILE', '').strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute(): path = Path(__file__).resolve().parents[1]/path
        context.load_verify_locations(cafile=path)
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; MeridianLocalDashboard/1.0)',
                                   'Accept': 'application/json,text/csv;q=0.9', 'Cache-Control': 'no-cache'})
    with urlopen(request, timeout=12, context=context) as response:
        body = response.read(3_000_001)
    if len(body) > 3_000_000: raise ValueError('Treasury source response too large.')
    return body.decode('utf-8-sig')


def csv_records(body, ids, today):
    reader = csv.DictReader(io.StringIO(body.lstrip('\ufeff')))
    if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)) or not {'observation_date', *ids}.issubset(reader.fieldnames):
        raise ValueError('FRED returned an unexpected daily-yield schema.')
    rows = {}
    for item in reader:
        stamp = date.fromisoformat(item['observation_date'])
        key = stamp.isoformat()
        if key in rows or stamp > today: raise ValueError('FRED returned duplicate or future observation dates.')
        values = {}
        for identifier in ids:
            raw = item.get(identifier)
            if not isinstance(raw, str): raise ValueError('FRED returned an incomplete CSV row.')
            text = raw.strip()
            value = None if text in ('', '.', 'NA') else float(text)
            if value is not None and not math.isfinite(value): raise ValueError('Invalid source yield.')
            values[identifier] = value
        rows[key] = values
    if not rows: raise ValueError('FRED returned no yield observations.')
    return rows


def validate_rows(rows, today, required=None):
    if not isinstance(rows, list) or not 2 <= len(rows) <= 30000: raise ValueError('Yield history is incomplete.')
    seen = set()
    result = []
    for row in rows:
        stamp = date.fromisoformat(row['date'])
        if stamp.isoformat() in seen or stamp > today: raise ValueError('Invalid yield date.')
        seen.add(stamp.isoformat())
        values = {key: row[key] for key, *_ in TENORS}
        if any(value is not None and (type(value) not in (int, float) or not math.isfinite(value)) for value in values.values()):
            raise ValueError('Invalid yield value.')
        if stamp.isoformat() >= START: result.append({'date': stamp.isoformat(), **values})
    if any(sum(row[key] is not None for row in result) < 2 for key in (required if required is not None else [item[0] for item in TENORS])):
        raise ValueError('Each Treasury maturity needs at least two observations.')
    return sorted(result, key=lambda row: row['date'])


def api_download(transport, today, api_key):
    """Download native daily levels; authenticated URLs never leave this backend."""
    def one(tenor):
        key, _, identifier, _ = tenor
        query = urlencode({'series_id': identifier, 'api_key': api_key, 'file_type': 'json',
                           'observation_start': START, 'observation_end': today.isoformat(),
                           'units': 'lin', 'sort_order': 'asc', 'limit': 100000, 'offset': 0})
        response = json.loads(transport('https://api.stlouisfed.org/fred/series/observations?'+query))
        observations = response['observations']
        if (not isinstance(observations, list) or type(response['count']) is not int
                or response['count'] != len(observations) or response.get('offset') != 0
                or response.get('units') != 'lin'):
            raise ValueError('FRED API returned incomplete or transformed observations.')
        values = {}
        for item in observations:
            stamp = date.fromisoformat(item['date']).isoformat()
            if stamp in values or stamp < START or stamp > today.isoformat():
                raise ValueError('FRED API returned invalid observation dates.')
            raw = item['value']
            if not isinstance(raw, str): raise ValueError('FRED API returned an invalid value.')
            value = None if raw.strip() in ('', '.', 'NA') else float(raw)
            if value is not None and not math.isfinite(value): raise ValueError('Invalid API yield.')
            values[stamp] = value
        return key, values
    with ThreadPoolExecutor(max_workers=5) as executor:
        sources = dict(executor.map(one, TENORS))
    dates = sorted({stamp for values in sources.values() for stamp in values})
    rows = [{'date': stamp, **{key: values.get(stamp) for key, values in sources.items()}} for stamp in dates]
    return validate_rows(rows, today), 'FRED API'


def download(transport, today, api_key=None):
    api_failed = False
    if api_key:
        try:
            return api_download(transport, today, api_key)
        except (OSError, HTTPException, ValueError, KeyError, TypeError):
            # Suppress credential-bearing exception URLs and provider bodies.
            api_failed = True
    ids = [item[2] for item in TENORS]
    route = 'FRED public CSV'
    try:
        records = csv_records(transport(fred_url(ids)), ids, today)
    except (OSError, HTTPException, ValueError, KeyError, TypeError):
        def one(identifier):
            return identifier, csv_records(transport(fred_url([identifier])), [identifier], today)
        with ThreadPoolExecutor(max_workers=5) as executor:
            sources = dict(executor.map(one, ids))
        records = {stamp: {identifier: sources[identifier].get(stamp, {}).get(identifier) for identifier in ids}
                   for stamp in sorted({stamp for source in sources.values() for stamp in source})}
        route = 'FRED public CSV · individual-series downloads'
    rows = [{'date': stamp, **{key: values[identifier] for key, _, identifier, _ in TENORS}} for stamp, values in records.items()]
    return validate_rows(rows, today), route+(' (API unavailable)' if api_failed else '')


def latest_pair(rows, key):
    available = [row for row in rows if row[key] is not None]
    if not available: return {'value': None, 'date': None, 'previous_date': None, 'change_bp': None}
    last = available[-1]
    previous = available[-2] if len(available) > 1 else None
    return {'value': last[key], 'date': last['date'], 'previous_date': previous['date'] if previous else None,
            'change_bp': round(last[key]-previous[key], 6) if previous else None}


def analytics(rows, tenors=None):
    tenors = TENORS if tenors is None else tenors
    result = []
    for original in rows:
        row = dict(original)
        for key, _, _, weights in METRICS:
            row[key] = (round(sum(original[leg]*weight for leg, weight in weights.items())*100, 6)
                        if all(original[leg] is not None for leg in weights) else None)
        result.append(row)
    quotes = []
    for key, label, identifier, years in tenors:
        pair = latest_pair(rows, key)
        if pair['change_bp'] is not None: pair['change_bp'] = round(pair['change_bp']*100, 6)
        quotes.append({'key': key, 'label': label, 'series_id': identifier, 'years': years, **pair})
    measures = [{'key': key, 'kind': kind, 'formula': formula, **latest_pair(result, key)} for key, kind, formula, _ in METRICS]
    common = [row for row in rows if all(row[key] is not None for key, _, identifier, _ in tenors if identifier)]
    latest = common[-1] if common else None
    previous = common[-2] if len(common) > 1 else None
    curve = [{'tenor': label, 'years': years, 'value': latest[key], 'previous': previous[key] if previous else None}
             for key, label, identifier, years in tenors if identifier] if latest else []
    return {'rows': result, 'quotes': quotes, 'metrics': measures, 'curve': curve,
            'curve_date': latest['date'] if latest else None, 'previous_curve_date': previous['date'] if previous else None,
            'latest_observation_date': max((row['date'] for row in rows if any(row[key] is not None for key, *_ in TENORS)), default=None)}


class TreasuryYieldService:
    def __init__(self, path, enabled=True, transport=None, clock=None, api_key=None, definition=None, downloader=None):
        self.path = Path(path)
        self.enabled = enabled
        self.transport = transport or fetch_text
        self.api_key = api_key if api_key is not None else (os.environ.get('BONDS_FRED_API_KEY') or os.environ.get('FRED_API_KEY'))
        self.definition = definition
        self.country_code = definition['code'] if definition else 'USA'
        self.tenors = definition['tenors'] if definition else TENORS
        self.required = [key for key, _, identifier, _ in self.tenors if identifier and not (self.country_code=='GBR' and key=='m3')]
        self.source_name = definition['source'] if definition else 'FRED · Federal Reserve H.15'
        self.downloader = downloader
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = RLock()
        self.snapshot = None
        self.last_attempt = None
        self.last_finished = None
        self.error = None
        self.cache_warning = None
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(saved, dict) and saved.get('schema') == 1:
                checked = datetime.fromisoformat(saved['checked_at'])
                if checked.tzinfo is None or checked > self.clock(): raise ValueError('Invalid saved check time.')
                if saved.get('country', 'USA') != self.country_code: raise ValueError('Saved yields belong to another country.')
                rows = validate_rows(saved['rows'], self.clock().date(), self.required)
                route = saved.get('source_route', 'FRED public CSV')
                allowed_routes = (self.source_name,) if definition else ('FRED API', 'FRED public CSV', 'FRED public CSV · individual-series downloads',
                                 'FRED public CSV (API unavailable)', 'FRED public CSV · individual-series downloads (API unavailable)')
                if route not in allowed_routes:
                    raise ValueError('Invalid saved source route.')
                self.snapshot = {'rows': rows, 'checked_at': checked.isoformat(), 'source_route': route, 'view': analytics(rows, self.tenors), 'metadata':saved.get('metadata',{}) if self.country_code=='GBR' else {}}
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def view(self, refresh=False, saved_only=False):
        entered = self.clock()
        with self.lock:
            now = self.clock()
            interval = 30 if self.error else poll_interval(now) if self.country_code == 'USA' else 60 if self.country_code in ('CHN','JPN','GBR','CAN','SGP') else 300
            # Successful cache intervals start at request time so download latency
            # does not turn a five-minute browser poll into ten-minute source checks.
            reference = self.last_finished if self.error else self.last_attempt
            age = (now-reference).total_seconds() if reference else float('inf')
            shared = self.last_finished is not None and self.last_finished >= entered
            if self.enabled and not saved_only and not shared and (self.last_finished is None or age >= interval or (refresh and not self.error)):
                self.last_attempt = now
                try:
                    downloaded = self.downloader(now.date()) if self.downloader else download(self.transport, now.date(), self.api_key)
                    rows, route = downloaded[:2]
                    metadata = downloaded[2] if self.country_code=='GBR' and len(downloaded)==3 else {}
                    rows = validate_rows(rows, now.date(), self.required)
                    view = analytics(rows, self.tenors)
                    if self.snapshot:
                        old = self.snapshot['view']
                        if view['latest_observation_date'] < old['latest_observation_date'] or rows[0]['date'] > self.snapshot['rows'][0]['date']:
                            raise ValueError('FRED returned regressed or truncated history.')
                        if self.country_code in ('JPN','GBR','CAN','SGP'):
                            for key in self.required:
                                before = [row['date'] for row in self.snapshot['rows'] if row[key] is not None]
                                after = [row['date'] for row in rows if row[key] is not None]
                                if after[0] > before[0] or after[-1] < before[-1]:
                                    raise ValueError('The source returned regressed or truncated tenor history.')
                    checked = self.clock().isoformat()
                    self.snapshot = {'rows': rows, 'checked_at': checked, 'source_route': route, 'view': view, 'metadata':metadata}
                    self.error = None
                    try:
                        self.path.parent.mkdir(parents=True, exist_ok=True)
                        temporary = self.path.with_suffix('.tmp')
                        temporary.write_text(json.dumps({'schema': 1, 'country': self.country_code, 'rows': rows, 'checked_at': checked, 'source_route': route, 'metadata':metadata}), encoding='utf-8')
                        temporary.replace(self.path)
                        self.cache_warning = None
                    except OSError:
                        self.cache_warning = 'Source data loaded, but the local saved copy could not be written.'
                except (OSError, HTTPException, ValueError, KeyError, TypeError):
                    self.error = f'The {self.source_name} yield download failed validation or could not be reached. Retrying automatically; any saved observations retain their original dates.'
                self.last_finished = self.clock()
            snapshot = self.snapshot
            fresh = snapshot is not None and (now-datetime.fromisoformat(snapshot['checked_at'])).total_seconds() <= 600
            status = 'disabled' if not self.enabled else 'ready' if snapshot and fresh and not self.error else 'stale' if snapshot else 'unavailable'
            metadata = snapshot.get('metadata',{}) if snapshot else {}
            if status=='ready' and metadata.get('source_warning'): status='stale'
            base = snapshot['view'] if snapshot else {'rows': [], 'quotes': [], 'metrics': [], 'curve': [], 'curve_date': None, 'previous_curve_date': None, 'latest_observation_date': None}
            latest = base['latest_observation_date']
            lag = (now.date()-date.fromisoformat(latest)).days if latest else None
            return deepcopy({**base, 'status': status, 'source': self.source_name,
                             'source_route': snapshot['source_route'] if snapshot else None,
                             'source_warning': metadata.get('source_warning') or ('FRED API unavailable; official public FRED downloads are being used.' if snapshot and '(API unavailable)' in snapshot['source_route'] else None),
                             'source_checks': metadata.get('source_checks'),
                             'frequency': 'Daily', 'yield_unit': 'percent', 'analytics_unit': 'bp',
                             'last_checked_at': snapshot['checked_at'] if snapshot else None,
                             'last_attempt_at': self.last_attempt.isoformat() if self.last_attempt else None,
                             'error': self.error, 'cache_warning': self.cache_warning,
                             'poll_interval_seconds': 30 if self.error else poll_interval(self.clock()) if self.country_code == 'USA' else 60 if self.country_code in ('CHN','JPN','GBR','CAN','SGP') else 300,
                             'observation_warning': f'The newest published observation is {lag} calendar days old. Check for source publication delays.' if lag is not None and lag > 7 else None,
                             'sources': [{'tenor': label, 'series_id': identifier, 'url': self.definition['url'] if self.definition else 'https://fred.stlouisfed.org/series/'+identifier} for _, label, identifier, _ in self.tenors if identifier],
                             'notice': 'Automatically refreshed official daily constant-maturity yields, not intraday executable bond prices. Spreads and butterflies use matching observation dates; missing legs remain unavailable. Butterflies use 2 × belly − front − back (unweighted yields).'
                             })
