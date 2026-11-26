"""New Zealand external accounts: live Stats NZ releases, Infoshare and BIS.

All monetary observations are NZD billions. Actual (NSA) current accounts and
nominal quarterly GDP share a basis. Positive inward flows = liabilities-assets.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import csv
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from http.client import HTTPException
from http.cookiejar import CookieJar
import io
import json
import math
from pathlib import Path
import re
from threading import RLock
import time
from urllib.parse import urljoin, urlparse
from urllib.request import Request, build_opener, HTTPCookieProcessor
from .bop_http import urlopen, https_handler
from uuid import uuid4
from .bop import BOPError, COUNTRIES, FIELDS, aggregate, numeric, quarter_index, quarter_label
from .bop_japan import strict_sum, quarter_sum
from .bop_macro import build_trade_view, monthly_index

SNAPSHOT = Path(__file__).parent / 'snapshots' / 'new_zealand_bop.json'
SPEC = json.loads((SNAPSHOT.parent / 'new_zealand_series.json').read_text(encoding='utf-8'))
CATALOGUE = 'https://www.stats.govt.nz/large-datasets/csv-files-for-download/'
EXPORT = 'https://infoshare.stats.govt.nz/infoshare/ExportDirect.aspx'
CURRENCY = 'https://stats.bis.org/api/v1/data/WS_EER/M.N.B.NZ?format=csv'
GROUP_NAMES = {'accounts': 'Stats NZ balance of payments, investment flows, reserves and sector debt',
               'gdp': 'Stats NZ current-price quarterly GDP',
               'trade': 'Stats NZ Infoshare monthly merchandise exports and imports',
               'currency': 'BIS broad nominal New Zealand dollar index, direct feed'}
GROUP_FIELDS = {**{group: tuple(fields) for group, fields in SPEC.items()},
                'trade': ('exports', 'imports'), 'currency': ('currency_index',)}
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; JRTradingWorkbench/1.0)',
           'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def bounded_read(response, limit):
    body = response.read(limit + 1)
    if len(body) > limit: raise BOPError('New Zealand source exceeded its download limit.')
    return body


class ExportForm(HTMLParser):
    def __init__(self):
        super().__init__(); self.fields = []; self.periods = []; self.option = None; self.selector = False

    def handle_starttag(self, tag, attrs):
        item = dict(attrs)
        if tag == 'input' and item.get('type') == 'hidden': self.fields.append((item['name'], item.get('value', '')))
        if tag == 'select': self.selector = item.get('name') == 'ctl00$MainContent$TimeVariableSelector$lbVariableOptions'
        if tag == 'option' and self.selector: self.option = item.get('value')

    def handle_endtag(self, tag):
        if tag == 'select': self.selector = False
        if tag == 'option': self.option = None

    def handle_data(self, value):
        if self.option is not None and re.fullmatch(r'(199[7-9]|20\d\d)M(0[1-9]|1[0-2])', value):
            if value[:4] <= str(date.today().year): self.periods.append(self.option)


def fetch_infoshare_trade():
    """Submit a public read-only series query using the site's fresh form state."""
    opener = build_opener(https_handler(), HTTPCookieProcessor(CookieJar()))
    with opener.open(Request(EXPORT, headers=HEADERS), timeout=30) as response:
        form_body = bounded_read(response, 1_000_000).decode('utf-8-sig')
    form = ExportForm(); form.feed(form_body)
    if not form.periods or len(form.periods) > 1500 or not any(name == '__VIEWSTATE' for name, _ in form.fields):
        raise BOPError('Stats NZ changed the Export Direct form.')
    prefix = 'ctl00$MainContent$'
    fields = form.fields + [(prefix + 'TimeVariableSelector$lbVariableOptions', value) for value in form.periods]
    fields += [(prefix + 'tbMissingText', '..'), (prefix + 'TimeVariableSelector$tbSelected', str(len(form.periods))),
               (prefix + 'rblCSVFormat', 'full'), (prefix + 'rblSeriesDescription', 'description_at_top'),
               (prefix + 'cbShowStatusCodes', 'on'), (prefix + 'btnGenerate.x', '10'), (prefix + 'btnGenerate.y', '10')]
    boundary = '----JRTradingWorkbenchNZ' + uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n' for name, value in fields]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{prefix}fuSearchFile"; filename="series.sch"\r\nContent-Type: text/plain\r\n\r\nEXPM.SCT99F\r\nIMPM.SCT99C\r\n--{boundary}--\r\n')
    request = Request(EXPORT, data=''.join(parts).encode('utf-8'), headers={**HEADERS, 'Content-Type': 'multipart/form-data; boundary=' + boundary})
    with opener.open(request, timeout=45) as response:
        if 'text/csv' not in response.headers.get('Content-Type', ''): raise BOPError('Stats NZ did not return a time-series export.')
        return bounded_read(response, 2_000_000)


def fetch_new_zealand(url):
    if url == EXPORT: return fetch_infoshare_trade()
    with urlopen(Request(url, headers={**HEADERS, 'Accept': 'text/csv' if url == CURRENCY else '*/*'}), timeout=45) as response:
        return bounded_read(response, 50_000_000 if url.endswith('.csv') and url.startswith('https://www.stats.govt.nz/assets/') else 2_000_000)


class Catalogue(HTMLParser):
    def __init__(self): super().__init__(); self.documents = []

    def handle_starttag(self, tag, attrs):
        for _, value in attrs:
            if not value or 'DocumentLink' not in value: continue
            try: content = json.loads(value)
            except ValueError: continue
            self.walk(content)

    def walk(self, item):
        if isinstance(item, dict):
            if 'DocumentLink' in item: self.documents.append(item)
            for value in item.values(): self.walk(value)
        elif isinstance(item, list):
            for value in item: self.walk(value)


def discover_csv(body, group):
    parser = Catalogue(); parser.feed(body.decode('utf-8-sig'))
    expression = r'balance-of-payments-and-international-investment-position-[a-z]+-\d{4}-quarter\.csv' if group == 'accounts' else r'gross-domestic-product-[a-z]+-\d{4}-quarter\.csv'
    links = {urljoin(CATALOGUE, item['DocumentLink']) for item in parser.documents if re.fullmatch(expression, item.get('FileName', ''))}
    if len(links) != 1: raise BOPError('Stats NZ has no unambiguous current release CSV.')
    link = links.pop()
    if urlparse(link).netloc != 'www.stats.govt.nz' or not link.startswith('https://www.stats.govt.nz/assets/Uploads/'):
        raise BOPError('Stats NZ download left its official assets path.')
    return link


def source_number(value, status=''):
    if value in (None, '', '..', 'C', 'S') or status in ('C', 'S'): return None
    try: result = numeric(value)
    except (ValueError, TypeError) as exc: raise BOPError('Stats NZ returned an invalid number.') from exc
    if result is None: raise BOPError('Stats NZ returned a non-finite number.')
    return result


def parse_stats_csv(body, group):
    reader = csv.DictReader(io.StringIO(body.decode('utf-8-sig'), newline=''))
    magnitude = 'MAGNTUDE' if group == 'accounts' else 'MAGNITUDE'
    if not {'Series_reference', 'Period', 'Data_value', 'STATUS', 'UNITS', magnitude, 'Subject', 'Group', 'Series_title_1'}.issubset(reader.fieldnames or []):
        raise BOPError('Stats NZ changed its release CSV schema.')
    definitions = {spec['id']: (field, spec) for field, spec in SPEC[group].items()}
    seen = {field: set() for field in SPEC[group]}; observations = {}; metadata = []
    for item in reader:
        if item['Series_reference'] not in definitions: continue
        field, spec = definitions[item['Series_reference']]
        if any(item[key] != spec[key] for key in ('UNITS', magnitude, 'Subject', 'Group', 'Series_title_1')):
            raise BOPError('Stats NZ changed a series unit, magnitude, adjustment or definition.')
        if item['STATUS'] not in ('F', 'R', 'P', 'C', 'S', 'FINAL', 'REVISED', 'PROVISIONAL') or item.get('Suppressed', '') not in ('', 'Y'):
            raise BOPError('Stats NZ returned an unrecognized observation status.')
        if not re.fullmatch(r'\d{4}\.(03|06|09|12)', item['Period']): raise BOPError('Stats NZ dates are not quarterly.')
        year, month = int(item['Period'][:4]), int(item['Period'][5:])
        at = date(year, month - 2, 1).isoformat()
        if at > date.today().isoformat() or at in seen[field]: raise BOPError('Stats NZ returned future or duplicate periods.')
        seen[field].add(at)
        if year < 1997: continue
        value = source_number(item['Data_value'], 'S' if item.get('Suppressed') == 'Y' else item['STATUS'])
        observations.setdefault(at, {'date': at})[field] = value / 1000 if value is not None and spec['UNITS'] == 'Dollars' else value
    for field, spec in SPEC[group].items():
        dates = sorted(seen[field])
        first = spec['first_period']
        expected_start = f'{first[:4]}-{int(first[5:]) - 2:02d}-01'
        if len(dates) < 20 or dates[0] != expected_start or [quarter_index(at) for at in dates] != list(range(quarter_index(dates[0]), quarter_index(dates[-1]) + 1)):
            raise BOPError('A requested Stats NZ quarterly series is missing or truncated.')
        if not any(row.get(field) is not None for row in observations.values()): raise BOPError('A Stats NZ series has no usable values.')
        metadata.append({'field': field, 'id': spec['id'], 'name': spec['name'], 'native_unit': spec['UNITS'],
                         'magnitude': spec[magnitude], 'frequency': 'Quarterly', 'adjustment': 'NSA', 'latest_source_period': dates[-1]})
    return [{**{field: None for field in SPEC[group]}, **observations[at]} for at in sorted(observations)], metadata


def parse_infoshare_trade(body):
    rows = list(csv.reader(io.StringIO(body.decode('utf-8-sig'), newline='')))
    observations, found = {}, set()
    for index, row in enumerate(rows):
        if not row or not row[0].startswith('Series is '): continue
        code = row[0][len('Series is '):]
        if code not in ('EXPM.SCT99F', 'IMPM.SCT99C') or code in found: raise BOPError('Stats NZ returned a different or duplicate trade series.')
        found.add(code); field = 'exports' if code.startswith('EXP') else 'imports'
        section_end = next((i for i in range(index + 1, len(rows)) if rows[i] and rows[i][0].startswith('Series is ')), len(rows))
        section = rows[index + 1:section_end]
        unit = next((item for item in section if item and item[0].startswith('Units = ')), [])
        description = ' '.join(' '.join(item) for item in section[:8])
        if unit[:3] != ['Units = $;', 'Magnitude = Units;', 'Frequency = Monthly;'] or 'All Merchandise Trade' not in description or ('fob' if field == 'exports' else 'cif') not in description.lower():
            raise BOPError('Stats NZ changed the trade currency, scale, frequency, coverage or valuation.')
        header = next((item for item in section if len(item) > 1 and item[1] == 'Jan'), [])
        if header[1:24:2] != list(MONTHS): raise BOPError('Stats NZ changed the monthly trade column layout.')
        seen = set()
        for item in section:
            if not item or not re.fullmatch(r'\d{4}', item[0]): continue
            year = int(item[0])
            if len(item) < 25: raise BOPError('Stats NZ truncated a trade year.')
            for month in range(1, 13):
                at = date(year, month, 1).isoformat(); value = item[month * 2 - 1]; status = item[month * 2]
                if at >= date.today().replace(day=1).isoformat():
                    if value not in ('', '..'): raise BOPError('Stats NZ returned an incomplete trade month.')
                    continue
                if at in seen or status not in ('', 'C', 'S', 'E', 'P', 'R'): raise BOPError('Stats NZ returned duplicate trade dates or invalid statuses.')
                seen.add(at)
                number = source_number(value, status)
                if year < 1997: continue
                if number is not None and number < 0: raise BOPError('Stats NZ returned a negative merchandise total.')
                observations.setdefault(at, {'date': at})[field] = number / 1_000_000_000 if number is not None else None
    if found != {'EXPM.SCT99F', 'IMPM.SCT99C'}: raise BOPError('Stats NZ omitted a requested trade series.')
    # Trailing unreported months are not observations; internal missing months
    # remain gaps so complete-quarter checks cannot silently fill them.
    result = [{**{'exports': None, 'imports': None}, **observations[at]} for at in sorted(observations)]
    while result and result[-1]['exports'] is None and result[-1]['imports'] is None: result.pop()
    if len(result) < 24 or result[0]['date'] != '1997-01-01' or not all(any(row[field] is not None for row in result) for field in ('exports', 'imports')):
        raise BOPError('Stats NZ returned insufficient monthly trade history.')
    return result


def parse_bis_nzd(body):
    reader = csv.DictReader(io.StringIO(body.decode('utf-8-sig')))
    required = {'FREQ', 'EER_TYPE', 'EER_BASKET', 'REF_AREA', 'UNIT_MEASURE', 'COLLECTION', 'TIME_PERIOD', 'OBS_VALUE', 'OBS_STATUS'}
    if not required.issubset(reader.fieldnames or []): raise BOPError('BIS changed its NZD index schema.')
    result, seen = [], set()
    for item in reader:
        if tuple(item[key] for key in ('FREQ', 'EER_TYPE', 'EER_BASKET', 'REF_AREA', 'UNIT_MEASURE', 'COLLECTION')) != ('M', 'N', 'B', 'NZ', '882', 'A'):
            raise BOPError('BIS changed the NZD country, frequency, basket or units.')
        if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', item['TIME_PERIOD']): raise BOPError('BIS returned an invalid month.')
        at = item['TIME_PERIOD'] + '-01'
        if at in seen or at >= date.today().replace(day=1).isoformat(): raise BOPError('BIS returned duplicate or incomplete months.')
        seen.add(at); value = source_number(item['OBS_VALUE'])
        if value is not None and (value <= 0 or item['OBS_STATUS'] not in ('A', 'P', 'E')): raise BOPError('BIS returned an invalid NZD index value.')
        if at >= '1997-01-01': result.append({'date': at, 'currency_index': value})
    if not result or not any(row['currency_index'] is not None for row in result): raise BOPError('BIS returned no NZD observations.')
    return sorted(result, key=lambda row: row['date'])


def validate_new_zealand(rows):
    identities = {
        'current_account_components': ('current_account', [('goods', 1), ('services', 1), ('primary_income', 1), ('secondary_income', 1)]),
        'goods_balance': ('goods', [('goods_exports', 1), ('goods_imports', -1)]),
        'primary_income': ('primary_income', [('income_receipts', 1), ('income_payments', -1)]),
        'portfolio_assets': ('portfolio_assets', [('equity_assets', 1), ('debt_assets', 1)]),
        'portfolio_liabilities': ('portfolio_liabilities', [('equity_liabilities', 1), ('debt_liabilities', 1)]),
        'total_assets': ('assets_total', [('direct_assets', 1), ('portfolio_assets', 1), ('derivative_assets', 1), ('other_assets', 1), ('reserve_transactions', 1)]),
        'total_liabilities': ('liabilities_total', [('direct_liabilities', 1), ('portfolio_liabilities', 1), ('derivative_liabilities', 1), ('other_liabilities', 1)]),
        'financial_inflow': ('financial_inflow', [('liabilities_total', 1), ('assets_total', -1)]),
        'external_accounts': ('current_account', [('capital_account', -1), ('financial_inflow', -1), ('statistical_discrepancy', -1)]),
    }
    checks = []
    for name, (field, terms) in identities.items():
        errors = [abs(row[field] - sum(row[key] * sign for key, sign in terms)) for row in rows
                  if row.get(field) is not None and all(row.get(key) is not None for key, _ in terms)]
        if not errors or max(errors) > .0050001: raise BOPError(f'New Zealand accounting identity failed: {name}.')
        checks.append({'name': name, 'observations_checked': len(errors), 'max_difference_nzd_billions': max(errors)})
    return {'status': 'passed', 'tolerance_nzd_billions': .0050001, 'checks': checks}


def build_new_zealand_view(snapshot, frequency='trailing4q'):
    if frequency not in ('quarterly', 'trailing4q'): raise ValueError('Choose quarterly or trailing4q.')
    groups = snapshot['groups']; accounts = groups.get('accounts', {}).get('rows', [])
    if not accounts: raise BOPError('No verified New Zealand accounts are available. Refresh to retry Stats NZ.')
    validation = validate_new_zealand(accounts)
    trade = groups.get('trade', {}).get('rows', [])
    customs = {row['date']: row for row in quarter_sum(trade, ('exports', 'imports'))}
    base = []
    for source in accounts:
        if any(source.get(field) is None for field in ('current_account', 'goods', 'services', 'primary_income', 'secondary_income')): continue
        row = {**{field: None for field in FIELDS}, **source, 'period': quarter_label(source['date'])}
        c = customs.get(row['date'], {})
        row['customs_exports'], row['customs_imports'] = c.get('exports'), c.get('imports')
        row['customs_goods'] = c['exports'] - c['imports'] if c.get('exports') is not None and c.get('imports') is not None else None
        row['goods_gap'] = row['customs_goods'] - row['goods'] if row['customs_goods'] is not None else None
        row['net_lending'] = row['current_account'] + row['capital_account'] if row.get('capital_account') is not None else None
        row['financial_account'] = -row['financial_inflow'] if row.get('financial_inflow') is not None else None
        row['net_financial_inflow'] = row.get('financial_inflow'); base.append(row)
    selected = aggregate(base, frequency)
    if not selected: raise BOPError('No complete New Zealand BOP periods are available.')
    periods = 4 if frequency == 'trailing4q' else 1
    gdps = {row['date']: row['gdp'] for row in groups.get('gdp', {}).get('rows', [])}
    currencies = {monthly_index(row['date']): row['currency_index'] for row in groups.get('currency', {}).get('rows', [])}
    macro, windows = [], {}
    for index, source in enumerate(base):
        at = source['date']; q = quarter_index(at); window = base[max(0, index - periods + 1):index + 1]
        if [quarter_index(row['date']) for row in window] != list(range(q - periods + 1, q + 1)): window = []
        windows[at] = window
        ca = strict_sum(window, 'current_account'); gdp_values = [gdps.get(row['date']) for row in window]
        gdp = sum(gdp_values) if gdp_values and all(value is not None for value in gdp_values) else None
        def inward(category):
            assets, liabilities = strict_sum(window, category + '_assets'), strict_sum(window, category + '_liabilities')
            return liabilities - assets if assets is not None and liabilities is not None else None
        parts = [source.get(field) for field in ('debt_government', 'debt_central_bank', 'debt_banks', 'debt_other')]
        debt = sum(parts) if all(value is not None for value in parts) else None
        stock = source.get('reserve_stock')
        indices = [currencies.get(month) for month in range(q * 3 - (periods - 1) * 3, q * 3 + 3)]
        macro.append({'date': at, 'period': quarter_label(at),
                      'current_account_gdp_pct': ca / gdp * 100 if ca is not None and gdp is not None and gdp > 0 else None,
                      **{'net_' + key: inward(key) for key in ('direct', 'equity', 'debt', 'other')},
                      'reserve_transactions': strict_sum(window, 'reserve_transactions'), 'reserve_stock': stock,
                      'short_term_external_debt': debt, 'reserves_debt_pct': stock / debt * 100 if stock is not None and debt is not None and debt > 0 else None,
                      'currency_index': sum(indices) / len(indices) if all(value is not None for value in indices) else None})
    sources = [{'group': name, 'name': GROUP_NAMES[name], 'url': group['url'], 'checked_at': group['checked_at'],
                'retrieved_at': group['checked_at'], 'latest_observation': group['rows'][-1]['date'],
                'series': group.get('series', []), 'downloads': group.get('downloads', [])} for name, group in groups.items() if group.get('rows')]
    contributors = windows[selected[-1]['date']]
    methodology = [
        'Stats NZ actual (NSA) quarterly accounts and matching nominal expenditure GDP are in NZD billions. Native million NZD are divided by 1000; monthly trade native NZD units by one billion. GDP is an actual quarter, never divided by four. Ratios are recalculated from the current GDP release; historical BOP-published ratios can use another vintage. Published million-NZD quarter totals can also round differently from annual totals.',
        'Net inward investment = liabilities incurred minus assets acquired, including equity fund shares. Four investment categories exclude derivatives and reserves. Positive reserve transactions mean acquisition and exclude valuation changes; stocks are separately published quarter-end IIP reserve assets.',
        'Reserve coverage uses the four published sector short-term external-debt totals from Table 20: government, central bank, deposit-taking corporations and other sectors. Direct-investment intercompany debt has no term split and is excluded. Stocks are never summed. Residual-maturity non-equity liabilities, which can include derivatives, are not substituted.',
        'Customs and BOP goods are both NSA. Merchandise exports include re-exports and are FOB; customs imports are CIF while BOP imports are FOB. Ownership, coverage, freight, timing and revisions also affect the gap.',
        'The higher-frequency source is monthly merchandise trade, goods only and NSA. Three complete months form a quarter; rolling totals, three-plotted-quarter moving averages and year-on-year growth use full history before date filtering.',
        'The direct BIS broad nominal NZD index (2020=100) is averaged over three/twelve complete months. This BIS basket is separate from the RBNZ trade-weighted index. Higher means stronger NZD.',
    ]
    return {'country': deepcopy(next(item for item in COUNTRIES if item['code'] == 'NZL')), 'countries': deepcopy(COUNTRIES),
            'status': 'ready', 'frequency': frequency, 'units': 'NZD billions', 'reporting_currency': 'NZD',
            'seasonal_adjustment': 'Not seasonally adjusted', 'retrieved_at': groups['accounts']['checked_at'],
            'last_successful_check_at': groups['accounts']['checked_at'], 'reference_url': 'https://www.stats.govt.nz/topics/balance-of-payments/',
            'series': selected, 'latest': selected[-1], 'period_start': contributors[0]['period'], 'period_end': selected[-1]['period'],
            'contributing_quarters': deepcopy(contributors), 'latest_quarter': deepcopy(base[-1]),
            'insights': [], 'validation': validation, 'sources': sources, 'methodology': methodology,
            'macro': {'country_code': 'NZL', 'status': 'ready' if len(groups) == 4 else 'partial', 'frequency': frequency,
                      'quarterly': macro, 'trade': build_trade_view(trade, frequency), 'monthly': deepcopy(trade), 'trade_scope': 'merchandise',
                      'sources': sources, 'warnings': list(snapshot.get('warnings', []))}}


class NewZealandBOPService:
    def __init__(self, cache_path, enabled=True, transport=None):
        self.cache_path = Path(cache_path); self.enabled = enabled; self.transport = transport or fetch_new_zealand
        self.lock = RLock(); self.last_completed = self.last_failure = self.last_attempt_at = None
        self.snapshot = {'schema_version': 1, 'country': 'NZL', 'groups': {}, 'warnings': []}
        for path in (self.cache_path, SNAPSHOT):
            try:
                candidate = json.loads(path.read_text(encoding='utf-8'))
                if candidate['schema_version'] != 1 or candidate['country'] != 'NZL': continue
                build_new_zealand_view(candidate); self.snapshot = candidate; break
            except (OSError, ValueError, KeyError, TypeError, BOPError): continue

    def _download(self, name):
        if name in ('accounts', 'gdp'):
            link = discover_csv(self.transport(CATALOGUE), name); url = CATALOGUE
            rows, metadata = parse_stats_csv(self.transport(link), name); downloads = [CATALOGUE, link]
            if name == 'accounts': validate_new_zealand(rows)
        elif name == 'trade':
            url = EXPORT; rows = parse_infoshare_trade(self.transport(url)); downloads = [url]
            metadata = [{'field': field, 'id': code, 'name': name_, 'native_unit': 'NZD units', 'frequency': 'Monthly', 'adjustment': 'NSA', 'url': EXPORT}
                        for field, code, name_ in [('exports', 'EXPM.SCT99F', 'All merchandise exports including re-exports, FOB'), ('imports', 'IMPM.SCT99C', 'All merchandise imports, CIF')]]
        else:
            url = CURRENCY; rows = parse_bis_nzd(self.transport(url)); downloads = [url]
            metadata = [{'field': 'currency_index', 'id': 'M.N.B.NZ', 'name': 'BIS broad nominal effective NZD index', 'native_unit': '2020 = 100', 'frequency': 'Monthly', 'url': 'https://data.bis.org/topics/EER/BIS,WS_EER,1.0/M.N.B.NZ'}]
        if any(row.get(field) is not None and row[field] < 0 for row in rows for field in ('gdp', 'reserve_stock', 'debt_government', 'debt_central_bank', 'debt_banks', 'debt_other')):
            raise BOPError('A New Zealand GDP or stock amount became negative.')
        return {'url': url, 'rows': rows, 'series': metadata, 'downloads': downloads, 'checked_at': datetime.now(timezone.utc).isoformat()}

    def view(self, frequency='trailing4q', refresh=False):
        requested_at = time.monotonic()
        with self.lock:
            backoff = self.last_failure is not None and time.monotonic() - self.last_failure < 30
            shared = self.last_completed is not None and self.last_completed >= requested_at
            performed = False
            if refresh and self.enabled and not backoff and not shared:
                performed = True; self.last_attempt_at = datetime.now(timezone.utc).isoformat()
                candidate, warnings = deepcopy(self.snapshot), []
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = {name: executor.submit(self._download, name) for name in GROUP_NAMES}
                    for name, future in futures.items():
                        try:
                            group = future.result(); old = candidate['groups'].get(name)
                            if old:
                                for field in GROUP_FIELDS[name]:
                                    before = {row['date'] for row in old['rows'] if row.get(field) is not None}
                                    after = {row['date'] for row in group['rows'] if row.get(field) is not None}
                                    if not before.issubset(after): raise BOPError('New Zealand source coverage regressed.')
                            candidate['groups'][name] = group
                        except (OSError, HTTPException, ValueError, TypeError, KeyError, BOPError):
                            warnings.append(f'{GROUP_NAMES[name]} refresh failed; saved observations retain their original check time.')
                candidate['warnings'] = warnings; build_new_zealand_view(candidate, frequency); self.snapshot = candidate
                try:
                    self.cache_path.parent.mkdir(parents=True, exist_ok=True); temporary = self.cache_path.with_suffix('.tmp')
                    temporary.write_text(json.dumps(candidate, allow_nan=False), encoding='utf-8'); temporary.replace(self.cache_path)
                except OSError: self.snapshot['warnings'].append('Fresh New Zealand data is in memory, but its local cache could not be saved.')
                self.last_failure = time.monotonic() if warnings else None; self.last_completed = time.monotonic()
            result = build_new_zealand_view(self.snapshot, frequency)
            result.update({'refresh_available': self.enabled,
                           'delivery': 'source_checked' if refresh and not self.snapshot.get('warnings') and (performed or shared) else 'saved_snapshot',
                           'last_attempt_at': self.last_attempt_at, 'refresh_error': None, 'source_warning': None,
                           'refresh_cooldown_seconds': max(0, math.ceil(30 - (time.monotonic() - self.last_failure))) if self.last_failure else 0})
            return result
