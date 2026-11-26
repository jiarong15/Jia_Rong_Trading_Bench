"""Official US external accounts; quarterly flows, never annualized rates.

BEA international transactions are distributed by FRED. Census trade is
aggregated only from complete three-month quarters. Source vintages remain
separate: a customs/BOP gap can include revisions as well as basis adjustments.
"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import date, datetime, timezone
from html.parser import HTMLParser
import io
import json
import math
import os
from pathlib import Path
import re
from threading import RLock
import time
from urllib.request import Request
from urllib.parse import urlencode
from .bop_http import urlopen


COUNTRIES = [
    {'code': 'USA', 'name': 'United States', 'label': 'USA', 'currency': 'USD', 'connected': True},
    {'code': 'CHN', 'name': 'China', 'label': 'China', 'currency': 'CNY', 'connected': True},
    {'code': 'JPN', 'name': 'Japan', 'label': 'Japan', 'currency': 'JPY', 'connected': True},
    {'code': 'GBR', 'name': 'United Kingdom', 'label': 'UK', 'currency': 'GBP', 'connected': True},
    {'code': 'CAN', 'name': 'Canada', 'label': 'Canada', 'currency': 'CAD', 'connected': True},
    {'code': 'AUS', 'name': 'Australia', 'label': 'Australia', 'currency': 'AUD', 'connected': True},
    {'code': 'NZL', 'name': 'New Zealand', 'label': 'New Zealand', 'currency': 'NZD', 'connected': True},
    {'code': 'CHE', 'name': 'Switzerland', 'label': 'Switzerland', 'currency': 'CHF', 'connected': True},
    {'code': 'SGP', 'name': 'Singapore', 'label': 'Singapore', 'currency': 'SGD', 'connected': True},
]
SERIES = {
    'current_account': ('IEABC', 'Current account'),
    'goods': ('IEABCG', 'Goods balance (BOP)'),
    'services': ('IEABCS', 'Services balance'),
    'primary_income': ('IEABCPI', 'Primary income balance'),
    'secondary_income': ('IEABCSI', 'Secondary income balance'),
    'net_lending': ('IEANLC', 'Net lending: current + capital accounts'),
    'financial_account': ('IEANLF', 'Net lending: financial account'),
    'statistical_discrepancy': ('IEASD', 'Statistical discrepancy'),
    'income_receipts': ('IEAXI', 'Primary income receipts'),
    'income_payments': ('IEAMI', 'Primary income payments'),
    'capital_account': ('IEABCP', 'Capital account balance'),
}
FIELDS = tuple(SERIES) + ('customs_goods', 'customs_exports', 'customs_imports', 'goods_gap', 'net_financial_inflow')
FRED_URL = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id=' + ','.join(v[0] for v in SERIES.values()) + '&cosd=1999-01-01'
CENSUS_URL = 'https://www.census.gov/foreign-trade/balance/c0004.html'
REFERENCE_URL = 'https://www.cfr.org/articles/chinas-customs-bop-gap-is-not-a-customs-problem'
SNAPSHOT = Path(__file__).parent / 'snapshots' / 'usa_bop.json'
MONTHS = {name: i + 1 for i, name in enumerate(('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'))}


class BOPError(Exception):
    pass


def quarter_index(value):
    at = date.fromisoformat(value)
    return at.year * 4 + (at.month - 1) // 3


def quarter_label(value):
    at = date.fromisoformat(value)
    return f'{at.year} Q{(at.month - 1) // 3 + 1}'


def numeric(value):
    if value is None or str(value).strip() in ('', '.', 'NA', '(NA)'):
        return None
    result = float(str(value).replace(',', '').replace('\u2212', '-').strip())
    if not math.isfinite(result):
        raise BOPError('A source returned a non-finite observation.')
    return result


class CensusRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = []
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row = []
        elif tag in ('td', 'th'):
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split()))
            self.cell = None
        elif tag == 'tr' and self.row:
            self.rows.append(self.row)


def parse_census(html):
    if 'World, Seasonally Adjusted' not in html:
        raise BOPError('Census did not return the seasonally adjusted world trade table.')
    parser = CensusRows()
    parser.feed(html)
    months = {}
    for cells in parser.rows:
        if len(cells) != 4:
            continue
        match = re.fullmatch(r'([A-Za-z]+)\s+(\d{4})', cells[0])
        if not match or match[1] not in MONTHS:
            continue
        year, month = int(match[2]), MONTHS[match[1]]
        values = [numeric(v) for v in cells[1:]]
        if any(v is None for v in values):
            continue
        exports, imports, balance = values
        if abs(exports - imports - balance) > .25:
            raise BOPError('Census trade columns do not reconcile within published rounding.')
        key = f'{year}-{month:02d}-01'
        if key in months:
            raise BOPError('Census returned a duplicate month.')
        months[key] = {'customs_exports': exports, 'customs_imports': imports, 'customs_goods': balance}
    if not months:
        raise BOPError('No monthly observations could be read from Census.')
    quarters = {}
    for key in sorted(months):
        at = date.fromisoformat(key)
        start = ((at.month - 1) // 3) * 3 + 1
        keys = [f'{at.year}-{m:02d}-01' for m in range(start, start + 3)]
        if all(k in months for k in keys):
            quarters[keys[0]] = {field: sum(months[k][field] for k in keys) / 1000 for field in months[key]}
    return quarters, max(months)


def parse_fred(body):
    reader = csv.DictReader(io.StringIO(body.lstrip('\ufeff')))
    if not reader.fieldnames or not {'observation_date', *(v[0] for v in SERIES.values())}.issubset(reader.fieldnames):
        raise BOPError('FRED returned an unexpected series schema.')
    rows = []
    seen = set()
    for item in reader:
        at = date.fromisoformat(item['observation_date'])
        if at.day != 1 or at.month not in (1, 4, 7, 10) or at.isoformat() in seen:
            raise BOPError('FRED returned invalid or duplicate quarterly dates.')
        seen.add(at.isoformat())
        row = {'date': at.isoformat(), 'period': quarter_label(at.isoformat())}
        for field, (series, _) in SERIES.items():
            value = numeric(item[series])
            row[field] = value / 1000 if value is not None else None
        if all(row[k] is not None for k in ('current_account', 'goods', 'services', 'primary_income', 'secondary_income')):
            if abs(row['current_account'] - sum(row[k] for k in ('goods', 'services', 'primary_income', 'secondary_income'))) > .005:
                raise BOPError('The BEA current-account components do not reconcile.')
        if all(row[k] is not None for k in ('net_lending', 'financial_account', 'statistical_discrepancy')):
            if abs(row['financial_account'] - row['net_lending'] - row['statistical_discrepancy']) > .005:
                raise BOPError('The BEA financial account and statistical discrepancy do not reconcile.')
        for total, terms in (
            ('primary_income', [('income_receipts', 1), ('income_payments', -1)]),
            ('net_lending', [('current_account', 1), ('capital_account', 1)]),
        ):
            if row[total] is not None and all(row[key] is not None for key, _ in terms):
                if abs(row[total] - sum(row[key] * sign for key, sign in terms)) > .005:
                    raise BOPError('Published BEA income or capital-account figures do not reconcile within source rounding.')
        rows.append(row)
    if not rows:
        raise BOPError('FRED returned no quarterly observations.')
    return sorted(rows, key=lambda row: row['date'])


def assemble_snapshot(fred_csv, census_html, retrieved_at):
    rows = parse_fred(fred_csv)
    customs, latest_month = parse_census(census_html)
    for row in rows:
        row.update(customs.get(row['date'], {field: None for field in ('customs_goods', 'customs_exports', 'customs_imports')}))
        row['goods_gap'] = round(row['customs_goods'] - row['goods'], 6) if row['customs_goods'] is not None and row['goods'] is not None else None
        row['net_financial_inflow'] = -row['financial_account'] if row['financial_account'] is not None else None
    return {
        'schema_version': 2, 'country': 'USA', 'retrieved_at': retrieved_at,
        'units': 'USD billions', 'seasonal_adjustment': 'Seasonally adjusted',
        'census_latest_month': latest_month, 'series': rows,
        'sources': [
            {'name': 'BEA international transactions via FRED', 'url': 'https://fred.stlouisfed.org/release/tables?rid=49&eid=996', 'download_url': FRED_URL,
             'retrieved_at': retrieved_at, 'series': [{'field': field, 'id': value[0], 'name': value[1], 'url': 'https://fred.stlouisfed.org/series/' + value[0]} for field, value in SERIES.items()]},
            {'name': 'US Census: world goods trade, seasonally adjusted', 'url': CENSUS_URL, 'retrieved_at': retrieved_at, 'series': []},
        ],
    }


def aggregate(rows, frequency):
    if frequency == 'quarterly':
        return deepcopy(rows)
    if frequency != 'trailing4q':
        raise ValueError('Choose quarterly or trailing4q.')
    result = []
    for i in range(3, len(rows)):
        window = rows[i - 3:i + 1]
        # Missing calendar quarters must never become an apparent 12-month sum.
        if [quarter_index(r['date']) for r in window] != list(range(quarter_index(window[0]['date']), quarter_index(window[0]['date']) + 4)):
            continue
        row = {'date': window[-1]['date'], 'period': window[-1]['period']}
        for key in FIELDS:
            values = [r.get(key) for r in window]
            row[key] = round(sum(values), 6) if all(v is not None for v in values) else None
        result.append(row)
    return result


def dollars(value):
    return f'{"+" if value >= 0 else "-"}${abs(value):,.1f}bn'


def insights(rows, latest, frequency='trailing4q'):
    result = []
    at = latest['period']
    ca = latest['current_account']
    prior = next((r for r in rows if quarter_index(r['date']) == quarter_index(latest['date']) - 4), None)
    change = ''
    if prior and prior.get('current_account') is not None:
        delta = ca - prior['current_account']
        change = f' The balance {"improved" if delta >= 0 else "deteriorated"} by ${abs(delta):,.1f}bn compared with the {"four-quarter window" if frequency == "trailing4q" else "quarter"} a year earlier.'
    result.append({'title': 'External balance', 'text': f'The {"four-quarter" if frequency == "trailing4q" else "quarterly"} current-account balance is {dollars(ca)}, ending {at}.{change}', 'tone': 'neutral'})
    goods, services = latest.get('goods'), latest.get('services')
    if goods is not None and services is not None:
        if goods < 0 and services > 0:
            text = f'A {dollars(services)} services surplus offsets {services / abs(goods) * 100:.1f}% of the {dollars(goods)} goods deficit.'
        else:
            text = f'The goods balance is {dollars(goods)} and services contribute {dollars(services)}.'
        result.append({'title': 'Trade composition', 'text': text, 'tone': 'neutral'})
    gap = latest.get('goods_gap')
    if gap is not None:
        result.append({'title': 'Read the customs / BOP wedge', 'text': f'Census goods minus BOP goods is {dollars(gap)}. Coverage, valuation, timing and different revision vintages can create this wedge; it does not by itself identify misreporting.', 'tone': 'neutral'})
    income = latest.get('primary_income')
    if income is not None:
        result.append({'title': 'Income supports the balance' if income > 0 else 'Income adds to the deficit' if income < 0 else 'Income is balanced',
                       'text': f'Net primary income is {dollars(income)}. This includes investment income and compensation of employees; it is broader than investment income alone.', 'tone': 'neutral'})
    result.append({'title': 'FX interpretation', 'text': 'A current-account deficit implies an external financing requirement. Its currency effect also depends on capital flows, interest-rate differentials and hedging demand; this view does not produce a USD trade signal.', 'tone': 'neutral'})
    return result


def build_view(snapshot, country='USA', frequency='trailing4q'):
    if country in ('JPN', 'CHN', 'GBR', 'SGP', 'AUS', 'NZL', 'CHE', 'CAN'):
        raise BOPError('Use the country-specific BOP adapter for these external accounts.')
    selected = next((c for c in COUNTRIES if c['code'] == country), None)
    if selected is None:
        raise KeyError(country)
    base = {'country': deepcopy(selected), 'countries': deepcopy(COUNTRIES), 'frequency': frequency, 'reference_url': REFERENCE_URL}
    if not selected['connected']:
        return {**base, 'status': 'not_connected', 'message': f'{selected["name"]} BOP data is not connected yet. The USA is the first available country.', 'series': [], 'insights': [], 'sources': [], 'latest': None}
    rows = aggregate(snapshot['series'], frequency)
    complete = [r for r in rows if all(r.get(k) is not None for k in ('current_account', 'goods', 'services', 'primary_income', 'secondary_income'))]
    if not complete:
        raise BOPError('No complete current-account periods are available.')
    latest = complete[-1]
    # Do not append dates containing only a subset of the current-account series.
    rows = [r for r in rows if r['date'] <= latest['date']]
    quarter_rows = [r for r in snapshot['series'] if r['date'] <= latest['date']]
    contributors = quarter_rows[-4:] if frequency == 'trailing4q' else quarter_rows[-1:]
    return {**base, 'status': 'ready', 'units': 'USD billions', 'seasonal_adjustment': 'Seasonally adjusted',
            'retrieved_at': snapshot['retrieved_at'], 'census_latest_month': snapshot['census_latest_month'],
            'period_start': contributors[0]['period'], 'period_end': latest['period'],
            'contributing_quarters': deepcopy(contributors), 'latest_quarter': deepcopy(quarter_rows[-1]),
            'validation': validate_accounts(snapshot['series']),
            'series': rows, 'latest': latest, 'insights': insights(rows, latest, frequency), 'sources': deepcopy(snapshot['sources']),
            'methodology': [
                'All flows are seasonally adjusted, in nominal USD billions. Quarterly values are not annualized. Rolling 4Q adds four consecutive quarters; a missing input remains unavailable.',
                'Current account = goods + services + primary income + secondary income. Primary income includes investment income and employee compensation; secondary income includes current transfers.',
                'Capital-account balance is BEA\'s published IEABCP series. Published balances can differ from sums of published components by a few USD millions because of independent source rounding; these are preserved rather than forced to reconcile.',
                'Census goods are exports minus imports, summed over three complete months. Goods gap = Census balance minus BEA BOP balance. A positive gap means Census is more positive. Source revision vintages can differ.',
                'BEA sign convention: financial account = net acquisition of assets minus net incurrence of liabilities, including derivatives. Net financial inflow is its sign reversal. Statistical discrepancy = financial account minus (current + capital accounts).',
                'The CFR article supplies the analytical reference. These are independent US charts, with no estimated US goods adjustment. Partner-country mirror checks and bank-flow attribution require additional datasets and are not shown.',
            ]}


def validate_accounts(rows):
    identities = {
        'current_account_components': ('current_account', [('goods', 1), ('services', 1), ('primary_income', 1), ('secondary_income', 1)]),
        'primary_income': ('primary_income', [('income_receipts', 1), ('income_payments', -1)]),
        'current_and_capital': ('net_lending', [('current_account', 1), ('capital_account', 1)]),
        'financial_account': ('financial_account', [('net_lending', 1), ('statistical_discrepancy', 1)]),
    }
    checks = []
    for name, (total, terms) in identities.items():
        errors = [abs(row[total] - sum(row[key] * sign for key, sign in terms)) * 1000
                  for row in rows if row.get(total) is not None and all(row.get(key) is not None for key, _ in terms)]
        maximum = max(errors, default=0)
        if maximum > 5.000001:
            raise BOPError('Accounting validation exceeded the USD 5 million source-rounding tolerance.')
        checks.append({'name': name, 'observations_checked': len(errors), 'max_difference_usd_millions': round(maximum, 6)})
    return {'status': 'passed', 'tolerance_usd_millions': 5, 'checks': checks}


def fetch_text(url):
    # Fixed public source URLs only; no credentials or user-supplied destinations.
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; JRTradingWorkbench/1.0)', 'Accept': 'text/csv,text/html;q=0.9,*/*;q=0.5', 'Cache-Control': 'no-cache', 'Pragma': 'no-cache'})
    with urlopen(request, timeout=20) as response:
        body = response.read(5_000_001)
    if len(body) > 5_000_000:
        raise BOPError('The public source response exceeded the download limit.')
    return body.decode('utf-8-sig')


def fetch_fred(transport, api_key=None):
    """Use the registered FRED API when configured; public CSV needs no key."""
    warning = None
    if api_key:
        def series_observations(item):
            series_id = item[0]
            query = urlencode({'series_id': series_id, 'api_key': api_key, 'file_type': 'json',
                               'observation_start': '1999-01-01', 'units': 'lin', 'limit': 100000})
            response = json.loads(transport('https://api.stlouisfed.org/fred/series/observations?' + query))
            observations = response['observations']
            if int(response['count']) != len(observations):
                raise BOPError('The FRED API returned an incomplete observation page.')
            dates = [item['date'] for item in observations]
            if len(dates) != len(set(dates)):
                raise BOPError('The FRED API returned duplicate observation dates.')
            return series_id, {item['date']: item['value'] for item in observations}
        try:
            with ThreadPoolExecutor(max_workers=4) as executor:
                values = dict(executor.map(series_observations, SERIES.values()))
            dates = sorted({at for observations in values.values() for at in observations})
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(['observation_date', *values])
            for at in dates:
                writer.writerow([at, *(observations.get(at, '') for observations in values.values())])
            body = buffer.getvalue()
            parse_fred(body)
            return body, 'FRED API', None
        except (OSError, ValueError, TypeError, KeyError, BOPError):
            # Never expose authenticated URLs or provider error bodies.
            warning = 'FRED API unavailable; the public FRED download was used instead.'
    return transport(FRED_URL), 'FRED public CSV', warning


class BOPService:
    def __init__(self, cache_path, enabled=True, transport=None, api_key=None):
        self.cache_path = Path(cache_path)
        self.enabled = enabled
        self.transport = transport or fetch_text
        self.api_key = api_key if api_key is not None else os.environ.get('FRED_API_KEY')
        self.lock = RLock()
        self.last_attempt = None
        self.last_error = None
        self.last_completed = None
        self.last_attempt_at = None
        self.snapshot = None
        for path in (self.cache_path, SNAPSHOT):
            if not path.exists():
                continue
            try:
                candidate = json.loads(path.read_text(encoding='utf-8'))
                if candidate['schema_version'] != 2 or candidate['country'] != 'USA':
                    continue
                build_view(candidate)
                self.snapshot = candidate
                break
            except (OSError, KeyError, ValueError, TypeError, BOPError):
                continue

    def view(self, country='USA', frequency='trailing4q', refresh=False):
        if country in ('JPN', 'CHN', 'GBR', 'SGP', 'AUS', 'NZL', 'CHE', 'CAN'):
            raise BOPError('Use the country-specific BOP adapter for these external accounts.')
        requested_at = time.monotonic()
        with self.lock:
            selected = next((c for c in COUNTRIES if c['code'] == country), None)
            if selected is None:
                raise KeyError(country)
            if not selected['connected']:
                return build_view(None, country, frequency)
            performed = False
            failure_backoff = self.last_error and self.last_attempt is not None and time.monotonic() - self.last_attempt < 30
            shared_check = self.last_completed is not None and self.last_completed >= requested_at
            if refresh and self.enabled and not failure_backoff and not shared_check:
                performed = True
                self.last_attempt = time.monotonic()
                self.last_attempt_at = datetime.now(timezone.utc).isoformat()
                try:
                    fred_csv, method, warning = fetch_fred(self.transport, self.api_key)
                    candidate = assemble_snapshot(fred_csv, self.transport(CENSUS_URL), datetime.now(timezone.utc).isoformat())
                    candidate['sources'][0]['access_method'] = method
                    candidate['source_warning'] = warning
                    if self.snapshot and candidate['series'][-1]['date'] < self.snapshot['series'][-1]['date']:
                        raise BOPError('The source returned an older vintage; the saved snapshot was retained.')
                    if self.snapshot and len(candidate['series']) < len(self.snapshot['series']):
                        raise BOPError('The source history was truncated; the saved snapshot was retained.')
                    if self.snapshot and candidate['census_latest_month'] < self.snapshot['census_latest_month']:
                        raise BOPError('The source returned older customs data; the saved snapshot was retained.')
                    build_view(candidate)
                    self.snapshot = candidate
                    self.last_error = None
                    try:
                        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                        temporary = self.cache_path.with_suffix('.tmp')
                        temporary.write_text(json.dumps(candidate, allow_nan=False), encoding='utf-8')
                        temporary.replace(self.cache_path)
                    except OSError:
                        self.snapshot['source_warning'] = ' '.join(filter(None, (warning, 'Fresh USA data is in memory, but the local cache could not be saved.')))
                except (OSError, ValueError, BOPError):
                    self.last_error = 'Official-source refresh failed. The last verified snapshot is retained with its original retrieval time.'
                finally:
                    self.last_completed = time.monotonic()
            if self.snapshot is None:
                raise BOPError('No verified USA BOP snapshot is available. Use Refresh data to retry the official sources.')
            result = build_view(self.snapshot, country, frequency)
            result.update({'refresh_error': self.last_error,
                           'delivery': 'source_checked' if refresh and not self.last_error and (performed or shared_check) else 'saved_snapshot',
                           'last_attempt_at': self.last_attempt_at, 'last_successful_check_at': self.snapshot['retrieved_at'],
                           'source_warning': self.snapshot.get('source_warning'),
                           'refresh_available': self.enabled,
                           'refresh_cooldown_seconds': max(0, math.ceil(30 - (time.monotonic() - self.last_attempt))) if self.last_error and self.last_attempt is not None else 0})
            return result
