"""US BOP companions: native frequencies, explicit stock/flow denominators."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import csv
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from http.client import HTTPException
import io
import json
from pathlib import Path
import re
from threading import RLock
import time
from urllib.request import Request
from .bop_http import urlopen
from .bop import BOPError, CensusRows, numeric, quarter_index, quarter_label

QUARTER_IDS = ('GDP', 'IEAADI', 'IEAIDI', 'IEAAPIE', 'IEAIPIE', 'IEAAPID', 'IEAIPID', 'IEAAOI', 'IEAIOI', 'IEAAR')
GROUPS = {
    'quarterly': (QUARTER_IDS, 'quarterly'),
    'reserve_stock': (('IIPRESEQ',), 'quarterly'),
    'monthly': (('BOPTEXP', 'BOPTIMP'), 'monthly'),
    'daily': (('DTWEXBGS', 'DEXUSEU'), 'daily'),
}
DEBT_INDEX = 'https://home.treasury.gov/data/treasury-international-capital-tic-system-home-page/tic-forms-instructions/us-gross-external-debt'
DEBT_ROOT = 'https://ticdata.treasury.gov/resource-center/data-chart-center/tic/Documents/'
SNAPSHOT = Path(__file__).parent / 'snapshots' / 'usa_bop_macro.json'


class DebtDateMismatch(BOPError):
    pass


def source_url(ids):
    return 'https://fred.stlouisfed.org/graph/fredgraph.csv?id=' + ','.join(ids) + '&cosd=1998-01-01'


def fetch_macro_text(url):
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; MeridianLocalDashboard/1.0)', 'Accept': 'text/csv,text/html;q=0.9,*/*;q=0.5', 'Cache-Control': 'no-cache', 'Pragma': 'no-cache'})
    with urlopen(request, timeout=20) as response:
        body = response.read(5_000_001)
    if len(body) > 5_000_000:
        raise BOPError('Source download exceeded the size limit.')
    try:
        return body.decode('utf-8-sig')
    except UnicodeDecodeError:
        if url.startswith(DEBT_ROOT):
            return body.decode('cp1252')
        raise BOPError('Source did not return the expected text data.')


def parse_csv(body, ids, frequency):
    reader = csv.DictReader(io.StringIO(body.lstrip('\ufeff')))
    if not reader.fieldnames or not {'observation_date', *ids}.issubset(reader.fieldnames):
        raise BOPError('A requested FRED series is missing from the response.')
    rows = []
    seen = set()
    for item in reader:
        at = date.fromisoformat(item['observation_date'])
        if at.isoformat() in seen or (frequency != 'daily' and at.day != 1) or (frequency == 'quarterly' and at.month not in (1, 4, 7, 10)):
            raise BOPError('Source dates do not match the requested native frequency.')
        seen.add(at.isoformat())
        rows.append({'date': at.isoformat(), **{key: numeric(item[key]) for key in ids}})
    if not rows or any(not any(row[key] is not None for row in rows) for key in ids):
        raise BOPError('A requested FRED series has no usable observations.')
    return sorted(rows, key=lambda row: row['date'])


class DebtLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = {}

    def handle_starttag(self, tag, attrs):
        if tag != 'a':
            return
        match = re.search(r'/(debtd(\d{4})q([1-4])\.html?)$', dict(attrs).get('href', ''))
        if match:
            year, quarter = int(match[2]), int(match[3])
            self.links[f'{year}-{(quarter - 1) * 3 + 1:02d}-01'] = DEBT_ROOT + match[1]


def parse_debt(body, expected_date):
    parser = CensusRows()
    parser.feed(body)
    heading = ' '.join(' '.join(row) for row in parser.rows[:8])
    match = re.search(r'as of\s+(March|June|Sept(?:ember)?|December)\s+(\d{1,2}),?\s+(\d{4})', heading, re.I)
    if not match:
        raise BOPError('Treasury debt table has no recognized reporting date.')
    quarter = {'march': 1, 'june': 2, 'sept': 3, 'september': 3, 'december': 4}[match[1].lower()]
    if f'{match[3]}-{(quarter - 1) * 3 + 1:02d}-01' != expected_date:
        raise DebtDateMismatch('Treasury debt table date does not match its link.')
    # Table D has principal AND interest. Only principal belongs in the stock
    # of external debt maturing within one year; the combined total is not used.
    for i, row in enumerate(parser.rows):
        if any(re.fullmatch(r'Total(?:\s+\d+/)?', cell) for cell in row):
            for following in parser.rows[i + 1:i + 4]:
                if 'Principal' in following:
                    value = numeric(next((cell for cell in reversed(following) if cell.strip()), ''))
                    if value is not None and value > 0:
                        return value / 1000
    raise BOPError('Treasury total principal due within one year is unavailable.')


def monthly_index(value):
    at = date.fromisoformat(value)
    return at.year * 12 + at.month - 1


def build_trade_view(monthly, frequency):
    """Complete calendar-quarter trade, then selected-period levels and lags."""
    if not monthly:
        return []
    months = {monthly_index(row['date']): row for row in monthly}
    complete = {}
    for quarter in range(min(months) // 3, max(months) // 3 + 1):
        window = [months.get(index, {}) for index in range(quarter * 3, quarter * 3 + 3)]
        if all(row.get(field) is not None for row in window for field in ('exports', 'imports')):
            complete[quarter] = {field: sum(row[field] for row in window) for field in ('exports', 'imports')}
    return build_quarterly_trade_view(complete, frequency)


def build_quarterly_trade_view(complete, frequency):
    """Build selected-period levels/growth from verified complete quarter flows."""
    if frequency not in ('quarterly', 'trailing4q'):
        raise ValueError('Choose quarterly or trailing4q.')
    if not complete:
        return []
    selected = {}
    for quarter in range(min(complete), max(complete) + 1):
        window = [complete.get(index, {}) for index in range(quarter - (3 if frequency == 'trailing4q' else 0), quarter + 1)]
        selected[quarter] = {}
        for field in ('exports', 'imports'):
            values = [row.get(field) for row in window]
            selected[quarter][field] = sum(values) if all(value is not None for value in values) else None
    result = []
    for quarter, values in selected.items():
        at = f'{quarter // 4}-{(quarter % 4) * 3 + 1:02d}-01'
        row = {'date': at, 'period': quarter_label(at), **values}
        exports, imports = values['exports'], values['imports']
        row['trade_balance'] = exports - imports if exports is not None and imports is not None else None
        for field in ('exports', 'imports'):
            average = [selected.get(index, {}).get(field) for index in range(quarter - 2, quarter + 1)]
            previous = selected.get(quarter - 4, {}).get(field)
            row[field + '_ma3'] = sum(average) / 3 if all(value is not None for value in average) else None
            row[field + '_yoy'] = (values[field] / previous - 1) * 100 if values[field] is not None and previous is not None and previous > 0 else None
        result.append(row)
    return result


def build_macro_view(snapshot, base_rows, frequency='quarterly'):
    if frequency not in ('quarterly', 'trailing4q'):
        raise ValueError('Choose quarterly or trailing4q.')
    periods = 4 if frequency == 'trailing4q' else 1
    groups = snapshot.get('groups', {})
    lookup = {name: {row['date']: row for row in group['rows']} for name, group in groups.items()}
    monthly_raw = lookup.get('monthly', {})
    monthly_by_index = {monthly_index(at): row for at, row in monthly_raw.items()}
    monthly = []
    for at, raw in sorted(monthly_raw.items()):
        exports, imports = raw['BOPTEXP'], raw['BOPTIMP']
        if exports is None or imports is None:
            continue
        index = monthly_index(at)
        row = {'date': at, 'period': at[:7], 'exports': exports / 1000, 'imports': imports / 1000, 'trade_balance': (exports - imports) / 1000}
        for field, key in (('exports', 'BOPTEXP'), ('imports', 'BOPTIMP')):
            previous = monthly_by_index.get(index - 12, {}).get(key)
            row[field + '_yoy'] = (raw[key] / previous - 1) * 100 if previous is not None and previous > 0 else None
            values = [monthly_by_index.get(n, {}).get(key) for n in range(index - 2, index + 1)]
            row[field + '_ma3'] = sum(values) / 3000 if all(value is not None for value in values) else None
        monthly.append(row)

    daily = groups.get('daily', {}).get('rows', [])
    averages = {}
    if daily:
        latest = daily[-1]['date']
        for row in daily:
            # Exclude the current incomplete quarter; no projection from a
            # handful of daily quotes into an apparent complete-quarter average.
            if quarter_index(row['date']) >= quarter_index(latest):
                continue
            at = date.fromisoformat(row['date'])
            key = f'{at.year}-{((at.month - 1) // 3) * 3 + 1:02d}-01'
            values = averages.setdefault(key, {'DTWEXBGS': [], 'DEXUSEU': []})
            for series_id in values:
                if row[series_id] is not None:
                    values[series_id].append(row[series_id])
    quarterly = []
    base = sorted(base_rows, key=lambda row: row['date'])
    for i, original in enumerate(base):
        at = original['date']
        row = {'date': at, 'period': quarter_label(at)}
        window = base[i - periods + 1:i + 1] if i >= periods - 1 else []
        consecutive = len(window) == periods and [quarter_index(r['date']) for r in window] == list(range(quarter_index(at) - periods + 1, quarter_index(at) + 1))
        ca = [r.get('current_account') for r in window]
        gdp = [lookup.get('quarterly', {}).get(r['date'], {}).get('GDP') for r in window]
        # GDP is USD billions at an annual rate. Divide each quarter by four
        # before adding the selected period's GDP denominator.
        row['current_account_gdp_pct'] = 100 * sum(ca) / (sum(gdp) / 4) if consecutive and all(v is not None for v in ca + gdp) and sum(gdp) > 0 else None
        for field, asset, liability in (
            ('net_direct', 'IEAADI', 'IEAIDI'), ('net_equity', 'IEAAPIE', 'IEAIPIE'),
            ('net_debt', 'IEAAPID', 'IEAIPID'), ('net_other', 'IEAAOI', 'IEAIOI'),
        ):
            assets = [lookup.get('quarterly', {}).get(r['date'], {}).get(asset) for r in window]
            liabilities = [lookup.get('quarterly', {}).get(r['date'], {}).get(liability) for r in window]
            row[field] = (sum(liabilities) - sum(assets)) / 1000 if consecutive and all(v is not None for v in assets + liabilities) else None
        flows = [lookup.get('quarterly', {}).get(r['date'], {}).get('IEAAR') for r in window]
        stock = lookup.get('reserve_stock', {}).get(at, {}).get('IIPRESEQ')
        debt = lookup.get('debt', {}).get(at, {}).get('principal_due_1y')
        row['reserve_transactions'] = sum(flows) / 1000 if consecutive and all(value is not None for value in flows) else None
        row['reserve_stock'] = stock / 1000 if stock is not None else None
        row['reserves_debt_pct'] = 100 * (stock / 1000) / debt if stock is not None and debt is not None and debt > 0 else None
        # Quarter-end reserves divided by the mean monthly import bill for the
        # twelve months ENDING at that quarter, never a future month's imports.
        end_month = monthly_index(at) + 2
        imports = [monthly_by_index.get(n, {}).get('BOPTIMP') for n in range(end_month - 11, end_month + 1)]
        row['import_cover_months'] = stock / (sum(imports) / 12) if stock is not None and all(v is not None for v in imports) and sum(imports) > 0 else None
        for field, series_id in (('broad_dollar', 'DTWEXBGS'), ('eurusd', 'DEXUSEU')):
            daily_windows = [averages.get(r['date'], {}).get(series_id, []) for r in window]
            values = [value for group in daily_windows for value in group]
            row[field] = sum(values) / len(values) if consecutive and all(daily_windows) and values else None
        quarterly.append(row)
    return {
        'status': 'ready' if groups else 'unavailable', 'frequency': frequency,
        'quarterly': quarterly, 'trade': build_trade_view(monthly, frequency), 'monthly': monthly,
        'sources': [{'group': name, 'checked_at': group['checked_at'], 'url': group['url'], 'latest_observation': group['rows'][-1]['date']} for name, group in groups.items() if group['rows']],
        'warnings': snapshot.get('warnings', []) + [warning for group in groups.values() for warning in group.get('warnings', [])],
        'definitions': {
            'current_account_gdp_pct': 'Selected-period current-account sum / matching nominal GDP sum; quarterly GDP SAAR divided by four.',
            'net_flows': 'Liability incurrence minus foreign asset acquisition. Positive = net inward. Selected quarterly or rolling-four-quarter SA sum; excludes reserves and derivatives.',
            'currency': 'Average of available daily quotes within the selected complete quarter or four consecutive complete quarters; indices and exchange rates are never summed.',
            'reserves': 'BEA quarter-end reserve assets, including monetary gold at market value. Reserve transactions are separate from stock valuation changes.',
            'debt': 'Treasury Table D total principal due within one year, remaining maturity. Excludes interest and follows Treasury scheduled-payment coverage.',
            'import_cover': 'Quarter-end reserve stock / trailing twelve-month mean monthly goods-and-services imports.',
            'monthly_trade': 'Goods and services on a BOP basis, monthly SA. MA3 and YoY require consecutive calendar months; gaps remain missing.',
            'trade': 'Three complete calendar months form a quarter. Selected levels are quarterly or four-quarter sums. MA3 averages three selected quarterly observations; YoY compares with the same quarter one year earlier. Incomplete quarters are not projected; gaps stay missing.',
        },
    }


class BOPMacroService:
    def __init__(self, cache_path, enabled=True, transport=None):
        self.cache_path = Path(cache_path)
        self.enabled = enabled
        self.transport = transport or fetch_macro_text
        self.lock = RLock()
        self.snapshot = {'schema_version': 1, 'groups': {}, 'warnings': []}
        self.last_completed = None
        self.last_failure = None
        for path in (self.cache_path, SNAPSHOT):
            try:
                candidate = json.loads(path.read_text(encoding='utf-8'))
                if candidate['schema_version'] == 1 and isinstance(candidate['groups'], dict):
                    self.snapshot = candidate
                    break
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def _group(self, name):
        ids, frequency = GROUPS[name]
        url = source_url(ids)
        return {'url': url, 'checked_at': datetime.now(timezone.utc).isoformat(), 'rows': parse_csv(self.transport(url), ids, frequency)}

    def _debt(self):
        links = DebtLinks()
        links.feed(self.transport(DEBT_INDEX))
        if not links.links:
            raise BOPError('Treasury external-debt links are unavailable.')
        previous = self.snapshot['groups'].get('debt', {})
        rows = {row['date']: row for row in previous.get('rows', [])}
        recent = set(sorted(links.links)[-8:])
        requested = [(at, url) for at, url in links.links.items() if at not in rows or at in recent]
        def retrieve(item):
            at, url = item
            try:
                return {'date': at, 'principal_due_1y': parse_debt(self.transport(url), at), 'url': url}
            except DebtDateMismatch:
                return {'date': at, 'gap': True}
        warnings = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            for row in executor.map(retrieve, requested):
                if row.get('gap'):
                    rows.pop(row['date'], None)
                    warnings.append(f"Treasury's table linked for {quarter_label(row['date'])} reports another quarter; that debt-coverage observation is unavailable.")
                else:
                    rows[row['date']] = row
        if not rows:
            raise BOPError('No Treasury debt observations matched their reporting dates.')
        return {'url': DEBT_INDEX, 'checked_at': datetime.now(timezone.utc).isoformat(), 'rows': [rows[at] for at in sorted(rows)], 'warnings': warnings}

    def view(self, base_rows, refresh=False, frequency='quarterly'):
        requested_at = time.monotonic()
        with self.lock:
            recent_failure = self.last_failure is not None and time.monotonic() - self.last_failure < 30
            shared = self.last_completed is not None and self.last_completed >= requested_at
            if refresh and self.enabled and not recent_failure and not shared:
                candidate = deepcopy(self.snapshot)
                warnings = []
                def download(name):
                    return self._debt() if name == 'debt' else self._group(name)
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = {name: executor.submit(download, name) for name in (*GROUPS, 'debt')}
                    for name, future in futures.items():
                        try:
                            group = future.result()
                            old = candidate['groups'].get(name)
                            if old and (group['rows'][-1]['date'] < old['rows'][-1]['date'] or len(group['rows']) < len(old['rows'])):
                                raise BOPError('Regressed source coverage.')
                            candidate['groups'][name] = group
                        except (OSError, HTTPException, ValueError, TypeError, KeyError, BOPError):
                            warnings.append(f'{name.replace("_", " ").title()} source refresh failed; saved observations retain their original source-check time.')
                candidate['warnings'] = warnings
                if candidate['groups']:
                    self.snapshot = candidate
                    try:
                        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                        temporary = self.cache_path.with_suffix('.tmp')
                        temporary.write_text(json.dumps(candidate, allow_nan=False), encoding='utf-8')
                        temporary.replace(self.cache_path)
                    except OSError:
                        self.snapshot['warnings'].append('Fresh data is available in memory, but the local cache could not be saved.')
                self.last_failure = time.monotonic() if warnings else None
                self.last_completed = time.monotonic()
            return build_macro_view(self.snapshot, base_rows, frequency)
