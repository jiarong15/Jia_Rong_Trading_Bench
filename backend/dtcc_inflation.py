"""USD CPI-U five-year zero-coupon swap rates from DTCC public dissemination.

The displayed rate is the unweighted daily median of eligible executed trades,
updated from intraday files. It is not a dealer mid or a continuous quote.
Only aggregates leave this service; compact lifecycle records stay in its local
cache so corrections/errors can revise historical observations after a restart.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import csv
import io
import json
import math
from pathlib import Path
import re
from statistics import median
from threading import Event, RLock, Thread
from time import monotonic
from urllib.request import HTTPSHandler, Request, build_opener
import zipfile

from .fx_exact_feed import NoRedirect, timestamp
from .fx_volatility import tls_context

BASE = 'https://pddata.dtcc.com/ppd/'
SOURCE_URL = BASE
PROVIDER = 'DTCC public dissemination · executed-trade median'
POLL = 60
BASIS = 'USD_CPI_U_ZCIS_5Y_MEDIAN_V1'
HISTORY_DAYS = 365
HISTORY_BATCH = 8
SLICE_BATCH = 64
FILE_PATTERN = re.compile(r'CFTC_(CUMULATIVE|SLICE)_RATES_(\d{4})_(\d{2})_(\d{2})(?:_(\d+))?\.zip')
FIELDS = {'Dissemination Identifier', 'Original Dissemination Identifier', 'Action type', 'Event type',
          'Event timestamp', 'Execution Timestamp', 'Effective Date', 'Expiration Date',
          'Notional currency-Leg 1', 'Notional currency-Leg 2', 'UPI Underlier Name', 'UPI FISN',
          'Fixed rate-Leg 1', 'Fixed rate-Leg 2', 'Package indicator', 'Other payment amount',
          'Fixed rate payment frequency period-Leg 1', 'Fixed rate payment frequency period-Leg 2',
          'Floating rate payment frequency period-Leg 1', 'Floating rate payment frequency period-Leg 2',
          'Fixed rate payment frequency period multiplier-Leg 1', 'Fixed rate payment frequency period multiplier-Leg 2',
          'Floating rate payment frequency period multiplier-Leg 1', 'Floating rate payment frequency period multiplier-Leg 2'}


def public_bytes(url):
    """Bounded public GETs, verified TLS, no credentials and no redirects."""
    opener = build_opener(NoRedirect(), HTTPSHandler(context=tls_context()))
    request = Request(url, headers={'User-Agent': 'MeridianLocalDashboard/1.0', 'Cache-Control': 'no-cache'})
    deadline = monotonic() + 30
    body = bytearray()
    with opener.open(request, timeout=12) as response:
        while True:
            if monotonic() > deadline:
                raise TimeoutError('DTCC download exceeded its time budget.')
            chunk = response.read1(65536)
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > 20_000_000:
                raise ValueError('DTCC response exceeds size limit.')
    return bytes(body)


def file_identity(name):
    match = FILE_PATTERN.fullmatch(name)
    if not match:
        raise ValueError('Unexpected DTCC file name.')
    kind, year, month, day, sequence = match.groups()
    if (kind == 'SLICE') != (sequence is not None):
        raise ValueError('Invalid DTCC file kind.')
    return kind, date(int(year), int(month), int(day))


def parse_index(body, kind, now):
    document = json.loads(body)
    rows = document['CFTC_IR']
    if not isinstance(rows, list) or not 1 <= len(rows) <= 30000:
        raise ValueError('Invalid DTCC rates index.')
    result = {}
    for item in rows:
        name = item['fileName']; actual_kind, day = file_identity(name)
        if actual_kind != kind or day > now.date() or name in result:
            raise ValueError('Invalid DTCC index entry.')
        published = timestamp(item['dissemDTM'], now)
        if item.get('endTs'):
            published = max(published, timestamp(item['endTs'], now))
        if type(item['rowCount']) is not int or not 0 <= item['rowCount'] <= 500000:
            raise ValueError('Invalid DTCC row count.')
        # Never follow fullFilePath from a remote document. Construct downloads
        # on the fixed official DTCC host from the validated file name instead.
        result[name] = {'day': day.isoformat(), 'published_at': published.isoformat(), 'rows': item['rowCount']}
    return result


def single_payment(row, prefix, leg, years):
    period = row.get(f'{prefix} payment frequency period-Leg {leg}', '')
    multiplier = row.get(f'{prefix} payment frequency period multiplier-Leg {leg}', '')
    if period == 'EXPI':
        return True
    try:
        count = int(multiplier)
    except ValueError:
        return False
    return (period == 'YEAR' and count == years) or (period == 'MNTH' and count == years * 12)


def candidate(row, now):
    """Conservative par-trade screen; no annual coupons, packages or upfronts."""
    if (row['UPI Underlier Name'] != 'USA-CPI-U' or 'Swap Infl Idx USD' not in row['UPI FISN']
            or row['Notional currency-Leg 1'] != 'USD' or row['Notional currency-Leg 2'] != 'USD'
            or row['Event type'] != 'TRAD' or row['Package indicator'].upper() != 'FALSE'):
        return None
    payment = row['Other payment amount'].replace(',', '').strip()
    if payment and (not math.isfinite(float(payment)) or float(payment) != 0):
        return None
    executed = timestamp(row['Execution Timestamp'], now)
    start = date.fromisoformat(row['Effective Date']); end = date.fromisoformat(row['Expiration Date'])
    # Standard five-year anniversary, allowing normal weekend/holiday rolls.
    anniversary = start.replace(year=start.year + 5, day=min(start.day, 28)) if start.month == 2 and start.day == 29 else start.replace(year=start.year + 5)
    if abs((end - anniversary).days) > 7 or not -3 <= (start - executed.date()).days <= 10:
        return None
    legs = [leg for leg in ('1', '2') if row['Fixed rate-Leg ' + leg].strip()]
    if len(legs) != 1:
        return None
    leg = legs[0]; floating = '2' if leg == '1' else '1'
    if not single_payment(row, 'Fixed rate', leg, 5) or not single_payment(row, 'Floating rate', floating, 5):
        return None
    rate = float(row['Fixed rate-Leg ' + leg]) * 100
    if not math.isfinite(rate) or not -20 <= rate <= 50:
        raise ValueError('Invalid DTCC fixed rate.')
    return {'date': executed.date().isoformat(), 'executed_at': executed.isoformat(), 'value': rate}


def parse_archive(body, now, expected_rows=None):
    if not isinstance(body, bytes) or len(body) > 20_000_000:
        raise ValueError('Invalid DTCC archive.')
    events = []; seen = set(); count = 0
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        members = archive.infolist()
        if len(members) != 1 or not members[0].filename.lower().endswith('.csv') or members[0].file_size > 150_000_000:
            raise ValueError('Invalid DTCC CSV archive.')
        with archive.open(members[0]) as stream:
            reader = csv.DictReader(io.TextIOWrapper(stream, encoding='utf-8-sig'))
            if not reader.fieldnames or not FIELDS.issubset(reader.fieldnames) or len(reader.fieldnames) != len(set(reader.fieldnames)):
                raise ValueError('Changed DTCC rates schema.')
            for row in reader:
                count += 1
                if count > 500000 or None in row or any(value is None for value in row.values()):
                    raise ValueError('Incomplete DTCC record.')
                action = row['Action type']
                # MODI/TERM are lifecycle events, not new market-price trades.
                if action not in ('NEWT', 'CORR', 'EROR'):
                    continue
                trade = candidate(row, now) if action != 'EROR' else None
                if action == 'NEWT' and trade is None:
                    continue
                identifier = row['Dissemination Identifier']; original = row['Original Dissemination Identifier']
                if not identifier.isdigit() or (original and not original.isdigit()) or identifier in seen:
                    raise ValueError('Invalid DTCC dissemination identity.')
                if action != 'NEWT' and not original:
                    continue
                at = timestamp(row['Event timestamp'], now).isoformat()
                events.append({'id': identifier, 'original': original, 'action': action, 'at': at, 'trade': trade})
                seen.add(identifier)
    if expected_rows is not None and count != expected_rows:
        raise ValueError('Incomplete DTCC file download.')
    return events


def aggregate(files):
    """Apply correction chains before taking medians; never double count IDs."""
    events = {}
    for item in files.values():
        for event in item['events']:
            existing = events.get(event['id'])
            if existing is not None and existing != event:
                raise ValueError('Conflicting DTCC dissemination records.')
            events[event['id']] = event
    roots = {}; trades = {}
    for event in sorted(events.values(), key=lambda item: (item['at'], int(item['id']))):
        identifier = event['id']; action = event['action']
        if action == 'NEWT':
            roots[identifier] = identifier; trades[identifier] = event['trade']
        elif event['original'] in roots:
            root = roots[event['original']]; roots[identifier] = root
            trades[root] = None if action == 'EROR' else event['trade']
    days = {}
    for trade in trades.values():
        if trade is not None:
            days.setdefault(trade['date'], []).append(trade)
    values = {}; details = {}
    for day, rows in sorted(days.items()):
        values[day] = round(median(item['value'] for item in rows), 6)
        details[day] = {'trade_count': len(rows), 'last_trade_at': max(item['executed_at'] for item in rows)}
    return values, details


class DTCCInflationSource:
    def __init__(self, path, enabled=True, transport=None, clock=None):
        self.path = Path(path); self.enabled = enabled; self.transport = transport or public_bytes
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = RLock(); self.stop = Event(); self.thread = None; self.inflight = False
        self.bucket = None; self.files = {}; self.values = {}; self.details = {}
        self.attempt = None; self.checked = None; self.published = None; self.error = None
        self.pending_live = 0; self.pending_history = 0; self.cache_error = None
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            if saved['basis'] != BASIS or saved['schema'] != 1:
                raise ValueError('Invalid DTCC saved data basis.')
            # Cached aggregates are recomputed; raw CSVs never enter the API.
            now = self.clock()
            for name, item in saved['files'].items():
                _, day = file_identity(name)
                if day > now.date() or not isinstance(item['events'], list):
                    raise ValueError('Invalid DTCC saved file.')
                timestamp(item['published_at'], now)
                for event in item['events']:
                    if (event['action'] not in ('NEWT', 'CORR', 'EROR') or not event['id'].isdigit()
                            or (event['original'] and not event['original'].isdigit())):
                        raise ValueError('Invalid DTCC saved event.')
                    timestamp(event['at'], now)
                    trade = event['trade']
                    if trade is not None:
                        at = timestamp(trade['executed_at'], now)
                        if (trade['date'] != at.date().isoformat() or type(trade['value']) not in (int, float)
                                or not math.isfinite(trade['value']) or not -20 <= trade['value'] <= 50):
                            raise ValueError('Invalid DTCC saved trade.')
            self.files = saved['files']; self.values, self.details = aggregate(self.files)
            self.checked = timestamp(saved['checked_at'], now).isoformat() if saved.get('checked_at') else None
            self.published = timestamp(saved['published_at'], now).isoformat() if saved.get('published_at') else None
            self.pending_live = saved.get('pending_live', 0); self.pending_history = saved.get('pending_history', 0)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.files = {}; self.values = {}; self.details = {}; self.checked = None; self.published = None

    def download(self, name, metadata, now):
        kind, _ = file_identity(name)
        route = 'cumulative' if kind == 'CUMULATIVE' else 'intraday'
        body = self.transport(BASE + f'api/report/{route}/cftc/' + name)
        return {'published_at': metadata['published_at'], 'events': parse_archive(body, now, metadata['rows'])}

    def refresh(self, now=None, force=False):
        now = now or self.clock()
        with self.lock:
            if (not self.enabled or self.inflight or not force and self.attempt and (now - self.attempt).total_seconds() < POLL):
                return
            self.attempt = now; self.inflight = True
        try:
            if not self.bucket:
                bucket = self.transport(BASE + 'api/general/bucketname').decode().strip()
                if not re.fullmatch(r'kgc[0-9a-z-]{3,60}', bucket):
                    raise ValueError('Unexpected DTCC public bucket.')
                self.bucket = bucket
            root = f'https://{self.bucket}.s3.amazonaws.com/dashboard/'
            with ThreadPoolExecutor(max_workers=2) as pool:
                daily_job = pool.submit(self.transport, root + 'Cumulative.json')
                slices_job = pool.submit(self.transport, root + 'Slice.json')
                daily = parse_index(daily_job.result(), 'CUMULATIVE', now)
                slices = parse_index(slices_job.result(), 'SLICE', now)
            latest_day = max(item['day'] for item in daily.values())
            cutoff = (now.date() - timedelta(days=HISTORY_DAYS)).isoformat()
            with self.lock:
                files = deepcopy(self.files)
            loaded_days = [file_identity(name)[1].isoformat() for name in files if file_identity(name)[0] == 'CUMULATIVE']
            if loaded_days and latest_day < max(loaded_days):
                raise ValueError('DTCC index regressed.')
            missing_daily = [name for name in daily if daily[name]['day'] >= cutoff and name not in files]
            missing_daily.sort(key=lambda name: daily[name]['day'], reverse=True)
            selected_daily = missing_daily[:HISTORY_BATCH]
            if force and not selected_daily:
                selected_daily = [max(daily, key=lambda name: daily[name]['day'])]
            failed = []
            def download_batch(names, index):
                with ThreadPoolExecutor(max_workers=6) as pool:
                    jobs = {pool.submit(self.download, name, index[name], now): name for name in names}
                    for job in as_completed(jobs):
                        name = jobs[job]
                        try:
                            files[name] = job.result()
                        except Exception:
                            failed.append(name)
            download_batch(selected_daily, daily)
            loaded_days = [file_identity(name)[1].isoformat() for name in files if file_identity(name)[0] == 'CUMULATIVE']
            baseline = max(loaded_days, default='1990-01-01')
            missing_slices = [name for name in slices if slices[name]['day'] > baseline and name not in files]
            # Newest files first gives the current day promptly; pending coverage
            # is explicit until all the day's earlier files have been processed.
            missing_slices.sort(key=lambda name: slices[name]['published_at'], reverse=True)
            download_batch(missing_slices[:SLICE_BATCH], slices)
            # Daily files supersede that day's slices without counting twice.
            complete_days = set(loaded_days)
            files = {name: item for name, item in files.items() if file_identity(name)[1].isoformat() >= cutoff
                     and not (file_identity(name)[0] == 'SLICE' and file_identity(name)[1].isoformat() in complete_days)}
            values, details = aggregate(files)
            with self.lock:
                self.files = files; self.values = values; self.details = details
                self.pending_history = sum(name not in files for name in daily if daily[name]['day'] >= cutoff)
                self.pending_live = sum(name not in files for name in slices if slices[name]['day'] > baseline)
                self.published = max((item['published_at'] for item in files.values()), default=self.published)
                if not failed:
                    self.checked = self.clock().isoformat()
                self.error = 'Some DTCC files could not be checked. Retrying automatically; observations retain their trade dates.' if failed else None
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = self.path.with_suffix('.tmp')
                    temporary.write_text(json.dumps({'schema': 1, 'basis': BASIS, 'files': files,
                        'checked_at': self.checked, 'published_at': self.published,
                        'pending_live': self.pending_live, 'pending_history': self.pending_history}), encoding='utf-8')
                    temporary.replace(self.path); self.cache_error = None
                except OSError:
                    self.cache_error = 'DTCC observations could not be saved locally.'
        except Exception:
            with self.lock:
                self.error = 'DTCC source check failed. Saved rates retain their original trade timestamps; retrying automatically.'
        finally:
            with self.lock:
                self.inflight = False

    def snapshot(self):
        with self.lock:
            return deepcopy({'values': self.values if self.enabled else {}, 'observation_details': self.details if self.enabled else {},
                'provider': PROVIDER, 'source_url': SOURCE_URL, 'delivery': 'reported_transactions',
                'checked_at': self.checked, 'published_at': self.published, 'poll_interval_seconds': POLL,
                'last_trade_at': max((item['last_trade_at'] for item in self.details.values()), default=None),
                'pending_live_files': self.pending_live, 'pending_history_files': self.pending_history,
                'history_start': min(self.values, default=None),
                'status': 'disabled' if not self.enabled else 'stale' if self.error else 'catching_up' if self.pending_live else 'ready' if self.values else 'unavailable',
                'error': self.error or self.cache_error})

    def _run(self):
        while not self.stop.is_set():
            started = monotonic()
            self.refresh()
            if self.stop.wait(max(.1, POLL - (monotonic() - started))):
                break

    def start(self):
        if not self.enabled:
            return
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop.clear()
            self.thread = Thread(target=self._run, name='rates-dtcc-inflation', daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=4)
