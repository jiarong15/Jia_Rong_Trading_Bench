"""UK external accounts: ONS UKEA/MRET, HMRC and the Bank of England.

Amounts remain GBP billions. ONS asset transactions use the UK cash-flow sign
(outward acquisition negative); investment balances are already net inward.
"""
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
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from .bop import BOPError, COUNTRIES, FIELDS, aggregate, numeric, quarter_index, quarter_label
from .bop_macro import build_trade_view
from .bop_japan import quarter_sum, strict_sum

SNAPSHOT = Path(__file__).parent / 'snapshots' / 'uk_bop.json'
REFERENCE_URL = 'https://www.ons.gov.uk/economy/nationalaccounts/balanceofpayments/bulletins/balanceofpayments/latest'
UKEA_URL = 'https://www.ons.gov.uk/file?uri=/economy/grossdomesticproductgdp/datasets/unitedkingdomeconomicaccounts/current/ukea.csv'
TRADE_URL = 'https://www.ons.gov.uk/file?uri=/economy/nationalaccounts/balanceofpayments/datasets/tradeingoodsmretsallbopeu2013timeseriesspreadsheet/current/mret.csv'
STERLING_URL = 'https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?' + urlencode({
    'csv.x': 'yes', 'Datefrom': '01/Jan/1997', 'Dateto': 'now', 'SeriesCodes': 'XUQABK82',
    'CSVF': 'TN', 'UsingCodes': 'Y', 'VPD': 'Y', 'VFD': 'N'})
CUSTOMS_REFERENCE = 'https://www.uktradeinfo.com/api-documentation'
UKEA_CODES = {
    'current_account': 'HBOP', 'goods': 'BOKI', 'services': 'IKBD',
    'primary_income': 'HBOJ', 'secondary_income': 'IKBP', 'capital_account': 'FNVQ',
    'income_receipts': 'HBOH', 'income_payments': 'HBOI',
    'current_account_nsa': 'HBOG', 'bop_goods_nsa': 'LQCT', 'services_nsa': 'KTMS',
    'primary_income_nsa': 'HMBP', 'secondary_income_nsa': 'KTNF', 'capital_account_nsa': 'FKMJ',
    'financial_inflow': 'HBNT', 'asset_cash_flow': 'HBNR', 'liability_flow': 'HBNS',
    'statistical_discrepancy': 'HHDH', 'net_direct': 'MU7M', 'net_equity': 'HBWV',
    'net_debt': 'HBWX', 'net_other': 'HHYR', 'reserve_cash_flow': 'LTCV',
    'derivatives_inflow': 'ZPNN', 'reserve_stock': 'LTEB', 'gdp': 'YBHA',
    'debt_central_bank': 'VTZY', 'debt_banks': 'ZAVI', 'debt_other': 'ZAVB', 'debt_government': 'ZAVF',
    'direct_assets_cash': 'N2SV', 'direct_liabilities': 'N2SA',
    'equity_assets_cash': 'HBVI', 'equity_liabilities': 'XBLW',
    'debt_assets_cash': 'XBMW', 'debt_liabilities': 'XBLX',
    'other_assets_cash': 'XBMM', 'other_liabilities': 'XBMN',
}
TRADE_CODES = {'exports': 'IKBH', 'imports': 'IKBI', 'trade_balance': 'IKBJ'}
GROUP_NAMES = {'accounts': 'ONS UK Economic Accounts: BOP, GDP and external positions',
               'trade': 'ONS monthly UK goods-and-services trade',
               'currency': 'Bank of England broad sterling effective index',
               'customs': 'HMRC UK Trade Info overseas goods trade'}
GROUP_FIELDS = {'accounts': tuple(UKEA_CODES), 'trade': tuple(TRADE_CODES),
                'currency': ('currency_index',), 'customs': ('customs_exports', 'customs_imports', 'customs_goods')}


def customs_url(year):
    # Suppressed CN8 values are also present in published HS2 aggregates with
    # SuppressionIndex=0. Summing both duplicates those values. Retain the HS2
    # aggregates and estimates; do not filter CommodityId to eight-digit codes.
    query = f'filter(MonthId ge {year}01 and MonthId le {year}12 and SuppressionIndex eq 0)/groupby((MonthId,FlowTypeId),aggregate(Value with sum as TotalValue))'
    return 'https://api.uktradeinfo.com/OTS?' + urlencode({'$apply': query}, quote_via=quote, safe='$(),/')


def fetch_uk_text(url):
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; MeridianLocalDashboard/1.0)',
               'Accept': 'application/json,text/csv,*/*', 'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}
    with urlopen(Request(url, headers=headers), timeout=30) as response:
        body = response.read(20_000_001)
    if len(body) > 20_000_000:
        raise BOPError('UK source exceeded the download limit.')
    return body.decode('utf-8-sig')


def _number(text):
    value = str(text).strip()
    if value in ('', '..', '...', ':'):
        return None
    number = numeric(value)
    if number is None:
        raise BOPError('UK source returned an invalid numeric observation.')
    return number


def parse_ons(body, codes, frequency):
    rows = list(csv.reader(io.StringIO(body)))
    if len(rows) < 8 or rows[0][0] != 'Title' or [r[0] for r in rows[1:7]] != ['CDID', 'PreUnit', 'Unit', 'Release Date', 'Next release', 'Important Notes']:
        raise BOPError('ONS returned a different table layout.')
    positions, metadata = {}, []
    for field, code in codes.items():
        if rows[1].count(code) != 1:
            raise BOPError('An ONS series is missing or duplicated.')
        index = rows[1].index(code)
        if (rows[2][index], rows[3][index]) != ('£', 'm') and not (code == 'FKMJ' and rows[2][index] == rows[3][index] == ''):
            raise BOPError('An ONS series changed its GBP million units.')
        positions[field] = index
        metadata.append({'field': field, 'id': code, 'name': rows[0][index],
                         'native_unit': 'GBP millions', 'frequency': frequency,
                         'release_date': rows[4][index], 'next_release': rows[5][index],
                         'url': UKEA_URL if codes == UKEA_CODES else TRADE_URL})
    result, seen = [], set()
    for cells in rows[7:]:
        label = cells[0]
        if frequency == 'quarterly':
            match = re.fullmatch(r'(\d{4}) Q([1-4])', label)
            if not match:
                continue
            year, month = int(match[1]), (int(match[2]) - 1) * 3 + 1
        else:
            match = re.fullmatch(r'(\d{4}) ([A-Z]{3})', label)
            if not match:
                continue
            if match[2] not in ('JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'):
                raise BOPError('ONS returned an invalid month.')
            year, month = int(match[1]), ('JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC'.split().index(match[2]) + 1)
        if year < 1997:
            continue
        at = date(year, month, 1).isoformat()
        if at in seen or len(cells) != len(rows[0]):
            raise BOPError('ONS returned duplicate dates or truncated rows.')
        seen.add(at)
        values = {field: _number(cells[index]) for field, index in positions.items()}
        if all(value is None for value in values.values()):
            continue
        if at > date.today().isoformat():
            raise BOPError('ONS returned a future observation.')
        result.append({'date': at, **{field: value / 1000 if value is not None else None for field, value in values.items()}})
    result.sort(key=lambda row: row['date'])
    if not result or any(not any(row[field] is not None for row in result) for field in codes):
        raise BOPError('An ONS series has no usable observations.')
    if codes == UKEA_CODES:
        validate_uk_accounts(result)
    else:
        for row in result:
            if all(row[field] is not None for field in TRADE_CODES):
                if row['exports'] <= 0 or row['imports'] <= 0 or abs(row['trade_balance'] - row['exports'] + row['imports']) > .000001:
                    raise BOPError('ONS trade balances failed validation.')
    return result, metadata


def parse_sterling(body):
    reader = csv.DictReader(io.StringIO(body.strip()))
    if reader.fieldnames != ['DATE', 'XUQABK82']:
        raise BOPError('Bank of England returned a different series.')
    result, seen = [], set()
    months = {name: index + 1 for index, name in enumerate('Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split())}
    for cells in reader:
        parts = (cells.get('DATE') or '').split()
        if len(parts) != 3 or parts[1] not in months:
            raise BOPError('Bank of England returned an invalid date.')
        end = date(int(parts[2]), months[parts[1]], int(parts[0]))
        if end.month not in (3, 6, 9, 12) or end.day != (30 if end.month in (6, 9) else 31) or end > date.today():
            raise BOPError('Bank of England index is not a completed calendar quarter.')
        at = date(end.year, end.month - 2, 1).isoformat()
        value = _number(cells['XUQABK82'])
        if at in seen or value is None or value <= 0:
            raise BOPError('Bank of England returned invalid or duplicate values.')
        seen.add(at)
        result.append({'date': at, 'currency_index': value})
    if not result:
        raise BOPError('No sterling index observations are available.')
    return sorted(result, key=lambda row: row['date'])


def parse_customs(body, year):
    payload = json.loads(body)
    if payload.get('@odata.nextLink') or not isinstance(payload.get('value'), list) or '#OTS(' not in payload.get('@odata.context', ''):
        raise BOPError('HMRC returned a different or truncated aggregate.')
    months = {}
    for item in payload['value']:
        period, flow = item.get('MonthId'), item.get('FlowTypeId')
        if type(period) is not int or period // 100 != year or not 1 <= period % 100 <= 12 or flow not in (1, 2, 3, 4):
            raise BOPError('HMRC returned a different period or trade flow.')
        value = _number(item.get('TotalValue'))
        if value is None or value <= 0 or flow in months.setdefault(period, {}):
            raise BOPError('HMRC returned invalid or duplicate trade amounts.')
        months[period][flow] = value / 1_000_000_000
    result = []
    for period, flows in sorted(months.items()):
        if set(flows) != {1, 2, 3, 4}:
            raise BOPError('HMRC is missing an EU or non-EU flow.')
        at = date(year, period % 100, 1).isoformat()
        if at > date.today().isoformat():
            raise BOPError('HMRC returned a future observation.')
        exports, imports = flows[2] + flows[4], flows[1] + flows[3]
        result.append({'date': at, 'customs_exports': exports, 'customs_imports': imports, 'customs_goods': exports - imports})
    # An empty current year is legitimate before its first monthly release.
    if not result and year < date.today().year:
        raise BOPError('HMRC returned an empty historical year.')
    return result


def validate_uk_accounts(rows):
    identities = {
        'current_account_sa': ('current_account', ('goods', 'services', 'primary_income', 'secondary_income')),
        'current_account_nsa': ('current_account_nsa', ('bop_goods_nsa', 'services_nsa', 'primary_income_nsa', 'secondary_income_nsa')),
        'financial_inflow': ('financial_inflow', ('asset_cash_flow', 'liability_flow')),
        'direct_inward': ('net_direct', ('direct_assets_cash', 'direct_liabilities')),
        'equity_inward': ('net_equity', ('equity_assets_cash', 'equity_liabilities')),
        'debt_inward': ('net_debt', ('debt_assets_cash', 'debt_liabilities')),
        'other_inward': ('net_other', ('other_assets_cash', 'other_liabilities')),
        'financial_components': ('financial_inflow', ('net_direct', 'net_equity', 'net_debt', 'net_other', 'reserve_cash_flow', 'derivatives_inflow')),
    }
    checks = []
    for name, (total, terms) in identities.items():
        differences = [abs(row[total] - sum(row[key] for key in terms)) for row in rows
                       if row.get(total) is not None and all(row.get(key) is not None for key in terms)]
        maximum = max(differences, default=0)
        if not differences or maximum > .000001:
            raise BOPError(f'UK accounting validation failed: {name}.')
        checks.append({'name': name, 'observations_checked': len(differences), 'max_difference_gbp_billions': maximum})
    differences = [abs(row['current_account_nsa'] + row['capital_account_nsa'] + row['statistical_discrepancy'] + row['financial_inflow'])
                   for row in rows if all(row.get(key) is not None for key in ('current_account_nsa', 'capital_account_nsa', 'statistical_discrepancy', 'financial_inflow'))]
    if not differences or max(differences) > .000001:
        raise BOPError('UK unadjusted financial-account reconciliation failed.')
    checks.append({'name': 'financial_reconciliation_nsa', 'observations_checked': len(differences), 'max_difference_gbp_billions': max(differences)})
    return {'status': 'passed', 'tolerance_gbp_billions': .000001, 'checks': checks}


def _window(rows, index, periods):
    window = rows[max(0, index - periods + 1):index + 1]
    at = quarter_index(rows[index]['date'])
    return window if [quarter_index(row['date']) for row in window] == list(range(at - periods + 1, at + 1)) else []


def build_uk_view(snapshot, frequency='trailing4q'):
    if frequency not in ('quarterly', 'trailing4q'):
        raise ValueError('Choose quarterly or trailing4q.')
    groups = snapshot['groups']
    accounts = groups.get('accounts', {}).get('rows', [])
    if not accounts:
        raise BOPError('No verified UK external accounts are available. Refresh data to retry ONS.')
    validation = validate_uk_accounts(accounts)
    customs = {row['date']: row for row in quarter_sum(groups.get('customs', {}).get('rows', []), GROUP_FIELDS['customs'])}
    currencies = {row['date']: row for row in groups.get('currency', {}).get('rows', [])}
    base = []
    for original in accounts:
        if any(original.get(field) is None for field in ('current_account', 'goods', 'services', 'primary_income', 'secondary_income')):
            continue
        row = {**{field: None for field in FIELDS}, **original, 'period': quarter_label(original['date'])}
        row.update({field: customs.get(row['date'], {}).get(field) for field in GROUP_FIELDS['customs']})
        row['financial_account'] = -row['financial_inflow'] if row.get('financial_inflow') is not None else None
        row['net_financial_inflow'] = row.get('financial_inflow')
        row['net_lending'] = row['current_account'] + row['capital_account'] if row.get('capital_account') is not None else None
        row['goods_gap'] = row['customs_goods'] - row['bop_goods_nsa'] if row['customs_goods'] is not None and row.get('bop_goods_nsa') is not None else None
        base.append(row)
    periods = 4 if frequency == 'trailing4q' else 1
    selected = aggregate(base, frequency)
    windows = {original['date']: _window(base, index, periods) for index, original in enumerate(base)}
    for row in selected:
        row['bop_goods_nsa'] = strict_sum(windows[row['date']], 'bop_goods_nsa')
    if not selected:
        raise BOPError('No complete UK BOP periods are available.')
    macro = []
    # Include newer completed sterling quarters without projecting BOP, GDP or
    # positions into them. Those observations remain visible as explicit gaps.
    for at in sorted({row['date'] for row in base} | set(currencies)):
        window = windows.get(at, [])
        original = next((row for row in base if row['date'] == at), {})
        ca, gdp = strict_sum(window, 'current_account'), strict_sum(window, 'gdp')
        parts = [original.get(field) for field in ('debt_central_bank', 'debt_banks', 'debt_other', 'debt_government')]
        debt = sum(parts) if all(value is not None for value in parts) else None
        stock = original.get('reserve_stock')
        indices = [currencies.get(f'{q // 4}-{(q % 4) * 3 + 1:02d}-01', {}).get('currency_index') for q in range(quarter_index(at) - periods + 1, quarter_index(at) + 1)]
        reserve_cash = strict_sum(window, 'reserve_cash_flow')
        macro.append({'date': at, 'period': quarter_label(at),
                      'current_account_gdp_pct': 100 * ca / gdp if ca is not None and gdp is not None and gdp > 0 else None,
                      **{field: strict_sum(window, field) for field in ('net_direct', 'net_equity', 'net_debt', 'net_other')},
                      'reserve_transactions': -reserve_cash if reserve_cash is not None else None,
                      'reserve_stock': stock, 'short_term_external_debt': debt,
                      'reserves_debt_pct': 100 * stock / debt if stock is not None and debt is not None and debt > 0 else None,
                      'currency_index': sum(indices) / periods if all(value is not None for value in indices) else None})
    sources = [{'group': name, 'name': GROUP_NAMES[name], 'url': group['url'],
                'retrieved_at': group['checked_at'], 'checked_at': group['checked_at'],
                'latest_observation': group['rows'][-1]['date'], 'series': group.get('series', []),
                'downloads': group.get('downloads', [])}
               for name, group in groups.items() if group.get('rows')]
    trade = groups.get('trade', {}).get('rows', [])
    contributors = windows[selected[-1]['date']]
    return {'country': deepcopy(next(item for item in COUNTRIES if item['code'] == 'GBR')),
            'countries': deepcopy(COUNTRIES), 'status': 'ready', 'frequency': frequency,
            'units': 'GBP billions', 'reporting_currency': 'GBP', 'seasonal_adjustment': 'Seasonally adjusted',
            'retrieved_at': groups['accounts']['checked_at'], 'last_successful_check_at': groups['accounts']['checked_at'],
            'reference_url': REFERENCE_URL, 'series': selected, 'latest': selected[-1],
            'period_start': contributors[0]['period'], 'period_end': selected[-1]['period'],
            'contributing_quarters': deepcopy(contributors), 'latest_quarter': deepcopy(base[-1]),
            'insights': [], 'sources': sources, 'validation': validation,
            'methodology': [
                'ONS UKEA current account and components are seasonally adjusted, include precious metals, and use current-price GBP billions. These differ from the bulletin underlying totals excluding precious metals.',
                'The customs comparison and goods gap use HMRC versus ONS LQCT goods, both NSA. The SA goods headline uses BOKI and can differ. HMRC imports are CIF; BOP imports are FOB and follow economic ownership.',
                'HMRC API uses SuppressionIndex=0, retaining HS2 published aggregates and estimates while excluding suppressed CN8 duplicate values. EU and non-EU flows are combined. Historical revisions are re-fetched each refresh.',
                'CA/GDP uses the sum of SA current accounts divided by matching actual quarterly SA nominal GDP (YBHA); GDP is not an annual rate and is not divided by four.',
                'ONS asset cash flows are negative for outward acquisition. Published net investment balances are already inward: asset cash flow plus liabilities incurred. Reserve acquisition reverses the published LTCV cash-flow sign.',
                'Financial reconciliation uses NSA current and capital accounts. Financial-account net acquisition minus liabilities is negative HBNT; HHDH is retained as the reconciliation residual. SA and NSA accounts are not mixed in the identity.',
                'Reserves/debt uses quarter-end GBP ONS IIP reserve assets, including gold, divided by short-term debt of monetary authorities, banks, government and other sectors. Original maturity; intercompany lending lacks a maturity split and is excluded. Neither stock is summed.',
                'Sterling is BoE XUQABK82, broad effective exchange-rate index, Jan 2005=100, quarterly averages. Rolling 4Q takes four completed quarterly averages; higher means stronger sterling.',
                'Monthly ONS MRET total goods-and-services trade is SA including precious metals. Only complete quarters enter trade plots; 4Q sums, three-plotted-quarter averages and matching-period YoY use full history before range filtering.',
            ],
            'macro': {'status': 'ready', 'country_code': 'GBR', 'currency': 'GBP', 'frequency': frequency,
                      'quarterly': macro, 'monthly': trade, 'trade': build_trade_view(trade, frequency),
                      'sources': sources, 'warnings': snapshot.get('warnings', [])}}


class UKBOPService:
    def __init__(self, cache_path, enabled=True, transport=None):
        self.cache_path = Path(cache_path)
        self.enabled = enabled
        self.transport = transport or fetch_uk_text
        self.lock = RLock()
        self.snapshot = {'schema_version': 1, 'country': 'GBR', 'groups': {}, 'warnings': []}
        self.last_completed = self.last_failure = self.last_attempt_at = None
        for path in (self.cache_path, SNAPSHOT):
            try:
                candidate = json.loads(path.read_text(encoding='utf-8'))
                if candidate['schema_version'] != 1 or candidate['country'] != 'GBR':
                    continue
                build_uk_view(candidate)
                self.snapshot = candidate
                break
            except (OSError, ValueError, KeyError, TypeError, BOPError):
                continue

    def _download(self, name):
        downloads = []
        if name == 'customs':
            # Annual server-side aggregates avoid HMRC's full-history timeout.
            # 27 requests at initial implementation, below the 60/minute limit.
            urls = [(year, customs_url(year)) for year in range(2000, date.today().year + 1)]
            def download(item):
                year, link = item
                return parse_customs(self.transport(link), year)
            with ThreadPoolExecutor(max_workers=2) as executor:
                rows = [row for block in executor.map(download, urls) for row in block]
            url, metadata, downloads = CUSTOMS_REFERENCE, [], [link for _, link in urls]
            if not rows:
                raise BOPError('HMRC returned no UK trade observations.')
        elif name == 'currency':
            url = STERLING_URL
            rows, metadata = parse_sterling(self.transport(url)), [{'field': 'currency_index', 'id': 'XUQABK82',
                'name': 'Quarterly average broad sterling effective exchange-rate index',
                'native_unit': 'Jan 2005 = 100', 'frequency': 'quarterly', 'url': url}]
        else:
            url = UKEA_URL if name == 'accounts' else TRADE_URL
            rows, metadata = parse_ons(self.transport(url), UKEA_CODES if name == 'accounts' else TRADE_CODES,
                                       'quarterly' if name == 'accounts' else 'monthly')
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
                                        raise BOPError('UK source coverage regressed.')
                            candidate['groups'][name] = group
                        except (OSError, HTTPException, ValueError, TypeError, KeyError, BOPError):
                            warnings.append(f'{GROUP_NAMES[name]} refresh failed; saved observations retain their original check time.')
                candidate['warnings'] = warnings
                build_uk_view(candidate, frequency)
                self.snapshot = candidate
                try:
                    self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = self.cache_path.with_suffix('.tmp')
                    temporary.write_text(json.dumps(candidate, allow_nan=False), encoding='utf-8')
                    temporary.replace(self.cache_path)
                except OSError:
                    self.snapshot['warnings'].append('Fresh UK data is in memory, but the local cache could not be saved.')
                self.last_failure = time.monotonic() if warnings else None
                self.last_completed = time.monotonic()
            result = build_uk_view(self.snapshot, frequency)
            result.update({'refresh_available': self.enabled,
                           'delivery': 'source_checked' if refresh and not self.snapshot.get('warnings') and (performed or shared) else 'saved_snapshot',
                           'last_attempt_at': self.last_attempt_at, 'refresh_error': None, 'source_warning': None,
                           'refresh_cooldown_seconds': max(0, math.ceil(30 - (time.monotonic() - self.last_failure))) if self.last_failure else 0})
            return result
