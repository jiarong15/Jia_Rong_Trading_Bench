"""Japan external accounts from public BOJ, Japan Customs and Cabinet Office data.

Flows and stocks stay in JPY billions. BOJ monthly amounts are 100 million yen;
quarterly positions are already billions. No US observations or FX conversion.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import csv
from datetime import date, datetime, timezone
import io
import json
import math
from http.client import HTTPException
from pathlib import Path
import re
from threading import RLock
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from .bop import BOPError, COUNTRIES, FIELDS, aggregate, numeric, quarter_index, quarter_label
from .bop_macro import build_trade_view, monthly_index, parse_csv

SNAPSHOT = Path(__file__).parent / 'snapshots' / 'japan_bop.json'
API_ROOT = 'https://www.stat-search.boj.or.jp/api/v1/getDataCode'
CUSTOMS_URL = 'https://www.customs.go.jp/toukei/suii/html/data/d41ma.csv'
GDP_URL = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id=JPNNGDP&cosd=1996-01-01'
REFERENCE_URL = 'https://www.mof.go.jp/english/policy/international_policy/reference/balance_of_payments/ebpnet.htm'
BOP_CODES = {
    'current_account': 'BPBP6JYNCB', 'goods': 'BPBP6JYNTB', 'services': 'BPBP6JYNSN',
    'primary_income': 'BPBP6JYNPIN', 'secondary_income': 'BPBP6JYNSIN',
    'capital_account': 'BPBP6JYNCAN', 'financial_account': 'BPBP6JYNFB',
    'statistical_discrepancy': 'BPBP6JYNER', 'income_receipts': 'BPBP6JYNPIC',
    'income_payments': 'BPBP6JYNPID', 'direct_assets': 'BPBP6JYNFA13',
    'direct_liabilities': 'BPBP6JYNFL13', 'equity_assets': 'BPBP6JYNFA21',
    'equity_liabilities': 'BPBP6JYNFL21', 'debt_assets': 'BPBP6JYNFA22',
    'debt_liabilities': 'BPBP6JYNFL22', 'other_assets': 'BPBP6JYNFA3',
    'other_liabilities': 'BPBP6JYNFL3', 'reserve_transactions': 'BPBP6JYNFA4',
    'current_account_sa': 'BPBP6JYACBX12', 'goods_exports_sa': 'BPBP6JYAEXX12',
    'goods_imports_sa': 'BPBP6JYAIMX12', 'services_exports_sa': 'BPBP6JYASCX12',
    'services_imports_sa': 'BPBP6JYASDX12',
}
POSITION_CODES = {'reserve_stock': 'BPBP6PA47', 'debt_central_bank': 'BPBP6GEDC1',
                  'debt_banks': 'BPBP6GEDB1', 'debt_government': 'BPBP6GEDP1',
                  'debt_other': 'BPBP6GEDO1'}
CURRENCY_CODES = {'currency_index': 'FX180110001'}
BOJ_GROUPS = {
    'bop': ('BP01', BOP_CODES, 'MONTHLY', '100 million Yen', 10),
    'positions': ('BP01', POSITION_CODES, 'QUARTERLY', 'Billion Yen', 1),
    'currency': ('FM09', CURRENCY_CODES, 'MONTHLY', 'CY 2020=100', 1),
}
GROUP_NAMES = {'bop': 'Bank of Japan / MOF international transactions',
               'positions': 'Bank of Japan / MOF reserve assets and external debt',
               'currency': 'Bank of Japan nominal effective yen index',
               'gdp': 'Cabinet Office nominal GDP via FRED', 'customs': 'Japan Customs world trade'}


def boj_url(group):
    db, codes, _, _, _ = BOJ_GROUPS[group]
    return API_ROOT + '?' + urlencode({'format': 'json', 'lang': 'en', 'db': db,
                                       'startDate': '199601', 'code': ','.join(codes.values())})


def fetch_japan_text(url):
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; MeridianLocalDashboard/1.0)',
               'Accept': 'application/json,text/csv,text/html;q=0.9,*/*;q=0.5',
               'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}
    with urlopen(Request(url, headers=headers), timeout=25) as response:
        body = response.read(5_000_001)
    if len(body) > 5_000_000:
        raise BOPError('Japan source exceeded the download limit.')
    try:
        return body.decode('utf-8-sig')
    except UnicodeDecodeError:
        if url == CUSTOMS_URL:
            return body.decode('cp932')
        raise BOPError('Japan source did not return the expected text encoding.')


def parse_boj(body, group):
    db, codes, frequency, unit, divisor = BOJ_GROUPS[group]
    payload = json.loads(body)
    if payload.get('STATUS') != 200 or payload.get('NEXTPOSITION') not in (None, ''):
        raise BOPError('BOJ returned an error or truncated response.')
    if payload.get('PARAMETER', {}).get('DB', '').upper() != db:
        raise BOPError('BOJ returned a different database.')
    result = payload.get('RESULTSET', [])
    if len(result) != len(codes) or {r.get('SERIES_CODE') for r in result} != set(codes.values()):
        raise BOPError('A requested Japan series is missing or duplicated.')
    reverse = {code: field for field, code in codes.items()}
    observations, metadata = {}, []
    for series in result:
        if series.get('UNIT') != unit or series.get('FREQUENCY') != frequency:
            raise BOPError('A Japan series changed units or native frequency.')
        dates = series['VALUES']['SURVEY_DATES']
        values = series['VALUES']['VALUES']
        if not dates or len(dates) != len(values) or len(set(dates)) != len(dates):
            raise BOPError('BOJ returned invalid observation arrays.')
        field = reverse[series['SERIES_CODE']]
        usable = False
        for period, value in zip(dates, values):
            text = str(period)
            if not re.fullmatch(r'\d{6}', text):
                raise BOPError('BOJ returned an invalid observation date.')
            year, part = int(text[:4]), int(text[4:])
            if frequency == 'QUARTERLY':
                if part not in (1, 2, 3, 4):
                    raise BOPError('BOJ returned an invalid quarter.')
                part = (part - 1) * 3 + 1
            at = date(year, part, 1).isoformat()
            value = numeric(value)
            observations.setdefault(at, {'date': at})[field] = value / divisor if value is not None else None
            usable = usable or value is not None
        if not usable:
            raise BOPError('A requested Japan series has no observations.')
        metadata.append({'field': field, 'id': series['SERIES_CODE'], 'name': series['NAME_OF_TIME_SERIES'],
                         'native_unit': unit, 'frequency': frequency, 'last_update': str(series['LAST_UPDATE']),
                         'url': boj_url(group)})
    rows = [{**{field: None for field in codes}, **observations[at]} for at in sorted(observations)]
    # Drop only empty trailing periods; never interpolate interior missing values.
    while rows and all(rows[-1][field] is None for field in codes):
        rows.pop()
    if group == 'bop':
        validate_japan_accounts(rows)
    return rows, metadata


def parse_customs(body):
    if 'WORLD' not in body or 'a thousand yen' not in body:
        raise BOPError('Japan Customs returned a different table or unit.')
    rows, seen = [], set()
    for cells in csv.reader(io.StringIO(body)):
        if not cells or not re.fullmatch(r'\d{4}/\d{2}', cells[0].strip()):
            continue
        at = date.fromisoformat(cells[0].strip().replace('/', '-') + '-01').isoformat()
        if at in seen or len(cells) != 3:
            raise BOPError('Japan Customs returned duplicate dates or changed columns.')
        seen.add(at)
        exports, imports = (numeric(v) for v in cells[1:])
        # The source includes future-month placeholder rows with both values zero.
        if exports == imports == 0:
            continue
        if exports is None or imports is None or exports <= 0 or imports <= 0:
            raise BOPError('Japan Customs returned invalid trade amounts.')
        rows.append({'date': at, 'customs_exports': exports / 1_000_000,
                     'customs_imports': imports / 1_000_000,
                     'customs_goods': (exports - imports) / 1_000_000})
    if not rows:
        raise BOPError('Japan Customs returned no trade observations.')
    return sorted(rows, key=lambda row: row['date'])


def validate_japan_accounts(rows):
    identities = {
        'current_account_components': ('current_account', (('goods', 1), ('services', 1), ('primary_income', 1), ('secondary_income', 1))),
        'primary_income': ('primary_income', (('income_receipts', 1), ('income_payments', -1))),
        'financial_account': ('financial_account', (('current_account', 1), ('capital_account', 1), ('statistical_discrepancy', 1))),
    }
    checks = []
    for name, (total, terms) in identities.items():
        differences = [abs(row[total] - sum(row[key] * sign for key, sign in terms))
                       for row in rows if row.get(total) is not None and all(row.get(key) is not None for key, _ in terms)]
        maximum = max(differences, default=0)
        if maximum > .00001:
            raise BOPError('Japan BOP accounting identities failed validation.')
        checks.append({'name': name, 'observations_checked': len(differences), 'max_difference_jpy_billions': maximum})
    return {'status': 'passed', 'tolerance_jpy_billions': .00001, 'checks': checks}


def quarter_sum(monthly, fields):
    by_month = {monthly_index(row['date']): row for row in monthly}
    result = []
    for quarter in sorted({index // 3 for index in by_month}):
        window = [by_month.get(index, {}) for index in range(quarter * 3, quarter * 3 + 3)]
        at = f'{quarter // 4}-{(quarter % 4) * 3 + 1:02d}-01'
        row = {'date': at, 'period': quarter_label(at)}
        for field in fields:
            values = [item.get(field) for item in window]
            row[field] = sum(values) if all(value is not None for value in values) else None
        result.append(row)
    return result


def strict_sum(rows, field):
    values = [row.get(field) for row in rows]
    return sum(values) if values and all(value is not None for value in values) else None


def source_list(groups):
    return [{'group': name, 'name': GROUP_NAMES[name], 'checked_at': group['checked_at'],
             'retrieved_at': group['checked_at'], 'url': group['url'],
             'latest_observation': group['rows'][-1]['date'], 'series': group.get('series', [])}
            for name, group in groups.items() if group.get('rows')]


def build_japan_view(snapshot, frequency='trailing4q'):
    if frequency not in ('quarterly', 'trailing4q'):
        raise ValueError('Choose quarterly or trailing4q.')
    groups = snapshot['groups']
    if not groups.get('bop', {}).get('rows'):
        raise BOPError('No verified Japan BOP observations are available. Refresh data to retry BOJ.')
    monthly = groups['bop']['rows']
    quarters = quarter_sum(monthly, BOP_CODES)
    customs = {row['date']: row for row in quarter_sum(groups.get('customs', {}).get('rows', []), ('customs_exports', 'customs_imports', 'customs_goods'))}
    base = []
    for original in quarters:
        if not all(original.get(field) is not None for field in ('current_account', 'goods', 'services', 'primary_income', 'secondary_income')):
            continue
        row = {**{field: None for field in FIELDS}, **original}
        row.update({field: customs.get(row['date'], {}).get(field) for field in ('customs_exports', 'customs_imports', 'customs_goods')})
        row['net_lending'] = row['current_account'] + row['capital_account'] if row['capital_account'] is not None else None
        row['net_financial_inflow'] = -row['financial_account'] if row['financial_account'] is not None else None
        row['goods_gap'] = row['customs_goods'] - row['goods'] if row['customs_goods'] is not None else None
        base.append(row)
    validation = validate_japan_accounts(base)
    rows = aggregate(base, frequency)
    if not rows:
        raise BOPError('No complete Japan BOP periods are available.')
    latest = rows[-1]
    contributors = base[-4:] if frequency == 'trailing4q' else base[-1:]
    position = {row['date']: row for row in groups.get('positions', {}).get('rows', [])}
    gdp = {row['date']: row['JPNNGDP'] for row in groups.get('gdp', {}).get('rows', [])}
    currency = {monthly_index(row['date']): row.get('currency_index') for row in groups.get('currency', {}).get('rows', [])}
    periods = 4 if frequency == 'trailing4q' else 1
    macro_rows = []
    for i, original in enumerate(base):
        at = original['date']
        window = base[i - periods + 1:i + 1] if i + 1 >= periods else []
        if [quarter_index(row['date']) for row in window] != list(range(quarter_index(at) - periods + 1, quarter_index(at) + 1)):
            window = []
        row = {'date': at, 'period': quarter_label(at)}
        numerator = strict_sum(window, 'current_account_sa')
        denominator = strict_sum([{'gdp': gdp.get(item['date'])} for item in window], 'gdp')
        # Cabinet Office JPNNGDP is JPY bn SA at an ANNUAL rate.
        row['current_account_gdp_pct'] = 100 * numerator / (denominator / 4) if numerator is not None and denominator is not None and denominator > 0 else None
        for kind in ('direct', 'equity', 'debt', 'other'):
            assets = strict_sum(window, kind + '_assets')
            liabilities = strict_sum(window, kind + '_liabilities')
            row['net_' + kind] = liabilities - assets if liabilities is not None and assets is not None else None
        row['reserve_transactions'] = strict_sum(window, 'reserve_transactions')
        stock = position.get(at, {}).get('reserve_stock')
        debt_parts = [position.get(at, {}).get(field) for field in ('debt_central_bank', 'debt_banks', 'debt_government', 'debt_other')]
        debt = sum(debt_parts) if all(value is not None for value in debt_parts) else None
        row['reserve_stock'] = stock
        row['short_term_external_debt'] = debt
        row['reserves_debt_pct'] = 100 * stock / debt if stock is not None and debt is not None and debt > 0 else None
        first_month = monthly_index(window[0]['date']) if window else 0
        currency_values = [currency.get(index) for index in range(first_month, first_month + periods * 3)] if window else []
        row['currency_index'] = sum(currency_values) / len(currency_values) if currency_values and all(value is not None for value in currency_values) else None
        macro_rows.append(row)
    trade_monthly = []
    for original in monthly:
        exports = strict_sum([original], 'goods_exports_sa')
        imports = strict_sum([original], 'goods_imports_sa')
        services_exports, services_imports = original.get('services_exports_sa'), original.get('services_imports_sa')
        if any(value is None for value in (exports, imports, services_exports, services_imports)):
            continue
        exports += services_exports
        imports += services_imports
        trade_monthly.append({'date': original['date'], 'period': original['date'][:7], 'exports': exports, 'imports': imports, 'trade_balance': exports - imports})
    sources = source_list(groups)
    return {
        'country': deepcopy(next(item for item in COUNTRIES if item['code'] == 'JPN')),
        'countries': deepcopy(COUNTRIES), 'frequency': frequency, 'status': 'ready',
        'units': 'JPY billions', 'seasonal_adjustment': 'Not seasonally adjusted',
        'retrieved_at': groups['bop']['checked_at'], 'last_successful_check_at': groups['bop']['checked_at'],
        'reference_url': REFERENCE_URL, 'customs_latest_month': groups.get('customs', {}).get('rows', [{}])[-1].get('date'),
        'series': rows, 'latest': latest, 'period_start': contributors[0]['period'], 'period_end': latest['period'],
        'contributing_quarters': deepcopy(contributors), 'latest_quarter': deepcopy(base[-1]),
        'validation': validation, 'sources': sources, 'insights': [],
        'methodology': [
            'BOP components and investment/reserve transactions use BOJ BPM6 unadjusted monthly data, summed only over complete calendar quarters; amounts are JPY billions.',
            'Customs world trade is unadjusted, exports FOB and imports CIF. BOP uses economic ownership and FOB imports; the difference is not a misreporting measure.',
            'CA/GDP uses the separately published BOJ seasonally adjusted current account and matching Cabinet Office nominal GDP SA annual rate divided by four.',
            'Yen NEER is BOJ FX180110001, 2020=100. Quarterly/4Q views average three/twelve complete monthly indices; higher means stronger yen.',
            'Net inward investment = net incurrence of Japanese liabilities minus acquisition of foreign assets; equity includes fund shares. Reserves and derivatives are excluded.',
            'Reserve coverage uses BOJ quarter-end yen reserve positions / short-term external debt of central bank, banks, government and other sectors. Original maturity; intercompany lending has no maturity split and is excluded. This differs from US remaining-maturity coverage.',
            'Trade companion uses separately seasonally adjusted BOP goods plus services credits/debits, complete quarter/4Q sums, a three-plotted-quarter moving average and year-on-year changes from matching totals.',
        ],
        'macro': {'status': 'ready', 'country_code': 'JPN', 'currency': 'JPY', 'frequency': frequency,
                  'quarterly': macro_rows, 'trade': build_trade_view(trade_monthly, frequency),
                  'monthly': trade_monthly, 'sources': sources, 'warnings': snapshot.get('warnings', [])},
    }


class JapanBOPService:
    def __init__(self, cache_path, enabled=True, transport=None):
        self.cache_path = Path(cache_path)
        self.enabled = enabled
        self.transport = transport or fetch_japan_text
        self.lock = RLock()
        self.snapshot = {'schema_version': 1, 'country': 'JPN', 'groups': {}, 'warnings': []}
        self.last_completed = None
        self.last_failure = None
        self.last_attempt_at = None
        for path in (self.cache_path, SNAPSHOT):
            try:
                candidate = json.loads(path.read_text(encoding='utf-8'))
                if candidate['schema_version'] != 1 or candidate['country'] != 'JPN':
                    continue
                build_japan_view(candidate)
                self.snapshot = candidate
                break
            except (OSError, ValueError, TypeError, KeyError, BOPError):
                continue

    def _download(self, name):
        if name in BOJ_GROUPS:
            url = boj_url(name)
            rows, metadata = parse_boj(self.transport(url), name)
        elif name == 'customs':
            url = CUSTOMS_URL
            rows, metadata = parse_customs(self.transport(url)), []
        else:
            url = GDP_URL
            rows, metadata = parse_csv(self.transport(url), ('JPNNGDP',), 'quarterly'), [
                {'field': 'gdp', 'id': 'JPNNGDP', 'name': 'Cabinet Office nominal GDP, SA annual rate',
                 'native_unit': 'JPY billions, SA annual rate', 'url': 'https://fred.stlouisfed.org/series/JPNNGDP'}]
        return {'url': url, 'checked_at': datetime.now(timezone.utc).isoformat(), 'rows': rows, 'series': metadata}

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
                with ThreadPoolExecutor(max_workers=3) as executor:
                    futures = {name: executor.submit(self._download, name) for name in (*BOJ_GROUPS, 'gdp', 'customs')}
                    for name, future in futures.items():
                        try:
                            group = future.result()
                            old = candidate['groups'].get(name)
                            if old:
                                for field in (BOJ_GROUPS[name][1] if name in BOJ_GROUPS else ('JPNNGDP',) if name == 'gdp' else ('customs_exports', 'customs_imports')):
                                    before = [row['date'] for row in old['rows'] if row.get(field) is not None]
                                    after = [row['date'] for row in group['rows'] if row.get(field) is not None]
                                    if before and (not after or after[-1] < before[-1] or len(after) < len(before)):
                                        raise BOPError('Japan source coverage regressed.')
                            candidate['groups'][name] = group
                        except (OSError, HTTPException, ValueError, TypeError, KeyError, BOPError):
                            warnings.append(f'{GROUP_NAMES[name]} refresh failed; saved observations retain their original check time.')
                candidate['warnings'] = warnings
                # Validate before publishing or saving a new snapshot.
                if candidate['groups'].get('bop'):
                    build_japan_view(candidate, frequency)
                    self.snapshot = candidate
                    try:
                        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                        temporary = self.cache_path.with_suffix('.tmp')
                        temporary.write_text(json.dumps(candidate, allow_nan=False), encoding='utf-8')
                        temporary.replace(self.cache_path)
                    except OSError:
                        self.snapshot['warnings'].append('Fresh Japan data is in memory, but the local cache could not be saved.')
                self.last_failure = time.monotonic() if warnings else None
                self.last_completed = time.monotonic()
            result = build_japan_view(self.snapshot, frequency)
            warnings = self.snapshot.get('warnings', [])
            result.update({'refresh_available': self.enabled,
                           'delivery': 'source_checked' if refresh and not warnings and (performed or shared) else 'saved_snapshot',
                           'last_attempt_at': self.last_attempt_at,
                           'refresh_error': None, 'source_warning': None,
                           'refresh_cooldown_seconds': max(0, math.ceil(30 - (time.monotonic() - self.last_failure))) if self.last_failure else 0})
            return result
