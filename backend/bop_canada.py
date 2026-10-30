"""Canadian external accounts from current Statistics Canada and BIS releases."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import csv
from datetime import date, datetime, timezone
from http.client import HTTPException
import io
import json
import math
from pathlib import Path
import re
from threading import RLock
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import zipfile
from .bop import BOPError, COUNTRIES, FIELDS, aggregate, numeric, quarter_index, quarter_label
from .bop_japan import strict_sum
from .bop_macro import monthly_index, build_trade_view

SNAPSHOT = Path(__file__).parent / 'snapshots' / 'canada_bop.json'
SPEC = json.loads((SNAPSHOT.parent / 'canada_series.json').read_text(encoding='utf-8'))
CURRENCY = 'https://stats.bis.org/api/v1/data/WS_EER/M.N.B.CA?format=csv'
GROUP_TABLES = {'accounts': ('36100016',), 'investment': ('36100472',), 'gdp': ('36100104',),
                'positions': ('36100485',), 'debt': ('36100446',), 'trade': ('12100011', '12100144'), 'currency': ()}
GROUP_NAMES = {'accounts': 'Statistics Canada quarterly current and capital accounts',
               'investment': 'Statistics Canada quarterly financial account',
               'gdp': 'Statistics Canada unadjusted current-price expenditure GDP',
               'positions': 'Statistics Canada quarter-end official international reserves',
               'debt': 'Statistics Canada short-term gross external debt, all currencies',
               'trade': 'Statistics Canada monthly goods and services trade and customs comparison',
               'currency': 'BIS broad nominal Canadian dollar index'}
GROUP_FIELDS = {name: tuple(field for table in tables for field in SPEC[table]['fields']) for name, tables in GROUP_TABLES.items()}
GROUP_FIELDS['currency'] = ('currency_index',)


def table_url(pid): return 'https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=' + pid + '01'
def download_url(pid): return 'https://www150.statcan.gc.ca/n1/tbl/csv/' + pid + '-eng.zip'


def fetch_canada(url):
    parts = urlsplit(url)
    allowed = {download_url(pid) for pid in SPEC} | {CURRENCY}
    if parts.scheme != 'https' or url not in allowed: raise BOPError('Canadian source URL left the official-source allowlist.')
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; MeridianLocalDashboard/1.0)', 'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}
    with urlopen(Request(url, headers=headers), timeout=30) as response: body = response.read(8_000_001)
    if len(body) > 8_000_000: raise BOPError('Canadian source exceeded the download limit.')
    return body


def parse_statcan(body, pid):
    definition = SPEC[pid]; wanted = {spec['vector']: (field, spec) for field, spec in definition['fields'].items()}
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            name = pid + '.csv'; member = archive.getinfo(name)
            if member.file_size > 64_000_000 or len(archive.namelist()) > 20 or archive.namelist().count(name) != 1:
                raise BOPError('Statistics Canada returned an invalid or oversized archive.')
            content = archive.read(member).decode('utf-8-sig')
    except (zipfile.BadZipFile, KeyError, UnicodeError) as exc: raise BOPError('Statistics Canada did not return its expected CSV archive.') from exc
    reader = csv.DictReader(io.StringIO(content)); required = {'REF_DATE', 'VECTOR', 'COORDINATE', 'VALUE', 'STATUS', 'SYMBOL', 'TERMINATED', 'DECIMALS'}
    required.update(key for _, spec in wanted.values() for key in spec['metadata'])
    if not required.issubset(reader.fieldnames or []) or len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise BOPError('Statistics Canada changed its CSV columns.')
    rows, dates, metadata = {}, {field: set() for field in definition['fields']}, []
    count = 0
    for item in reader:
        count += 1
        if count > 300_000: raise BOPError('Statistics Canada returned too many observations.')
        if item['VECTOR'] not in wanted: continue
        field, spec = wanted[item['VECTOR']]
        if item['COORDINATE'] != spec['coordinate'] or any(item.get(key) != value for key, value in spec['metadata'].items()):
            raise BOPError('Statistics Canada changed a Canadian series definition, unit, scale or adjustment.')
        if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', item['REF_DATE']): raise BOPError('Statistics Canada changed its native dates.')
        at = item['REF_DATE'] + '-01'; parsed = date.fromisoformat(at)
        if parsed >= date.today().replace(day=1) or definition['frequency'] == 'quarterly' and parsed.month not in (1, 4, 7, 10):
            raise BOPError('Statistics Canada returned future or incorrectly spaced periods.')
        if at in dates[field]: raise BOPError('Statistics Canada returned duplicate series observations.')
        dates[field].add(at)
        if item['TERMINATED'] not in ('',): raise BOPError('A requested Canadian series was terminated.')
        status, value = item['STATUS'], item['VALUE']
        if status not in ('', 'p', 'r', 'A', 'B', 'C', 'D', 'E', 'F', 'x', '..', '...'):
            raise BOPError('Statistics Canada returned an unknown observation status.')
        if status in ('F', 'x', '..', '...') and value.strip(): raise BOPError('A suppressed Canadian observation unexpectedly contains a value.')
        # The selected native units are validated as million Canadian dollars.
        # This GDP vector is an actual unadjusted quarter, never an annual rate.
        number = numeric(value)
        if at >= '1997-01-01': rows.setdefault(at, {'date': at})[field] = number / 1000 if number is not None else None
    for field, spec in definition['fields'].items():
        ordered = sorted(dates[field]); index = monthly_index if definition['frequency'] == 'monthly' else quarter_index
        if not ordered or ordered[0] != spec['first_date'] + '-01' or [index(at) for at in ordered] != list(range(index(ordered[0]), index(ordered[-1]) + 1)):
            raise BOPError('Statistics Canada omitted or truncated a requested Canadian series.')
        if not any(row.get(field) is not None for row in rows.values()): raise BOPError('A requested Canadian series has no usable observations.')
        metadata.append({'field': field, 'id': spec['vector'], 'coordinate': spec['coordinate'], 'native_unit': 'Million CAD',
                         'frequency': definition['frequency'].title(), 'adjustment': 'NSA', 'name': ' / '.join(spec['metadata'].values()),
                         'url': table_url(pid), 'download_url': download_url(pid), 'latest_source_period': ordered[-1]})
    return [{**{field: None for field in definition['fields']}, **rows[at]} for at in sorted(rows)], metadata


def parse_bis_cad(body):
    reader = csv.DictReader(io.StringIO(body.decode('utf-8-sig')))
    required = {'FREQ', 'EER_TYPE', 'EER_BASKET', 'REF_AREA', 'UNIT_MEASURE', 'COLLECTION', 'TIME_PERIOD', 'OBS_VALUE', 'OBS_STATUS'}
    if not required.issubset(reader.fieldnames or []): raise BOPError('BIS changed the Canadian dollar index schema.')
    rows, seen = [], set()
    for item in reader:
        if tuple(item[key] for key in ('FREQ', 'EER_TYPE', 'EER_BASKET', 'REF_AREA', 'UNIT_MEASURE', 'COLLECTION')) != ('M', 'N', 'B', 'CA', '882', 'A'):
            raise BOPError('BIS changed the CAD country, frequency, basket or units.')
        if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', item['TIME_PERIOD']): raise BOPError('BIS changed its CAD reporting dates.')
        at = item['TIME_PERIOD'] + '-01'
        if at in seen or at >= date.today().replace(day=1).isoformat(): raise BOPError('BIS returned duplicate or incomplete CAD months.')
        seen.add(at); value = numeric(item['OBS_VALUE'])
        if value is not None and (value <= 0 or item['OBS_STATUS'] not in ('A', 'P', 'E')): raise BOPError('BIS returned an invalid CAD index value.')
        if at >= '1997-01-01': rows.append({'date': at, 'currency_index': value})
    rows.sort(key=lambda row: row['date'])
    if not rows or rows[0]['date'] != '1997-01-01' or [monthly_index(row['date']) for row in rows] != list(range(monthly_index(rows[0]['date']), monthly_index(rows[-1]['date']) + 1)) or not any(row['currency_index'] is not None for row in rows):
        raise BOPError('BIS omitted or truncated CAD index history.')
    return rows


IDENTITIES = {
    'accounts': {'current_account': ('current_account', [('goods', 1), ('services', 1), ('primary_income', 1), ('secondary_income', 1)]),
                 'primary_income': ('primary_income', [('income_receipts', 1), ('income_payments', -1)]),
                 'net_lending': ('net_lending_published', [('current_account', 1), ('capital_account', 1)])},
    'investment': {'portfolio_assets': ('portfolio_assets', [('equity_assets', 1), ('debt_assets', 1)]),
                   'portfolio_liabilities': ('portfolio_liabilities', [('equity_liabilities', 1), ('debt_liabilities', 1)]),
                   'assets_total': ('assets_total', [('direct_assets', 1), ('portfolio_assets', 1), ('other_assets', 1), ('reserve_transactions', 1)]),
                   'liabilities_total': ('liabilities_total', [('direct_liabilities', 1), ('portfolio_liabilities', 1), ('other_liabilities', 1)]),
                   'financial_account': ('financial_account', [('assets_total', 1), ('liabilities_total', -1)])},
    'positions': {'reserve_components': ('reserve_stock', [(field, 1) for field in ('reserve_securities', 'reserve_deposits', 'reserve_gold', 'reserve_sdr', 'reserve_imf', 'reserve_other')])},
    'debt': {'debt_components': ('debt_total', [('short_term_external_debt', 1), ('debt_long_term', 1), ('debt_intercompany', 1)])},
    'trade': {'services_balance': ('services_balance', [('services_exports', 1), ('services_imports', -1)])},
    'joined': {'external_accounts': ('statistical_discrepancy', [('financial_account', 1), ('net_lending_published', -1)])},
}


def validate_canada(rows, group):
    checks = []
    for name, (total, terms) in IDENTITIES.get(group, {}).items():
        errors = [abs(row[total] - sum(row[field] * sign for field, sign in terms)) for row in rows
                  if row.get(total) is not None and all(row.get(field) is not None for field, _ in terms)]
        # Native amounts are independently rounded to CAD 1 million. Permit only
        # the sum of the half-unit rounding bounds for the reported terms.
        tolerance = (len(terms) + 1) * .0005 + 1e-8
        if not errors or max(errors) > tolerance: raise BOPError(f'Canadian accounting identity failed: {name}.')
        checks.append({'name': name, 'observations_checked': len(errors), 'max_difference_cad_billions': max(errors), 'tolerance_cad_billions': tolerance})
    return {'status': 'passed', 'checks': checks}


def build_canada_view(snapshot, frequency='trailing4q'):
    if frequency not in ('quarterly', 'trailing4q'): raise ValueError('Choose quarterly or trailing4q.')
    groups = snapshot['groups']; accounts = groups.get('accounts', {}).get('rows', [])
    if not accounts: raise BOPError('No verified Canadian accounts are available. Refresh to retry Statistics Canada.')
    validation = validate_canada(accounts, 'accounts')
    for name in ('investment', 'positions', 'debt', 'trade'):
        if groups.get(name, {}).get('rows'): validation['checks'].extend(validate_canada(groups[name]['rows'], name)['checks'])
    lookup = {name: {row['date']: row for row in group['rows']} for name, group in groups.items()}
    raw_trade = groups.get('trade', {}).get('rows', [])
    customs = {row['date']: row for row in build_trade_view([{'date': row['date'], 'exports': row.get('customs_exports'), 'imports': row.get('customs_imports')} for row in raw_trade], 'quarterly')}
    base = []
    for source in accounts:
        if any(source.get(field) is None for field in ('current_account', 'goods', 'services', 'primary_income', 'secondary_income')): continue
        at = source['date']; investment = lookup.get('investment', {}).get(at, {})
        row = {**{field: None for field in FIELDS}, **source, **investment, 'date': at, 'period': quarter_label(at)}
        row['customs_exports'] = customs.get(at, {}).get('exports'); row['customs_imports'] = customs.get(at, {}).get('imports')
        row['customs_goods'] = row['customs_exports'] - row['customs_imports'] if row['customs_exports'] is not None and row['customs_imports'] is not None else None
        row['goods_gap'] = row['customs_goods'] - row['goods'] if row['customs_goods'] is not None else None
        row['net_lending'] = source.get('net_lending_published')
        row['net_financial_inflow'] = -row['financial_account'] if row['financial_account'] is not None else None
        base.append(row)
    if groups.get('investment', {}).get('rows'): validation['checks'].extend(validate_canada(base, 'joined')['checks'])
    selected = aggregate(base, frequency)
    if not selected: raise BOPError('No complete Canadian BOP periods are available.')
    periods = 4 if frequency == 'trailing4q' else 1; macro, windows = [], {}
    currencies = {monthly_index(row['date']): row['currency_index'] for row in groups.get('currency', {}).get('rows', [])}
    for index, source in enumerate(base):
        at = source['date']; q = quarter_index(at); window = base[max(0, index - periods + 1):index + 1]
        if [quarter_index(row['date']) for row in window] != list(range(q - periods + 1, q + 1)): window = []
        windows[at] = window; ca = strict_sum(window, 'current_account')
        gdp = strict_sum([lookup.get('gdp', {}).get(row['date'], {}) for row in window], 'gdp')
        def inward(category):
            assets, liabilities = strict_sum(window, category + '_assets'), strict_sum(window, category + '_liabilities')
            return liabilities - assets if assets is not None and liabilities is not None else None
        stock = lookup.get('positions', {}).get(at, {}).get('reserve_stock')
        debt = lookup.get('debt', {}).get(at, {}).get('short_term_external_debt')
        indices = [currencies.get(month) for month in range(q * 3 - (periods - 1) * 3, q * 3 + 3)]
        macro.append({'date': at, 'period': quarter_label(at),
                      'current_account_gdp_pct': ca / gdp * 100 if ca is not None and gdp is not None and gdp > 0 else None,
                      **{'net_' + key: inward(key) for key in ('direct', 'equity', 'debt', 'other')},
                      'reserve_transactions': strict_sum(window, 'reserve_transactions'), 'reserve_stock': stock, 'short_term_external_debt': debt,
                      'reserves_debt_pct': stock / debt * 100 if stock is not None and debt is not None and debt > 0 else None,
                      'currency_index': sum(indices) / len(indices) if all(value is not None for value in indices) else None})
    monthly = []
    for raw in raw_trade:
        row = deepcopy(raw)
        for field in ('exports', 'imports'):
            values = [raw.get('goods_' + field), raw.get('services_' + field)]
            row[field] = sum(values) if all(value is not None for value in values) else None
        monthly.append(row)
    sources = [{'group': name, 'name': GROUP_NAMES[name], 'url': group['url'], 'checked_at': group['checked_at'], 'retrieved_at': group['checked_at'],
                'latest_observation': group['rows'][-1]['date'], 'series': group.get('series', []), 'downloads': group.get('downloads', [])}
               for name, group in groups.items() if group.get('rows')]
    contributors = windows[selected[-1]['date']]
    return {'country': deepcopy(next(item for item in COUNTRIES if item['code'] == 'CAN')), 'countries': deepcopy(COUNTRIES),
            'status': 'ready', 'frequency': frequency, 'units': 'CAD billions', 'reporting_currency': 'CAD', 'seasonal_adjustment': 'Not seasonally adjusted',
            'retrieved_at': groups['accounts']['checked_at'], 'last_successful_check_at': groups['accounts']['checked_at'], 'reference_url': table_url('36100016'),
            'series': selected, 'latest': selected[-1], 'period_start': contributors[0]['period'], 'period_end': selected[-1]['period'],
            'contributing_quarters': deepcopy(contributors), 'latest_quarter': deepcopy(base[-1]), 'insights': [], 'validation': validation, 'sources': sources,
            'methodology': [
                'Statistics Canada current/capital accounts and financial flows are NSA, in million Canadian dollars converted to CAD billions. Published values are retained; accounting identities allow only native independent rounding bounds.',
                'CA/GDP uses matching unadjusted nominal expenditure GDP, an actual quarterly amount, not its separately published seasonally adjusted annual-rate series. Rolling 4Q sums four current accounts and four GDP amounts; GDP is never divided by four.',
                'Net inward investment equals liabilities incurred minus assets acquired. Portfolio equity includes investment fund shares; portfolio debt includes bonds and money-market instruments. Other investment uses the published Canadian/foreign totals, including banking. Reserves are excluded; this table does not separately identify derivatives.',
                'Official reserve transactions preserve the acquisition sign and exclude valuation changes. IIP reserve stocks, including monetary gold where held, and gross external debt are in CAD at quarter end. Debt has original maturity of one year or less; intercompany debt without a maturity allocation is excluded. Stocks and ratios are never summed.',
                'The customs comparison uses unadjusted all-country merchandise exports minus imports versus unadjusted BOP goods. Canadian customs imports and exports are on an FOB basis; economic ownership, coverage, valuation, timing and revisions still affect the gap.',
                'Monthly BOP goods plus services are combined only where both sources have observations, without annual interpolation. Three complete months form a quarter; selected totals, 3Q averages and year-on-year growth are calculated before range filtering. Incomplete quarters remain excluded.',
                'The direct BIS broad nominal CAD index is monthly, 2020=100, separate from the Bank of Canada CEER basket. Three/twelve complete months form quarter/4Q averages; higher means stronger CAD.',
            ],
            'macro': {'country_code': 'CAN', 'status': 'ready' if len(groups) == len(GROUP_NAMES) else 'partial', 'frequency': frequency,
                      'quarterly': macro, 'trade': build_trade_view(monthly, frequency), 'monthly': monthly, 'sources': sources, 'warnings': list(snapshot.get('warnings', []))}}


class CanadaBOPService:
    def __init__(self, cache_path, enabled=True, transport=None):
        self.cache_path = Path(cache_path); self.enabled = enabled; self.transport = transport or fetch_canada
        self.lock = RLock(); self.last_completed = self.last_failure = self.last_attempt_at = None
        self.snapshot = {'schema_version': 1, 'country': 'CAN', 'groups': {}, 'warnings': []}
        for path in (self.cache_path, SNAPSHOT):
            try:
                candidate = json.loads(path.read_text(encoding='utf-8'))
                if candidate['schema_version'] != 1 or candidate['country'] != 'CAN': continue
                build_canada_view(candidate); self.snapshot = candidate; break
            except (OSError, ValueError, KeyError, TypeError, BOPError): continue

    def _download(self, name):
        if name == 'currency':
            rows = parse_bis_cad(self.transport(CURRENCY)); url = CURRENCY; downloads = [url]
            metadata = [{'field': 'currency_index', 'id': 'M.N.B.CA', 'name': 'BIS broad nominal effective CAD index', 'native_unit': '2020 = 100', 'frequency': 'Monthly', 'url': 'https://data.bis.org/topics/EER/BIS,WS_EER,1.0/M.N.B.CA'}]
        else:
            observations, metadata, downloads = {}, [], []
            for pid in GROUP_TABLES[name]:
                download = download_url(pid); downloads.append(download)
                part, details = parse_statcan(self.transport(download), pid); metadata.extend(details)
                for row in part: observations.setdefault(row['date'], {'date': row['date']}).update(row)
            rows = [{**{field: None for field in GROUP_FIELDS[name]}, **observations[at]} for at in sorted(observations)]
            url = table_url(GROUP_TABLES[name][0]); validate_canada(rows, name)
        positive = ('gdp', 'reserve_stock', 'short_term_external_debt', 'debt_total', 'debt_long_term', 'debt_intercompany', 'customs_exports', 'customs_imports', 'goods_exports', 'goods_imports', 'services_exports', 'services_imports')
        if any(row.get(field) is not None and row[field] < 0 for row in rows for field in positive): raise BOPError('A Canadian GDP, stock or trade total became negative.')
        return {'url': url, 'rows': rows, 'series': metadata, 'downloads': downloads, 'checked_at': datetime.now(timezone.utc).isoformat()}

    def view(self, frequency='trailing4q', refresh=False):
        requested_at = time.monotonic()
        with self.lock:
            backoff = self.last_failure is not None and time.monotonic() - self.last_failure < 30
            shared = self.last_completed is not None and self.last_completed >= requested_at; performed = False
            if refresh and self.enabled and not backoff and not shared:
                performed = True; self.last_attempt_at = datetime.now(timezone.utc).isoformat(); candidate, warnings = deepcopy(self.snapshot), []
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = {name: executor.submit(self._download, name) for name in GROUP_NAMES}
                    for name, future in futures.items():
                        try:
                            group = future.result(); old = candidate['groups'].get(name)
                            if old:
                                for field in GROUP_FIELDS[name]:
                                    before = {row['date'] for row in old['rows'] if row.get(field) is not None}
                                    after = {row['date'] for row in group['rows'] if row.get(field) is not None}
                                    if not before.issubset(after): raise BOPError('Canadian source coverage regressed.')
                            candidate['groups'][name] = group
                        except (OSError, HTTPException, ValueError, TypeError, KeyError, BOPError):
                            warnings.append(f'{GROUP_NAMES[name]} refresh failed; saved observations retain their original check time.')
                candidate['warnings'] = warnings
                # Revised account and financial files must reconcile together;
                # never replace the good snapshot with inconsistent vintages.
                try: build_canada_view(candidate, frequency)
                except BOPError:
                    candidate = deepcopy(self.snapshot); candidate['warnings'] = ['Canadian source updates could not be reconciled; saved observations retain their original check time.']
                    warnings = candidate['warnings']; build_canada_view(candidate, frequency)
                self.snapshot = candidate
                try:
                    self.cache_path.parent.mkdir(parents=True, exist_ok=True); temporary = self.cache_path.with_suffix('.tmp')
                    temporary.write_text(json.dumps(candidate, allow_nan=False), encoding='utf-8'); temporary.replace(self.cache_path)
                except OSError: self.snapshot['warnings'].append('Fresh Canadian data is in memory, but its local cache could not be saved.')
                self.last_failure = time.monotonic() if warnings else None; self.last_completed = time.monotonic()
            result = build_canada_view(self.snapshot, frequency)
            result.update({'refresh_available': self.enabled,
                           'delivery': 'source_checked' if refresh and not self.snapshot.get('warnings') and (performed or shared) else 'saved_snapshot',
                           'last_attempt_at': self.last_attempt_at, 'refresh_error': None, 'source_warning': None,
                           'refresh_cooldown_seconds': max(0, math.ceil(30 - (time.monotonic() - self.last_failure))) if self.last_failure else 0})
            return result
