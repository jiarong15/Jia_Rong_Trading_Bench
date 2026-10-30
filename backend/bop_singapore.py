"""Singapore external accounts from SingStat (DOS, Enterprise Singapore, MAS).

Flows/stocks remain SGD billions. The quarterly portfolio series is combined;
the annual equity/debt split is never interpolated into quarterly observations.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import csv
from datetime import date, datetime, timezone
from http.client import HTTPException
import json
import io
import math
from pathlib import Path
import re
from threading import RLock
import time
from urllib.request import Request, urlopen
from .bop import BOPError, COUNTRIES, FIELDS, aggregate, numeric, quarter_index, quarter_label
from .bop_macro import build_trade_view, monthly_index
from .bop_japan import quarter_sum, strict_sum

SNAPSHOT = Path(__file__).parent / 'snapshots' / 'singapore_bop.json'
API_ROOT = 'https://tablebuilder.singstat.gov.sg/api/table/'
REFERENCE_URL = 'https://www.singstat.gov.sg/find-data/explore-data-themes/economy-prices/international-accounts'
CURRENCY_URL = 'https://stats.bis.org/api/v1/data/WS_EER/M.N.B.SG?format=csv'
BOP_CODES = {
    'overall_balance': '1', 'current_account': '1.1', 'goods': '1.1.1',
    'goods_exports': '1.1.1.1', 'goods_imports': '1.1.1.2', 'services': '1.1.2',
    'services_exports': '1.1.2.1', 'services_imports': '1.1.2.2',
    'primary_income': '1.1.3', 'income_receipts': '1.1.3.1', 'income_payments': '1.1.3.2',
    'secondary_income': '1.1.4', 'capital_financial': '1.2', 'financial_ex_reserves': '1.2.2',
    'direct_net': '1.2.2.1', 'direct_assets': '1.2.2.1.1', 'direct_liabilities': '1.2.2.1.2',
    'portfolio_net': '1.2.2.2', 'portfolio_assets': '1.2.2.2.1', 'portfolio_liabilities': '1.2.2.2.2',
    'derivatives_net': '1.2.2.3', 'other_net': '1.2.2.4',
    'other_assets': '1.2.2.4.1', 'other_liabilities': '1.2.2.4.2',
    'statistical_discrepancy': '1.3', 'reserve_transactions': '2',
}
DEBT_CODES = {'government_total': '1.2.1', 'debt_central_bank': '1.2.2.1',
              'debt_banks': '1.2.3.1', 'debt_other': '1.2.4.1'}
TABLES = {
    'bop': ('M060171', "Singapore's Balance Of Payments, Quarterly", 'Quarterly', 'Non-seasonally Adjusted', BOP_CODES, 'Million Dollars', 1000),
    'gdp': ('M015651', 'Gross Domestic Product At Current Prices, By Industry (SSIC 2020), Quarterly', 'Quarterly', 'Non-seasonally Adjusted', {'gdp': '1'}, 'Million Dollars', 1000),
    'debt': ('M060221', "Singapore's External Debt Statistics, (End Of Period), Quarterly", 'Quarterly', '', DEBT_CODES, 'Million Dollars', 1000),
    'reserves': ('M700031', 'Official Foreign Reserves (End Of Period), Monthly', 'Monthly', '', {'reserve_stock': '1'}, 'Million Dollars', 1000),
    'customs': ('M451001', 'Merchandise Trade By Commodity Section, (At Current Prices), Monthly', 'Monthly', 'Non-seasonally Adjusted', {'exports': '3', 'imports': '2'}, 'Thousand Dollars', 1_000_000),
}
GROUP_NAMES = {'bop': 'SingStat / DOS balance of payments', 'gdp': 'SingStat / DOS nominal GDP',
               'debt': 'SingStat / DOS external debt', 'reserves': 'MAS official foreign reserves via SingStat',
               'customs': 'Enterprise Singapore merchandise trade via SingStat',
               'currency': 'BIS broad nominal Singapore dollar index, direct feed'}
GROUP_FIELDS = {**{name: tuple(config[4]) for name, config in TABLES.items()}, 'currency': ('currency_index',)}


def fetch_singapore_text(url):
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; MeridianLocalDashboard/1.0)',
               'Accept': 'application/json,text/csv,*/*', 'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}
    with urlopen(Request(url, headers=headers), timeout=25) as response:
        body = response.read(5_000_001)
    if len(body) > 5_000_000:
        raise BOPError('Singapore source exceeded the download limit.')
    return body.decode('utf-8-sig')


def period_date(label, frequency):
    match = re.fullmatch(r'(\d{4}) ([1-4])Q' if frequency == 'Quarterly' else r'(\d{4}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)', label)
    if not match:
        raise BOPError('SingStat returned a different period format.')
    month = (int(match[2]) - 1) * 3 + 1 if frequency == 'Quarterly' else 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split().index(match[2]) + 1
    return date(int(match[1]), month, 1).isoformat()


def _number(value):
    if value in ('na', 'NA', '', None):
        return None
    if value == '-':  # SingStat explicitly defines this as nil/negligible.
        return 0.0
    try:
        result = numeric(value)
    except (ValueError, TypeError) as exc:
        raise BOPError('SingStat returned an invalid numeric observation.') from exc
    if result is None:
        raise BOPError('SingStat returned an invalid numeric observation.')
    return result


def parse_singstat(metadata, pages, group):
    table, title, frequency, adjustment, codes, unit, divisor = TABLES[group]
    if metadata.get('StatusCode') != 200:
        raise BOPError('SingStat metadata request failed.')
    meta = metadata['Data']['records']
    if (meta.get('id'), meta.get('title'), meta.get('frequency'), meta.get('adjustmentType')) != (table, title, frequency, adjustment):
        raise BOPError('SingStat changed the table, frequency or adjustment basis.')
    expected = {row['seriesNo']: row for row in meta['row']}
    if len(expected) != len(meta['row']) or not set(codes.values()).issubset(expected):
        raise BOPError('A requested Singapore series is missing or duplicated.')
    merged, count = {}, 0
    for offset, payload in pages:
        if payload.get('StatusCode') != 200:
            raise BOPError('SingStat data request failed.')
        page = payload['Data']
        if (page.get('id'), page.get('title'), page.get('frequency'), page.get('adjustmentType'), page.get('dataLastUpdated')) != (table, title, frequency, adjustment, meta.get('dataLastUpdated')):
            raise BOPError('SingStat pages changed release vintage or table metadata.')
        if group != 'customs' and int(page.get('offset') or 0) != offset:
            raise BOPError('SingStat returned a different pagination offset.')
        for row in page['row']:
            code = row['seriesNo']
            if code not in expected or (row['rowText'], row['uoM']) != (expected[code]['rowText'], expected[code]['uoM']):
                raise BOPError('SingStat changed series labels or units during pagination.')
            target = merged.setdefault(code, {})
            for item in row['columns']:
                label = item['key']
                period_date(label, frequency)
                if label in target:
                    raise BOPError('SingStat returned duplicate observations or repeated pages.')
                target[label] = item['value']
                count += 1
    # The API caps each response at 5,000 cells even if a larger limit is
    # requested. Match the metadata total, not DataCount (which is always 1).
    if group != 'customs' and (count != meta.get('total') or set(merged) != set(expected)):
        raise BOPError('SingStat returned an incomplete paginated table.')
    result, source_series = {}, []
    for field, code in codes.items():
        if expected[code]['uoM'] != unit or not merged.get(code) or meta['endPeriod'] not in merged[code]:
            raise BOPError('A Singapore series changed units or lost its latest observation.')
        if group == 'customs':
            # Selected monthly totals span the table's full calendar history.
            start = date.fromisoformat(period_date(meta['startPeriod'], frequency))
            end = date.fromisoformat(period_date(meta['endPeriod'], frequency))
            if len(merged[code]) != (end.year - start.year) * 12 + end.month - start.month + 1:
                raise BOPError('SingStat returned a truncated monthly trade series.')
        usable = False
        for label, text in merged[code].items():
            at = period_date(label, frequency)
            if at < '1997-01-01':
                continue
            value = _number(text)
            if at > date.today().isoformat():
                raise BOPError('SingStat returned a future observation.')
            result.setdefault(at, {'date': at})[field] = value / divisor if value is not None else None
            usable = usable or value is not None
        if not usable:
            raise BOPError('A Singapore series has no usable observations.')
        source_series.append({'field': field, 'id': table + ':' + code, 'name': expected[code]['rowText'],
                              'native_unit': unit, 'frequency': frequency, 'adjustment': adjustment,
                              'release_date': meta['dataLastUpdated'], 'footnote': expected[code].get('footnote', ''),
                              'url': 'https://tablebuilder.singstat.gov.sg/table/TS/' + table})
    rows = [{**{field: None for field in codes}, **result[at]} for at in sorted(result)]
    if group == 'bop':
        validate_singapore_accounts(rows)
    if group in ('gdp', 'reserves', 'customs') and any(row[field] is not None and row[field] <= 0 for row in rows for field in codes):
        raise BOPError('Singapore GDP, reserves or trade contains a non-positive level.')
    return rows, source_series


def validate_singapore_accounts(rows):
    checks = []
    identities = {
        'current_account': ('current_account', (('goods', 1), ('services', 1), ('primary_income', 1), ('secondary_income', 1))),
        'goods': ('goods', (('goods_exports', 1), ('goods_imports', -1))),
        'services': ('services', (('services_exports', 1), ('services_imports', -1))),
        'primary_income': ('primary_income', (('income_receipts', 1), ('income_payments', -1))),
        'direct': ('direct_net', (('direct_assets', 1), ('direct_liabilities', -1))),
        'portfolio': ('portfolio_net', (('portfolio_assets', 1), ('portfolio_liabilities', -1))),
        'other': ('other_net', (('other_assets', 1), ('other_liabilities', -1))),
        'financial_ex_reserves': ('financial_ex_reserves', (('direct_net', 1), ('portfolio_net', 1), ('derivatives_net', 1), ('other_net', 1))),
        'overall_balance': ('overall_balance', (('current_account', 1), ('capital_financial', -1), ('statistical_discrepancy', 1))),
        'reserve_acquisition': ('overall_balance', (('reserve_transactions', 1),)),
    }
    for name, (total, terms) in identities.items():
        differences = [abs(row[total] - sum(row[key] * sign for key, sign in terms)) for row in rows
                       if row.get(total) is not None and all(row.get(key) is not None for key, _ in terms)]
        maximum = max(differences, default=0)
        # Native amounts are rounded to SGD 0.1 million; allow the accumulated
        # rounding of four independently rounded components (SGD 0.3 million).
        if not differences or maximum > .0003001:
            raise BOPError(f'Singapore BOP accounting validation failed: {name}.')
        checks.append({'name': name, 'observations_checked': len(differences), 'max_difference_sgd_billions': maximum})
    return {'status': 'passed', 'tolerance_sgd_billions': .0003001, 'checks': checks}


def parse_bis_sgd(body):
    reader = csv.DictReader(io.StringIO(body))
    required = {'FREQ', 'EER_TYPE', 'EER_BASKET', 'REF_AREA', 'UNIT_MEASURE', 'COLLECTION', 'TIME_PERIOD', 'OBS_VALUE', 'OBS_STATUS'}
    if not required.issubset(reader.fieldnames or []):
        raise BOPError('BIS returned a different currency-index schema.')
    rows, seen = [], set()
    for item in reader:
        if tuple(item[key] for key in ('FREQ', 'EER_TYPE', 'EER_BASKET', 'REF_AREA', 'UNIT_MEASURE', 'COLLECTION')) != ('M', 'N', 'B', 'SG', '882', 'A'):
            raise BOPError('BIS changed country, basket, units or frequency.')
        if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', item['TIME_PERIOD']):
            raise BOPError('BIS returned an invalid month.')
        at = item['TIME_PERIOD'] + '-01'
        if at in seen or at >= date.today().replace(day=1).isoformat():
            raise BOPError('BIS returned duplicate or incomplete monthly observations.')
        seen.add(at)
        value = _number(item['OBS_VALUE'])
        if value is not None and (value <= 0 or item['OBS_STATUS'] not in ('A', 'P', 'E')):
            raise BOPError('BIS returned an invalid or forecast index observation.')
        if at >= '1997-01-01':
            rows.append({'date': at, 'currency_index': value})
    if not rows or not any(row['currency_index'] is not None for row in rows):
        raise BOPError('BIS returned no Singapore index observations.')
    return sorted(rows, key=lambda row: row['date'])


def build_singapore_view(snapshot, frequency='trailing4q'):
    if frequency not in ('quarterly', 'trailing4q'):
        raise ValueError('Choose quarterly or trailing4q.')
    groups = snapshot['groups']
    accounts = groups.get('bop', {}).get('rows', [])
    if not accounts:
        raise BOPError('No verified Singapore external accounts are available. Refresh data to retry SingStat.')
    validation = validate_singapore_accounts(accounts)
    months = groups.get('customs', {}).get('rows', [])
    customs = {row['date']: row for row in quarter_sum(months, ('exports', 'imports'))}
    base = []
    for original in accounts:
        if any(original.get(field) is None for field in ('current_account', 'goods', 'services', 'primary_income', 'secondary_income')):
            continue
        row = {**{field: None for field in FIELDS}, **original, 'period': quarter_label(original['date'])}
        trade = customs.get(row['date'], {})
        row['customs_exports'], row['customs_imports'] = trade.get('exports'), trade.get('imports')
        row['customs_goods'] = trade['exports'] - trade['imports'] if trade.get('exports') is not None and trade.get('imports') is not None else None
        row['goods_gap'] = row['customs_goods'] - row['goods'] if row['customs_goods'] is not None else None
        # Singapore consolidates capital under the financial account. Do not
        # invent a separate zero capital account or an unmixed full BPM6 FA.
        base.append(row)
    selected = aggregate(base, frequency)
    if not selected:
        raise BOPError('No complete Singapore BOP periods are available.')
    periods = 4 if frequency == 'trailing4q' else 1
    lookup = {name: {row['date']: row for row in group['rows']} for name, group in groups.items()}
    reserves = {monthly_index(row['date']): row.get('reserve_stock') for row in groups.get('reserves', {}).get('rows', [])}
    currency = {monthly_index(row['date']): row.get('currency_index') for row in groups.get('currency', {}).get('rows', [])}
    macro = []
    for index, original in enumerate(base):
        at = original['date']; q = quarter_index(at)
        window = base[max(0, index - periods + 1):index + 1]
        if [quarter_index(row['date']) for row in window] != list(range(q - periods + 1, q + 1)):
            window = []
        ca = strict_sum(window, 'current_account')
        gdps = [lookup.get('gdp', {}).get(row['date'], {}).get('gdp') for row in window]
        gdp = sum(gdps) if gdps and all(value is not None for value in gdps) else None
        debt_row = lookup.get('debt', {}).get(at, {})
        debt_parts = [debt_row.get(field) for field in ('debt_central_bank', 'debt_banks', 'debt_other')]
        # Government has no maturity split; a published zero total implies
        # zero short-term debt. A future nonzero total must not be assumed zero.
        debt = sum(debt_parts) if debt_row.get('government_total') == 0 and all(value is not None for value in debt_parts) else None
        stock = reserves.get(monthly_index(at) + 2)
        indices = [currency.get(month) for month in range(monthly_index(at) - (periods - 1) * 3, monthly_index(at) + 3)]
        flows = {}
        for field, source in [('net_direct', 'direct_net'), ('net_portfolio', 'portfolio_net'), ('net_other', 'other_net')]:
            value = strict_sum(window, source)
            flows[field] = -value if value is not None else None
        macro.append({'date': at, 'period': quarter_label(at), **flows, 'net_equity': None, 'net_debt': None,
                      'current_account_gdp_pct': 100 * ca / gdp if ca is not None and gdp is not None and gdp > 0 else None,
                      'reserve_transactions': strict_sum(window, 'reserve_transactions'),
                      'reserve_stock': stock, 'short_term_external_debt': debt,
                      'reserves_debt_pct': 100 * stock / debt if stock is not None and debt is not None and debt > 0 else None,
                      'currency_index': sum(indices) / len(indices) if all(value is not None for value in indices) else None})
    sources = [{'group': name, 'name': GROUP_NAMES[name], 'url': group['url'], 'checked_at': group['checked_at'],
                'retrieved_at': group['checked_at'], 'latest_observation': group['rows'][-1]['date'],
                'series': group.get('series', []), 'downloads': group.get('downloads', []), 'footnote': group.get('footnote', '')}
               for name, group in groups.items() if group.get('rows')]
    monthly = [{**row, 'period': row['date'][:7], 'trade_balance': row['exports'] - row['imports'] if row.get('exports') is not None and row.get('imports') is not None else None} for row in months]
    contributors = base[-periods:]
    return {'country': deepcopy(next(item for item in COUNTRIES if item['code'] == 'SGP')), 'countries': deepcopy(COUNTRIES),
            'status': 'ready', 'frequency': frequency, 'units': 'SGD billions', 'reporting_currency': 'SGD',
            'seasonal_adjustment': 'Not seasonally adjusted', 'retrieved_at': groups['bop']['checked_at'],
            'last_successful_check_at': groups['bop']['checked_at'], 'reference_url': REFERENCE_URL,
            'series': selected, 'latest': selected[-1], 'period_start': contributors[0]['period'], 'period_end': selected[-1]['period'],
            'contributing_quarters': deepcopy(contributors), 'latest_quarter': deepcopy(base[-1]),
            'insights': [], 'validation': validation, 'sources': sources,
            'methodology': [
                'Singapore DOS BOP and nominal GDP are unadjusted current-price SGD. Source million dollars are divided by 1000. CA/GDP matches actual quarter or four-quarter amounts, without dividing GDP by four.',
                'SingStat caps each table response at 5000 cells. All pages must match the metadata cell count and release date; repeated, missing, duplicate or mixed-vintage pages are rejected.',
                'Net inward direct, combined portfolio and other investment reverse DOS asset-minus-liability balances. The quarterly source combines portfolio equity and debt; the annual split is not interpolated. Derivatives and reserves are excluded from investment bars.',
                'Singapore consolidates the capital account under the financial account. Separate capital_account, net_lending and unmixed full financial_account fields remain unavailable. Validation uses the published A-B+C overall balance and reserve acquisition identity.',
                'Reserve transactions are net reserve acquisition from BOP, distinct from MAS quarter-end reserve stocks. Coverage uses central bank, deposit-taking corporations and other sectors short-term debt. Government has no maturity split; coverage is available only where its published total is zero. Intercompany loans lack a maturity split and are excluded. Stocks are never summed.',
                'Reserves are MAS official foreign reserves in SGD via SingStat, at quarter end, including gold. Prior to May 1999 they were valued at book cost; since May 1999 reserve assets are translated at end-month market exchange rates.',
                'Currency index is the direct BIS WS_EER M.N.B.SG broad nominal effective SGD series, 2020=100. It is a BIS trade-weighted index, separate from the MAS monetary-policy S$NEER basket. Use averages of three/twelve complete monthly observations.',
                'Enterprise Singapore monthly merchandise trade via SingStat is NSA, goods only including re-exports, exports FOB/imports CIF. Source thousand SGD are divided by one million. The customs/BOP gap also reflects ownership, merchanting, processing, freight, coverage and revisions.',
                'Monthly merchandise trade is aggregated only into complete quarters, then quarterly or strict 4Q totals. MA3 averages three plotted quarters; YoY compares the selected totals to the same quarter a year earlier. No services estimates or partial-quarter projections are added.',
            ],
            'macro': {'status': 'ready', 'country_code': 'SGP', 'currency': 'SGD', 'frequency': frequency,
                      'quarterly': macro, 'monthly': monthly, 'trade': build_trade_view(monthly, frequency),
                      'portfolio_split': 'combined', 'trade_scope': 'merchandise', 'sources': sources, 'warnings': snapshot.get('warnings', [])}}


class SingaporeBOPService:
    def __init__(self, cache_path, enabled=True, transport=None):
        self.cache_path = Path(cache_path)
        self.enabled = enabled
        self.transport = transport or fetch_singapore_text
        self.lock = RLock()
        self.snapshot = {'schema_version': 1, 'country': 'SGP', 'groups': {}, 'warnings': []}
        self.last_completed = self.last_failure = self.last_attempt_at = None
        for path in (self.cache_path, SNAPSHOT):
            try:
                candidate = json.loads(path.read_text(encoding='utf-8'))
                if candidate['schema_version'] != 1 or candidate['country'] != 'SGP':
                    continue
                build_singapore_view(candidate)
                self.snapshot = candidate
                break
            except (OSError, ValueError, KeyError, TypeError, BOPError):
                continue

    def _download(self, name):
        if name == 'currency':
            url = CURRENCY_URL
            rows = parse_bis_sgd(self.transport(url))
            metadata = [{'field': 'currency_index', 'id': 'M.N.B.SG', 'name': 'BIS broad nominal effective SGD index',
                         'native_unit': '2020 = 100', 'frequency': 'Monthly', 'url': 'https://data.bis.org/topics/EER/BIS,WS_EER,1.0/M.N.B.SG'}]
            downloads, footnote = [url], 'BIS basket; distinct from the MAS policy S$NEER.'
        else:
            table = TABLES[name][0]
            meta_url = API_ROOT + 'metadata/' + table
            meta = json.loads(self.transport(meta_url))
            count = meta.get('Data', {}).get('records', {}).get('total')
            if type(count) is not int or not 0 < count <= 100_000:
                raise BOPError('SingStat metadata has no bounded cell count.')
            url = 'https://tablebuilder.singstat.gov.sg/table/TS/' + table
            downloads, pages = [meta_url], []
            if name == 'customs':
                for code in ('2', '3'):
                    link = API_ROOT + 'tabledata/' + table + '?seriesNoORrowNo=' + code
                    downloads.append(link); pages.append((0, json.loads(self.transport(link))))
            else:
                for offset in range(0, count, 5000):
                    link = API_ROOT + 'tabledata/' + table + (f'?offset={offset}' if offset else '')
                    downloads.append(link); pages.append((offset, json.loads(self.transport(link))))
            rows, metadata = parse_singstat(meta, pages, name)
            footnote = meta['Data']['records'].get('footnote', '')
        return {'url': url, 'rows': rows, 'series': metadata, 'downloads': downloads, 'footnote': footnote,
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
                                        raise BOPError('Singapore source coverage regressed.')
                            candidate['groups'][name] = group
                        except (OSError, HTTPException, ValueError, TypeError, KeyError, BOPError):
                            warnings.append(f'{GROUP_NAMES[name]} refresh failed; saved observations retain their original check time.')
                candidate['warnings'] = warnings
                build_singapore_view(candidate, frequency)
                self.snapshot = candidate
                try:
                    self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = self.cache_path.with_suffix('.tmp')
                    temporary.write_text(json.dumps(candidate, allow_nan=False), encoding='utf-8')
                    temporary.replace(self.cache_path)
                except OSError:
                    self.snapshot['warnings'].append('Fresh Singapore data is in memory, but the local cache could not be saved.')
                self.last_failure = time.monotonic() if warnings else None
                self.last_completed = time.monotonic()
            result = build_singapore_view(self.snapshot, frequency)
            result.update({'refresh_available': self.enabled,
                           'delivery': 'source_checked' if refresh and not self.snapshot.get('warnings') and (performed or shared) else 'saved_snapshot',
                           'last_attempt_at': self.last_attempt_at, 'refresh_error': None, 'source_warning': None,
                           'refresh_cooldown_seconds': max(0, math.ceil(30 - (time.monotonic() - self.last_failure))) if self.last_failure else 0})
            return result
