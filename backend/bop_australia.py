"""Australia external accounts from current ABS releases and the direct BIS API.

AUD billions throughout. ABS retains BPM5 cash-flow signs: asset acquisition,
asset positions and current-account debits are negative in the native files.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import csv
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from http.client import HTTPException
import io
import json
import math
from pathlib import Path
import re
from threading import RLock
import time
from urllib.parse import urljoin, urlparse
from urllib.request import Request
from .bop_http import urlopen
from .bop import BOPError, COUNTRIES, FIELDS, aggregate, numeric, quarter_index, quarter_label
from .bop_macro import build_trade_view, monthly_index
from .bop_japan import quarter_sum, strict_sum
from .public_workbooks import read_xlsx_sheet

SNAPSHOT = Path(__file__).parent / 'snapshots' / 'australia_bop.json'
SPEC = json.loads((SNAPSHOT.parent / 'australia_series.json').read_text(encoding='utf-8'))
PAGES = {
    'bop': 'https://www.abs.gov.au/statistics/economy/international-trade/balance-payments-and-international-investment-position-australia/latest-release',
    'gdp': 'https://www.abs.gov.au/statistics/economy/national-accounts/australian-national-accounts-national-income-expenditure-and-product/latest-release',
    'trade': 'https://www.abs.gov.au/statistics/economy/international-trade/international-trade-goods/latest-release',
}
CURRENCY_URL = 'https://stats.bis.org/api/v1/data/WS_EER/M.N.B.AU?format=csv'
GROUP_NAMES = {'accounts': 'ABS balance of payments, original and seasonally adjusted',
               'investment': 'ABS financial account investment categories',
               'positions': 'ABS international investment position, reserve assets',
               'debt': 'ABS gross external debt liabilities by sector and maturity',
               'gdp': 'ABS seasonally adjusted nominal GDP', 'trade': 'ABS monthly BOP goods and merchandise trade',
               'currency': 'BIS broad nominal Australian dollar index, direct feed'}
GROUP_FIELDS = {**{group: tuple(field for file in files for field in file['fields']) for group, files in SPEC.items()},
                'currency': ('currency_index',)}


def fetch_australia(url):
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; JRTradingWorkbench/1.0)',
               'Accept': 'text/html,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*',
               'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}
    if url.startswith('https://stats.bis.org/'):
        headers['Accept'] = 'text/csv'
    with urlopen(Request(url, headers=headers), timeout=30) as response:
        body = response.read(8_000_001)
    if len(body) > 8_000_000:
        raise BOPError('Australian source exceeded the download limit.')
    return body


class ReleaseLinks(HTMLParser):
    def __init__(self):
        super().__init__(); self.links = set()

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            href = dict(attrs).get('href', '')
            if href.endswith('.xlsx'): self.links.add(href)


def discover_files(body, page, filenames):
    parser = ReleaseLinks(); parser.feed(body.decode('utf-8-sig'))
    base = page.rsplit('/', 1)[0] + '/'
    result = {}
    for filename in filenames:
        matches = {urljoin(page, link) for link in parser.links if urlparse(link).path.rsplit('/', 1)[-1] == filename}
        if len(matches) != 1:
            raise BOPError('The current ABS release is missing an unambiguous workbook link.')
        link = matches.pop()
        if not link.startswith(base) or urlparse(link).netloc != 'www.abs.gov.au' or urlparse(link).scheme != 'https':
            raise BOPError('ABS workbook link left the expected official release path.')
        result[filename] = link
    return result


def excel_date(value):
    try:
        number = float(value)
        if not number.is_integer() or not 1 <= number <= 100_000: raise ValueError()
        return (date(1899, 12, 30) + timedelta(days=int(number))).isoformat()
    except (ValueError, TypeError, OverflowError) as exc:
        raise BOPError('ABS returned an invalid Excel reporting date.') from exc


def _number(value):
    if value in (None, '', 'na', 'n.p.', 'np'): return None
    if value == '-': return 0.0
    try: result = numeric(value)
    except (ValueError, TypeError) as exc:
        raise BOPError('ABS returned an invalid numeric observation.') from exc
    if result is None: raise BOPError('ABS returned a non-finite numeric observation.')
    return result


def parse_abs_sheets(sheets, definition):
    """Validate published series IDs, labels, units, frequency and stock/flow type."""
    observations, metadata = {}, []
    for field, spec in definition['fields'].items():
        rows = sheets[spec['sheet']]
        if len(rows) < 11 or [row[0] for row in rows[1:10]] != ['Unit', 'Series Type', 'Data Type', 'Frequency', 'Collection Month', 'Series Start', 'Series End', 'No. Obs', 'Series ID']:
            raise BOPError('ABS changed its workbook metadata layout.')
        if rows[9].count(spec['id']) != 1:
            raise BOPError('An Australian series is missing or duplicated.')
        column = rows[9].index(spec['id'])
        actual = [rows[index][column] for index in (0, 1, 2, 3, 4)]
        expected = [spec[key] for key in ('name', 'unit', 'adjustment', 'type', 'frequency')]
        if actual != expected or spec['unit'] != '$ Millions':
            raise BOPError('ABS changed a series label, monetary unit, adjustment or stock/flow definition.')
        start, end = excel_date(rows[6][column]), excel_date(rows[7][column])
        try: expected_count = int(rows[8][column])
        except (TypeError, ValueError) as exc: raise BOPError('ABS observation count is invalid.') from exc
        seen, usable, latest = set(), False, None
        for cells in rows[10:]:
            if not cells or cells[0] is None: continue
            source_date = excel_date(cells[0]); day = date.fromisoformat(source_date)
            if day.day != 1 or (spec['frequency'] == 'Quarter' and day.month not in (3, 6, 9, 12)):
                raise BOPError('ABS dates do not match the native frequency.')
            if not start <= source_date <= end: continue
            if source_date in seen or source_date > date.today().isoformat():
                raise BOPError('ABS returned duplicate or future observations.')
            seen.add(source_date)
            value = _number(cells[column] if column < len(cells) else None)
            at = date(day.year, ((day.month - 1) // 3) * 3 + 1 if spec['frequency'] == 'Quarter' else day.month, 1).isoformat()
            if at < '1997-01-01': continue
            observations.setdefault(at, {'date': at})[field] = value * spec['multiplier'] / 1000 if value is not None else None
            if value is not None: usable, latest = True, source_date
        if len(seen) != expected_count or not usable or latest != end:
            raise BOPError('An ABS series is truncated or lacks its latest published total.')
        metadata.append({'field': field, 'id': spec['id'], 'name': spec['name'], 'native_unit': spec['unit'],
                         'frequency': spec['frequency'], 'adjustment': spec['adjustment'], 'data_type': spec['type'],
                         'multiplier': spec['multiplier'], 'sheet': spec['sheet'], 'filename': definition['filename'],
                         'latest_source_period': end})
    rows = [{**{field: None for field in definition['fields']}, **observations[at]} for at in sorted(observations)]
    return rows, metadata


def parse_abs(body, definition):
    # ABS Table 14 contains 48 MB of XML, predominantly unused cell comments
    # and VML drawings. Keep the 40 MB default for other sources and retain an
    # explicit bounded allowance for this verified workbook only.
    limit = 60_000_000 if definition['filename'] == '5302014.xlsx' else 40_000_000
    sheets = {sheet: read_xlsx_sheet(body, sheet, max_expanded_bytes=limit) for sheet in {spec['sheet'] for spec in definition['fields'].values()}}
    return parse_abs_sheets(sheets, definition)


def parse_bis_aud(body):
    reader = csv.DictReader(io.StringIO(body.decode('utf-8-sig')))
    required = {'FREQ', 'EER_TYPE', 'EER_BASKET', 'REF_AREA', 'UNIT_MEASURE', 'COLLECTION', 'TIME_PERIOD', 'OBS_VALUE', 'OBS_STATUS'}
    if not required.issubset(reader.fieldnames or []): raise BOPError('BIS returned a different index schema.')
    rows, seen = [], set()
    for item in reader:
        if tuple(item[key] for key in ('FREQ', 'EER_TYPE', 'EER_BASKET', 'REF_AREA', 'UNIT_MEASURE', 'COLLECTION')) != ('M', 'N', 'B', 'AU', '882', 'A'):
            raise BOPError('BIS changed the Australian country, basket, units or frequency.')
        if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', item['TIME_PERIOD']): raise BOPError('BIS returned an invalid month.')
        at = item['TIME_PERIOD'] + '-01'
        if at in seen or at >= date.today().replace(day=1).isoformat(): raise BOPError('BIS returned duplicate or incomplete months.')
        seen.add(at); value = _number(item['OBS_VALUE'])
        if value is not None and (value <= 0 or item['OBS_STATUS'] not in ('A', 'P', 'E')):
            raise BOPError('BIS returned an invalid or forecast index level.')
        if at >= '1997-01-01': rows.append({'date': at, 'currency_index': value})
    if not rows or not any(row['currency_index'] is not None for row in rows): raise BOPError('BIS returned no Australian index observations.')
    return sorted(rows, key=lambda row: row['date'])


def validate_australia(rows, group='accounts'):
    identities = {
        'accounts': {
            'current_account_sa': ('current_account', [('goods', 1), ('services', 1), ('primary_income', 1), ('secondary_income', 1)]),
            'current_account_original': ('current_account_nsa', [('bop_goods_nsa', 1), ('services_nsa', 1), ('primary_income_nsa', 1), ('secondary_income_nsa', 1)]),
            'primary_income_sa': ('primary_income', [('income_receipts', 1), ('income_payments', -1)]),
            'direct_inward': ('net_direct', [('direct_assets_cash', 1), ('direct_liabilities', 1)]),
            'other_inward': ('net_other', [('other_assets_cash', 1), ('other_liabilities', 1)]),
            'financial_components': ('financial_inflow', [('net_direct', 1), ('net_portfolio', 1), ('net_other', 1), ('derivatives_inflow', 1), ('reserve_cash', 1)]),
            'bop_original_reconciliation': ('current_account_nsa', [('capital_account', -1), ('financial_inflow', -1), ('statistical_discrepancy', -1)]),
        },
        'investment': {'portfolio_inward': ('portfolio_net_check', [('equity_assets_cash', 1), ('equity_liabilities', 1), ('debt_assets_cash', 1), ('debt_liabilities', 1)])},
        'positions': {'reserve_position_reconciliation': ('reserve_stock', [('reserve_opening', 1), ('reserve_transactions', 1), ('reserve_price', 1), ('reserve_fx', 1), ('reserve_other', 1)])},
        'trade': {'monthly_bop_goods': ('trade_balance', [('exports', 1), ('imports', -1)])},
    }
    checks = []
    for name, (total, terms) in identities.get(group, {}).items():
        differences = [abs(row[total] - sum(row[key] * sign for key, sign in terms)) for row in rows
                       if row.get(total) is not None and all(row.get(key) is not None for key, _ in terms)]
        maximum = max(differences, default=0)
        if not differences or maximum > .0050001: raise BOPError(f'Australian accounting validation failed: {name}.')
        checks.append({'name': name, 'observations_checked': len(differences), 'max_difference_aud_billions': maximum})
    return {'status': 'passed', 'tolerance_aud_billions': .0050001, 'checks': checks}


def build_australia_view(snapshot, frequency='trailing4q'):
    if frequency not in ('quarterly', 'trailing4q'): raise ValueError('Choose quarterly or trailing4q.')
    groups = snapshot['groups']; accounts = groups.get('accounts', {}).get('rows', [])
    if not accounts: raise BOPError('No verified Australian external accounts are available. Refresh data to retry ABS.')
    validation = validate_australia(accounts)
    for group in ('investment', 'positions', 'trade'):
        if groups.get(group, {}).get('rows'): validation['checks'].extend(validate_australia(groups[group]['rows'], group)['checks'])
    trade = groups.get('trade', {}).get('rows', [])
    customs = {row['date']: row for row in quarter_sum(trade, ('customs_exports', 'customs_imports'))}
    base = []
    for original in accounts:
        if any(original.get(field) is None for field in ('current_account', 'goods', 'services', 'primary_income', 'secondary_income')): continue
        row = {**{field: None for field in FIELDS}, **original, 'period': quarter_label(original['date'])}
        c = customs.get(row['date'], {})
        row.update({field: c.get(field) for field in ('customs_exports', 'customs_imports')})
        row['customs_goods'] = c['customs_exports'] - c['customs_imports'] if all(c.get(field) is not None for field in ('customs_exports', 'customs_imports')) else None
        row['goods_gap'] = row['customs_goods'] - row['bop_goods_nsa'] if row['customs_goods'] is not None and row.get('bop_goods_nsa') is not None else None
        row['financial_account'] = -row['financial_inflow'] if row.get('financial_inflow') is not None else None
        row['net_financial_inflow'] = row.get('financial_inflow')
        base.append(row)
    selected = aggregate(base, frequency)
    if not selected: raise BOPError('No complete Australian BOP periods are available.')
    periods = 4 if frequency == 'trailing4q' else 1
    lookup = {name: {row['date']: row for row in group['rows']} for name, group in groups.items()}
    currency = {monthly_index(row['date']): row.get('currency_index') for row in groups.get('currency', {}).get('rows', [])}
    macro, windows = [], {}
    for index, original in enumerate(base):
        at = original['date']; q = quarter_index(at)
        window = base[max(0, index - periods + 1):index + 1]
        if [quarter_index(row['date']) for row in window] != list(range(q - periods + 1, q + 1)): window = []
        windows[at] = window
        ca = strict_sum(window, 'current_account')
        gdps = [lookup.get('gdp', {}).get(row['date'], {}).get('gdp') for row in window]
        gdp = sum(gdps) if gdps and all(value is not None for value in gdps) else None
        investment = [lookup.get('investment', {}).get(row['date'], {}) for row in window]
        def inward(asset, liability):
            a, b = strict_sum(investment, asset), strict_sum(investment, liability)
            return a + b if a is not None and b is not None else None
        debt_parts = [lookup.get('debt', {}).get(at, {}).get(field) for field in ('debt_government', 'debt_central_bank', 'debt_banks', 'debt_other_financial', 'debt_other')]
        debt = sum(debt_parts) if all(value is not None for value in debt_parts) else None
        stock = lookup.get('positions', {}).get(at, {}).get('reserve_stock')
        indices = [currency.get(month) for month in range(monthly_index(at) - (periods - 1) * 3, monthly_index(at) + 3)]
        reserve = strict_sum(window, 'reserve_cash')
        macro.append({'date': at, 'period': quarter_label(at),
                      'current_account_gdp_pct': 100 * ca / gdp if ca is not None and gdp is not None and gdp > 0 else None,
                      'net_direct': strict_sum(window, 'net_direct'), 'net_other': strict_sum(window, 'net_other'),
                      'net_equity': inward('equity_assets_cash', 'equity_liabilities'), 'net_debt': inward('debt_assets_cash', 'debt_liabilities'),
                      'reserve_transactions': -reserve if reserve is not None else None, 'reserve_stock': stock,
                      'short_term_external_debt': debt, 'reserves_debt_pct': stock / debt * 100 if stock is not None and debt is not None and debt > 0 else None,
                      'currency_index': sum(indices) / len(indices) if all(value is not None for value in indices) else None})
    for row in selected: row['bop_goods_nsa'] = strict_sum(windows[row['date']], 'bop_goods_nsa')
    sources = [{'group': name, 'name': GROUP_NAMES[name], 'url': group['url'], 'checked_at': group['checked_at'],
                'retrieved_at': group['checked_at'], 'latest_observation': group['rows'][-1]['date'],
                'series': group.get('series', []), 'downloads': group.get('downloads', [])}
               for name, group in groups.items() if group.get('rows')]
    contributors = windows[selected[-1]['date']]
    return {'country': deepcopy(next(item for item in COUNTRIES if item['code'] == 'AUS')), 'countries': deepcopy(COUNTRIES),
            'status': 'ready', 'frequency': frequency, 'units': 'AUD billions', 'reporting_currency': 'AUD',
            'seasonal_adjustment': 'Seasonally adjusted', 'retrieved_at': groups['accounts']['checked_at'],
            'last_successful_check_at': groups['accounts']['checked_at'], 'reference_url': PAGES['bop'],
            'series': selected, 'latest': selected[-1], 'period_start': contributors[0]['period'], 'period_end': selected[-1]['period'],
            'contributing_quarters': deepcopy(contributors), 'latest_quarter': deepcopy(base[-1]),
            'insights': [], 'validation': validation, 'sources': sources,
            'methodology': [
                'ABS current account, goods, services and income headlines are seasonally adjusted current-price AUD. Native million AUD are divided by 1000. GDP is actual quarterly SA nominal GDP; matching quarter/4Q sums are used without dividing GDP by four.',
                'ABS retains BPM5 cash-flow signs: outward asset acquisition, asset stocks and current-account debits are negative. Investment bars add asset cash flows to liabilities incurred; portfolio equity includes fund shares. Reserves and derivatives are excluded from these investment bars.',
                'Reserve acquisition and reserve stocks reverse native ABS asset signs. Positions include gold and are quarter-end stocks. Transactions exclude price, exchange-rate and other position changes; the complete reserve-stock reconciliation is checked.',
                'Reserve coverage sums published short-term external debt of government, monetary authorities, depository corporations, other financial corporations and other sectors. This uses the gross external debt table; intercompany debt has no maturity split and is excluded. Stocks are never summed. The residual-maturity table is not substituted or carried forward.',
                'Customs comparison uses original ABS merchandise exports FOB and imports at customs value versus original BOP goods. The SA goods headline uses a separate series. Imports at customs value exclude international freight and insurance; ownership, merchanting, coverage, commodity price/timing adjustments and revisions also affect the gap.',
                'Monthly trade companion uses SA ABS BOP goods only, including non-monetary gold, from the latest monthly release. Services are quarterly and are not estimated into monthly data. Trade quarters require three published months; 4Q sums, MA3 and YoY use full history before filtering.',
                'Currency index is the direct BIS broad nominal effective AUD index M.N.B.AU, 2020=100, averaged over three/twelve complete months. This is a BIS basket, separate from the RBA trade-weighted index.',
                'Accounting reconciliation uses original current and capital accounts, original financial flows and original net errors/omissions. SA current-account headlines are not mixed into that identity. Net lending is unavailable in the mixed-basis headline API; original contributing fields remain available.',
                'Every refresh rediscovers workbook URLs on ABS latest-release pages, preserving access to new publication editions and historical revisions. Series IDs, names, AUD units, stock/flow types, adjustment, dates and declared observation counts are validated.',
            ],
            'macro': {'status': 'ready', 'country_code': 'AUS', 'currency': 'AUD', 'frequency': frequency,
                      'trade_scope': 'bop_goods', 'quarterly': macro, 'monthly': trade, 'trade': build_trade_view(trade, frequency),
                      'sources': sources, 'warnings': snapshot.get('warnings', [])}}


class AustraliaBOPService:
    def __init__(self, cache_path, enabled=True, transport=None):
        self.cache_path = Path(cache_path); self.enabled = enabled; self.transport = transport or fetch_australia
        self.lock = RLock(); self.last_completed = self.last_failure = self.last_attempt_at = None
        self.snapshot = {'schema_version': 1, 'country': 'AUS', 'groups': {}, 'warnings': []}
        for path in (self.cache_path, SNAPSHOT):
            try:
                candidate = json.loads(path.read_text(encoding='utf-8'))
                if candidate['schema_version'] != 1 or candidate['country'] != 'AUS': continue
                build_australia_view(candidate); self.snapshot = candidate; break
            except (OSError, ValueError, KeyError, TypeError, BOPError): continue

    def _download(self, name):
        if name == 'currency':
            url = CURRENCY_URL; rows = parse_bis_aud(self.transport(url)); downloads = [url]
            metadata = [{'field': 'currency_index', 'id': 'M.N.B.AU', 'name': 'BIS broad nominal effective AUD index',
                         'native_unit': '2020 = 100', 'frequency': 'Month', 'url': 'https://data.bis.org/topics/EER/BIS,WS_EER,1.0/M.N.B.AU'}]
        else:
            definitions = SPEC[name]; url = PAGES[definitions[0]['release']]
            links = discover_files(self.transport(url), url, [item['filename'] for item in definitions])
            observations, metadata, downloads = {}, [], [url]
            for definition in definitions:
                link = links[definition['filename']]; downloads.append(link)
                part, details = parse_abs(self.transport(link), definition)
                for row in part: observations.setdefault(row['date'], {'date': row['date']}).update(row)
                metadata.extend([{**item, 'url': link} for item in details])
            rows = [{**{field: None for field in GROUP_FIELDS[name]}, **observations[at]} for at in sorted(observations)]
            validate_australia(rows, name)
            if name in ('positions', 'debt', 'gdp', 'trade'):
                positive = ('reserve_stock',) if name == 'positions' else ('gdp',) if name == 'gdp' else ('exports', 'imports', 'customs_exports', 'customs_imports') if name == 'trade' else GROUP_FIELDS[name]
                if any(row.get(field) is not None and row[field] < 0 for row in rows for field in positive):
                    raise BOPError('An Australian stock or trade level became negative.')
        return {'url': url, 'rows': rows, 'series': metadata, 'downloads': downloads,
                'checked_at': datetime.now(timezone.utc).isoformat()}

    def view(self, frequency='trailing4q', refresh=False):
        requested_at = time.monotonic()
        with self.lock:
            backoff = self.last_failure is not None and time.monotonic() - self.last_failure < 30
            shared = self.last_completed is not None and self.last_completed >= requested_at
            performed = False
            if refresh and self.enabled and not backoff and not shared:
                performed = True
                self.last_attempt_at = datetime.now(timezone.utc).isoformat()
                candidate, warnings = deepcopy(self.snapshot), []
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = {name: executor.submit(self._download, name) for name in GROUP_NAMES}
                    for name, future in futures.items():
                        try:
                            group = future.result()
                            old = candidate['groups'].get(name)
                            if old:
                                for field in GROUP_FIELDS[name]:
                                    before = {row['date'] for row in old['rows'] if row.get(field) is not None}
                                    after = {row['date'] for row in group['rows'] if row.get(field) is not None}
                                    if not before.issubset(after):
                                        raise BOPError('Australian source coverage regressed.')
                            candidate['groups'][name] = group
                        except (OSError, HTTPException, ValueError, TypeError, KeyError, BOPError):
                            warnings.append(f'{GROUP_NAMES[name]} refresh failed; saved observations retain their original check time.')
                candidate['warnings'] = warnings
                build_australia_view(candidate, frequency)
                self.snapshot = candidate
                try:
                    self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = self.cache_path.with_suffix('.tmp')
                    temporary.write_text(json.dumps(candidate, allow_nan=False), encoding='utf-8')
                    temporary.replace(self.cache_path)
                except OSError:
                    self.snapshot['warnings'].append('Fresh Australian data is in memory, but the local cache could not be saved.')
                self.last_failure = time.monotonic() if warnings else None
                self.last_completed = time.monotonic()
            result = build_australia_view(self.snapshot, frequency)
            result.update({'refresh_available': self.enabled,
                           'delivery': 'source_checked' if refresh and not self.snapshot.get('warnings') and (performed or shared) else 'saved_snapshot',
                           'last_attempt_at': self.last_attempt_at, 'refresh_error': None, 'source_warning': None,
                           'refresh_cooldown_seconds': max(0, math.ceil(30 - (time.monotonic() - self.last_failure))) if self.last_failure else 0})
            return result
