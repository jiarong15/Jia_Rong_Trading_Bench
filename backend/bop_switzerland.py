"""Swiss external accounts from SNB, SECO and BIS public live releases."""
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
import re
import ssl
import subprocess
from threading import RLock
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request
from .bop_http import urlopen
from .bop import BOPError, COUNTRIES, FIELDS, aggregate, numeric, quarter_index, quarter_label
from .bop_japan import strict_sum
from .bop_macro import monthly_index, build_trade_view

SNAPSHOT = Path(__file__).parent / 'snapshots' / 'switzerland_bop.json'
SPEC = json.loads((SNAPSHOT.parent / 'switzerland_series.json').read_text(encoding='utf-8'))
SNB_ROOT = 'https://data.snb.ch/api/cube/'
GDP_DATA = 'https://scheduler.swissdatas.ch/scheduled/ch-seco-gdp.csv'
GDP_META = 'https://scheduler.swissdatas.ch/scheduled/ch-seco-gdp.json'
CURRENCY = 'https://stats.bis.org/api/v1/data/WS_EER/M.N.B.CH?format=csv'
GROUP_NAMES = {'accounts': 'SNB quarterly balance of payments and FOCBS customs comparison',
               'investment': 'SNB quarterly financial account investment categories',
               'positions': 'SNB quarter-end IIP reserve assets including gold',
               'debt': 'SNB SDDS Plus sector short-term external debt',
               'gdp': 'SECO unadjusted current-price quarterly GDP',
               'trade': 'FOCBS monthly business-cycle merchandise trade via SNB',
               'currency': 'BIS broad nominal Swiss franc index, direct feed'}
GROUP_FIELDS = {**{group: tuple(field for item in definitions for field in item['fields']) for group, definitions in SPEC.items()},
                'gdp': ('gdp',), 'currency': ('currency_index',)}


def fetch_switzerland(url):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.netloc not in ('data.snb.ch', 'scheduler.swissdatas.ch', 'stats.bis.org'):
        raise BOPError('Swiss source URL left the official-source allowlist.')
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; JRTradingWorkbench/1.0)', 'Cache-Control': 'no-cache', 'Pragma': 'no-cache',
               'Accept': 'text/csv' if url == CURRENCY else 'application/json,text/csv,*/*'}
    try:
        with urlopen(Request(url, headers=headers), timeout=30) as response: body = response.read(8_000_001)
    except URLError as exc:
        # Windows' native trusted certificate store can complete the SNB chain
        # when Python's store cannot. Certificate verification stays enabled.
        if os.name != 'nt' or parts.netloc != 'data.snb.ch' or not isinstance(exc.reason, ssl.SSLCertVerificationError): raise
        result = subprocess.run(['curl.exe', '--fail', '--silent', '--show-error', '--location', '--proto', '=https',
                                 '--proto-redir', '=https', '--max-redirs', '3', '--max-time', '30', '--max-filesize', '8000000',
                                 '--header', 'Cache-Control: no-cache', '--header', 'Pragma: no-cache', '--user-agent', headers['User-Agent'], url],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode: raise BOPError('The official SNB HTTPS download failed.')
        body = result.stdout
    if len(body) > 8_000_000: raise BOPError('Swiss source exceeded the download limit.')
    return body


def number_or_gap(value):
    if value is None or value == '': return None
    if isinstance(value, bool): raise BOPError('Swiss source returned a boolean observation.')
    try: result = numeric(value)
    except (ValueError, TypeError) as exc: raise BOPError('Swiss source returned an invalid number.') from exc
    if result is None: raise BOPError('Swiss source returned a non-finite number.')
    return result


def native_date(value, frequency):
    if frequency == 'P3M' and re.fullmatch(r'\d{4}-Q[1-4]', value):
        return f'{value[:4]}-{(int(value[-1]) - 1) * 3 + 1:02d}-01'
    if frequency == 'P1M' and re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', value): return value + '-01'
    raise BOPError('Swiss source dates do not match the native frequency.')


def parse_snb(body, definition):
    content = json.loads(body); series = content.get('timeseries')
    if not isinstance(series, list) or len(series) > 1000: raise BOPError('SNB changed its time-series schema.')
    wanted = {spec['id']: (field, spec) for field, spec in definition['fields'].items()}
    found, observations, metadata = set(), {}, []
    for item in series:
        key = item.get('metadata', {}).get('key')
        if key not in wanted: continue
        if key in found: raise BOPError('SNB returned a duplicate requested series.')
        found.add(key); field, spec = wanted[key]
        if item.get('metadata') != spec['metadata'] or item.get('header') != spec['header']:
            raise BOPError('SNB changed a series unit, scale, frequency or definition.')
        frequency = spec['metadata']['frequency']; dates, usable = set(), False
        values = item.get('values')
        if not isinstance(values, list) or not 20 <= len(values) <= 5000: raise BOPError('SNB omitted or truncated a requested series.')
        for value in values:
            at = native_date(value['date'], frequency)
            if at in dates or at > date.today().isoformat(): raise BOPError('SNB returned duplicate or future observations.')
            dates.add(at); number = number_or_gap(value['value'])
            if at < '1997-01-01': continue
            observations.setdefault(at, {'date': at})[field] = number / spec['divisor'] if number is not None else None
            usable |= number is not None
        dates = sorted(dates)
        index = quarter_index if frequency == 'P3M' else monthly_index
        if not usable or dates[0] != native_date(spec['first_date'], frequency) or [index(at) for at in dates] != list(range(index(dates[0]), index(dates[-1]) + 1)):
            raise BOPError('SNB returned incomplete native reporting periods.')
        metadata.append({'field': field, 'id': key, 'name': ' / '.join(part['dimItem'] for part in item['header']),
                         'native_unit': spec['metadata'].get('unit', 'Value, in CHF millions'), 'frequency': 'Quarterly' if frequency == 'P3M' else 'Monthly',
                         'adjustment': 'SA and calendar adjusted' if definition['cube'] == 'ausshawarm' else 'NSA',
                         'url': SNB_ROOT + definition['cube'] + '/data/json/en', 'latest_source_period': dates[-1]})
    if found != set(wanted): raise BOPError('SNB omitted a requested Swiss series.')
    return [{**{field: None for field in definition['fields']}, **observations[at]} for at in sorted(observations)], metadata


def parse_seco_gdp(body, meta_body):
    meta = json.loads(meta_body)
    if (meta.get('source_name', {}).get('en') != 'State Secretariat for Economic Affairs (SECO)' or
        meta.get('units', {}).get('type', {}).get('nom', {}).get('en') != 'Swiss Francs, at current prices' or
        meta.get('labels', {}).get('structure', {}).get('gdp', {}).get('en') != 'Gross domestic product at market prices, B1GQ' or
        meta.get('labels', {}).get('seas_adj', {}).get('na', {}).get('en') != 'Not seasonally adjusted'):
        raise BOPError('SECO changed the GDP source, currency, price basis, aggregate or adjustment.')
    reader = csv.DictReader(io.StringIO(body.decode('utf-8-sig')))
    if reader.fieldnames != ['structure', 'type', 'seas_adj', 'date', 'value']: raise BOPError('SECO changed its GDP CSV layout.')
    rows, seen = [], set()
    for item in reader:
        if (item['structure'], item['type'], item['seas_adj']) != ('gdp', 'nom', 'na'): continue
        at = date.fromisoformat(item['date'])
        if at.day != 1 or at.month not in (1, 4, 7, 10) or at > date.today() or at.isoformat() in seen: raise BOPError('SECO returned invalid quarterly dates.')
        seen.add(at.isoformat()); value = number_or_gap(item['value'])
        # The SECO native CSV is in million CHF, verified against nom_q in its
        # officially linked unadjusted workbook. Reject a changed scale rather
        # than silently plotting CHF units or an annualized rate as millions.
        if value is not None and not 1000 < value < 1_000_000: raise BOPError('SECO GDP levels left the verified million-CHF scale.')
        if at.year >= 1997: rows.append({'date': at.isoformat(), 'gdp': value / 1000 if value is not None else None})
    rows.sort(key=lambda row: row['date'])
    if not rows or rows[0]['date'] != '1997-01-01' or [quarter_index(row['date']) for row in rows] != list(range(quarter_index(rows[0]['date']), quarter_index(rows[-1]['date']) + 1)):
        raise BOPError('SECO omitted or truncated quarterly GDP history.')
    if rows[-1]['gdp'] is None: raise BOPError('SECO has no latest GDP total.')
    return rows, [{'field': 'gdp', 'id': 'SECO.gdp.nom.na', 'name': 'GDP at current market prices, actual quarter, unadjusted',
                   'native_unit': 'Million CHF', 'frequency': 'Quarterly', 'adjustment': 'NSA', 'url': GDP_DATA, 'metadata_url': GDP_META,
                   'native_scale_reference': 'https://www.seco.admin.ch/en/gross-domestic-product', 'metadata_updated_utc': meta.get('updated_utc')}]


def parse_bis_chf(body):
    reader = csv.DictReader(io.StringIO(body.decode('utf-8-sig')))
    required = {'FREQ', 'EER_TYPE', 'EER_BASKET', 'REF_AREA', 'UNIT_MEASURE', 'COLLECTION', 'TIME_PERIOD', 'OBS_VALUE', 'OBS_STATUS'}
    if not required.issubset(reader.fieldnames or []): raise BOPError('BIS changed the CHF index schema.')
    rows, seen = [], set()
    for item in reader:
        if tuple(item[key] for key in ('FREQ', 'EER_TYPE', 'EER_BASKET', 'REF_AREA', 'UNIT_MEASURE', 'COLLECTION')) != ('M', 'N', 'B', 'CH', '882', 'A'):
            raise BOPError('BIS changed the CHF country, frequency, basket or units.')
        at = native_date(item['TIME_PERIOD'], 'P1M')
        if at in seen or at >= date.today().replace(day=1).isoformat(): raise BOPError('BIS returned duplicate or incomplete months.')
        seen.add(at); value = number_or_gap(item['OBS_VALUE'])
        if value is not None and (value <= 0 or item['OBS_STATUS'] not in ('A', 'P', 'E')): raise BOPError('BIS returned an invalid CHF index value.')
        if at >= '1997-01-01': rows.append({'date': at, 'currency_index': value})
    if not rows or not any(row['currency_index'] is not None for row in rows): raise BOPError('BIS returned no CHF index observations.')
    return sorted(rows, key=lambda row: row['date'])


def validate_swiss(rows, group):
    identities = {
        'accounts': {
            'current_account': ('current_account', [('goods', 1), ('services', 1), ('primary_income', 1), ('secondary_income', 1)]),
            'primary_income': ('primary_income', [('income_receipts', 1), ('income_payments', -1)]),
            'customs_balance': ('customs_goods_native', [('customs_exports', 1), ('customs_imports', -1)]),
            'goods_details': ('goods', [('goods_detail_check', 1)]),
            'goods_bridge': ('goods', [('customs_goods_native', 1), ('goods_supplements', 1), ('goods_merchanting', 1)]),
            'financial_ex_derivatives': ('financial_ex_derivatives', [('assets_total', 1), ('liabilities_total', -1)]),
            'external_accounts': ('statistical_discrepancy', [('financial_ex_derivatives', 1), ('derivatives_net', 1), ('current_account', -1), ('capital_account', -1)]),
        },
        'investment': {
            'portfolio_assets': ('portfolio_assets', [('equity_assets', 1), ('debt_assets', 1)]),
            'portfolio_liabilities': ('portfolio_liabilities', [('equity_liabilities', 1), ('debt_liabilities', 1)]),
            'direct_outward': ('direct_net_outward', [('direct_assets', 1), ('direct_liabilities', -1)]),
            'portfolio_outward': ('portfolio_net_outward', [('portfolio_assets', 1), ('portfolio_liabilities', -1)]),
            'other_outward': ('other_net_outward', [('other_assets', 1), ('other_liabilities', -1)]),
            'full_financial_account': ('financial_check', [('direct_net_outward', 1), ('portfolio_net_outward', 1), ('other_net_outward', 1), ('reserve_transactions', 1), ('derivatives_check', 1)]),
        },
        'positions': {'reserve_components': ('reserve_stock', [('reserve_gold', 1), ('reserve_imf', 1), ('reserve_sdr', 1), ('reserve_foreign_currency', 1), ('reserve_other', 1)])},
    }
    checks = []
    for name, (total, terms) in identities.get(group, {}).items():
        errors = [abs(row[total] - sum(row[field] * sign for field, sign in terms)) for row in rows
                  if row.get(total) is not None and all(row.get(field) is not None for field, _ in terms)]
        # SNB's historical income net and receipts/payments differ by up to
        # CHF 0.2m in older quarters. Preserve each published series, recording this
        # small native residual separately from the tighter other identities.
        tolerance = .0002001 if name == 'primary_income' else .000001
        if not errors or max(errors) > tolerance: raise BOPError(f'Swiss accounting identity failed: {name}.')
        checks.append({'name': name, 'observations_checked': len(errors), 'max_difference_chf_billions': max(errors), 'tolerance_chf_billions': tolerance})
    return {'status': 'passed', 'checks': checks}


def build_switzerland_view(snapshot, frequency='trailing4q'):
    if frequency not in ('quarterly', 'trailing4q'): raise ValueError('Choose quarterly or trailing4q.')
    groups = snapshot['groups']; accounts = groups.get('accounts', {}).get('rows', [])
    if not accounts: raise BOPError('No verified Swiss accounts are available. Refresh to retry the SNB.')
    validation = validate_swiss(accounts, 'accounts')
    for group in ('investment', 'positions'):
        if groups.get(group, {}).get('rows'): validation['checks'].extend(validate_swiss(groups[group]['rows'], group)['checks'])
    base = []
    for source in accounts:
        if any(source.get(field) is None for field in ('current_account', 'goods', 'services', 'primary_income', 'secondary_income')): continue
        row = {**{field: None for field in FIELDS}, **source, 'period': quarter_label(source['date'])}
        row['customs_goods'] = row['customs_exports'] - row['customs_imports'] if row.get('customs_exports') is not None and row.get('customs_imports') is not None else None
        row['goods_gap'] = row['customs_goods'] - row['goods'] if row['customs_goods'] is not None else None
        row['financial_account'] = row['financial_ex_derivatives'] + row['derivatives_net'] if row.get('financial_ex_derivatives') is not None and row.get('derivatives_net') is not None else None
        row['net_financial_inflow'] = -row['financial_account'] if row['financial_account'] is not None else None
        row['net_lending'] = row['current_account'] + row['capital_account'] if row.get('capital_account') is not None else None
        base.append(row)
    selected = aggregate(base, frequency)
    if not selected: raise BOPError('No complete Swiss BOP periods are available.')
    periods = 4 if frequency == 'trailing4q' else 1
    lookup = {name: {row['date']: row for row in group['rows']} for name, group in groups.items()}
    currencies = {monthly_index(row['date']): row['currency_index'] for row in groups.get('currency', {}).get('rows', [])}
    macro, windows = [], {}
    for index, source in enumerate(base):
        at = source['date']; q = quarter_index(at); window = base[max(0, index - periods + 1):index + 1]
        if [quarter_index(row['date']) for row in window] != list(range(q - periods + 1, q + 1)): window = []
        windows[at] = window; ca = strict_sum(window, 'current_account')
        gdps = [lookup.get('gdp', {}).get(row['date'], {}).get('gdp') for row in window]
        gdp = sum(gdps) if gdps and all(value is not None for value in gdps) else None
        investment = [lookup.get('investment', {}).get(row['date'], {}) for row in window]
        def inward(category):
            assets, liabilities = strict_sum(investment, category + '_assets'), strict_sum(investment, category + '_liabilities')
            return liabilities - assets if assets is not None and liabilities is not None else None
        parts = [lookup.get('debt', {}).get(at, {}).get(field) for field in ('debt_government', 'debt_central_bank', 'debt_banks', 'debt_other')]
        debt = sum(parts) if all(value is not None for value in parts) else None
        stock = lookup.get('positions', {}).get(at, {}).get('reserve_stock')
        indices = [currencies.get(month) for month in range(q * 3 - (periods - 1) * 3, q * 3 + 3)]
        macro.append({'date': at, 'period': quarter_label(at),
                      'current_account_gdp_pct': ca / gdp * 100 if ca is not None and gdp is not None and gdp > 0 else None,
                      **{'net_' + key: inward(key) for key in ('direct', 'equity', 'debt', 'other')},
                      'reserve_transactions': strict_sum(investment, 'reserve_transactions'), 'reserve_stock': stock,
                      'short_term_external_debt': debt, 'reserves_debt_pct': stock / debt * 100 if stock is not None and debt is not None and debt > 0 else None,
                      'currency_index': sum(indices) / len(indices) if all(value is not None for value in indices) else None})
    sources = [{'group': name, 'name': GROUP_NAMES[name], 'url': group['url'], 'checked_at': group['checked_at'],
                'retrieved_at': group['checked_at'], 'latest_observation': group['rows'][-1]['date'], 'series': group.get('series', []),
                'downloads': group.get('downloads', [])} for name, group in groups.items() if group.get('rows')]
    contributors = windows[selected[-1]['date']]; trade = groups.get('trade', {}).get('rows', [])
    return {'country': deepcopy(next(item for item in COUNTRIES if item['code'] == 'CHE')), 'countries': deepcopy(COUNTRIES),
            'status': 'ready', 'frequency': frequency, 'units': 'CHF billions', 'reporting_currency': 'CHF',
            'seasonal_adjustment': 'Not seasonally adjusted', 'retrieved_at': groups['accounts']['checked_at'],
            'last_successful_check_at': groups['accounts']['checked_at'], 'reference_url': 'https://data.snb.ch/en/topics/aube/cube/bopoverq',
            'series': selected, 'latest': selected[-1], 'period_start': contributors[0]['period'], 'period_end': selected[-1]['period'],
            'contributing_quarters': deepcopy(contributors), 'latest_quarter': deepcopy(base[-1]), 'insights': [], 'validation': validation, 'sources': sources,
            'methodology': [
                'SNB external accounts are quarterly NSA. SECO GDP is nominal, unadjusted, not sports-event adjusted and measures the actual quarter. Million-CHF values become billions; SNB SDDS debt is already in CHF billions. GDP is never divided by four.',
                'Net inward investment = liabilities incurred minus assets acquired. Portfolio equity includes collective investment schemes. Derivatives and reserves are excluded from the four investment bars. The complete financial account adds derivatives to the overview total explicitly labelled excluding derivatives.',
                'Reserve transactions preserve the SNB acquisition sign and exclude valuation changes. Reserve stocks include gold. Reserve coverage sums government, central bank, banks and other-sector short-term external debt, on original maturity. Intercompany debt has no maturity split and is excluded. Stocks are never summed.',
                'Customs comparison uses SNB-published FOCBS broad foreign trade, including non-monetary gold, versus full BOP goods; both NSA. Imports are CIF at customs and FOB in BOP. Merchanting, ownership, supplements, freight, timing and revisions also affect the gap. The published customs-to-BOP goods bridge is checked.',
                'The monthly companion uses FOCBS business-cycle total 1, seasonally and working-day adjusted, via SNB. It excludes gold bars and other precious metals, coins, precious stones and gems, works of art and antiques. This goods-only measure differs from the broad customs comparison. Complete monthly quarters form flows, moving averages and year-on-year changes.',
                'The direct BIS broad nominal CHF index is monthly, 2020=100, and is distinct from the SNB effective-franc indices. Three/twelve complete months form quarter/4Q averages; higher means a stronger franc.',
            ],
            'macro': {'country_code': 'CHE', 'status': 'ready' if len(groups) == 7 else 'partial', 'frequency': frequency,
                      'quarterly': macro, 'trade': build_trade_view(trade, frequency), 'monthly': deepcopy(trade), 'trade_scope': 'merchandise',
                      'sources': sources, 'warnings': list(snapshot.get('warnings', []))}}


class SwitzerlandBOPService:
    def __init__(self, cache_path, enabled=True, transport=None):
        self.cache_path = Path(cache_path); self.enabled = enabled; self.transport = transport or fetch_switzerland
        self.lock = RLock(); self.last_completed = self.last_failure = self.last_attempt_at = None
        self.snapshot = {'schema_version': 1, 'country': 'CHE', 'groups': {}, 'warnings': []}
        for path in (self.cache_path, SNAPSHOT):
            try:
                candidate = json.loads(path.read_text(encoding='utf-8'))
                if candidate['schema_version'] != 1 or candidate['country'] != 'CHE': continue
                build_switzerland_view(candidate); self.snapshot = candidate; break
            except (OSError, ValueError, KeyError, TypeError, BOPError): continue

    def _download(self, name):
        if name in SPEC:
            observations, metadata, downloads = {}, [], []
            for definition in SPEC[name]:
                url = SNB_ROOT + definition['cube'] + '/data/json/en'; downloads.append(url)
                part, details = parse_snb(self.transport(url), definition); metadata.extend(details)
                for row in part: observations.setdefault(row['date'], {'date': row['date']}).update(row)
            rows = [{**{field: None for field in GROUP_FIELDS[name]}, **observations[at]} for at in sorted(observations)]
            validate_swiss(rows, name); url = 'https://data.snb.ch/en/topics/' + ('cross' if name == 'debt' else 'aube') + '/cube/' + SPEC[name][0]['cube']
        elif name == 'gdp':
            rows, metadata = parse_seco_gdp(self.transport(GDP_DATA), self.transport(GDP_META)); downloads = [GDP_DATA, GDP_META]
            url = 'https://www.seco.admin.ch/en/gross-domestic-product'
        else:
            rows = parse_bis_chf(self.transport(CURRENCY)); url = CURRENCY; downloads = [url]
            metadata = [{'field': 'currency_index', 'id': 'M.N.B.CH', 'name': 'BIS broad nominal effective CHF index', 'native_unit': '2020 = 100', 'frequency': 'Monthly', 'url': 'https://data.bis.org/topics/EER/BIS,WS_EER,1.0/M.N.B.CH'}]
        positive = ('reserve_stock', 'reserve_gold', 'reserve_imf', 'reserve_sdr', 'reserve_foreign_currency', 'gdp', 'debt_government', 'debt_central_bank', 'debt_banks', 'debt_other', 'exports', 'imports')
        if any(row.get(field) is not None and row[field] < 0 for row in rows for field in positive): raise BOPError('A Swiss GDP, stock or merchandise total became negative.')
        return {'url': url, 'rows': rows, 'series': metadata, 'downloads': downloads, 'checked_at': datetime.now(timezone.utc).isoformat()}

    def view(self, frequency='trailing4q', refresh=False):
        requested_at = time.monotonic()
        with self.lock:
            backoff = self.last_failure is not None and time.monotonic() - self.last_failure < 30
            shared = self.last_completed is not None and self.last_completed >= requested_at; performed = False
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
                                    if not before.issubset(after): raise BOPError('Swiss source coverage regressed.')
                            candidate['groups'][name] = group
                        except (OSError, HTTPException, ValueError, TypeError, KeyError, subprocess.SubprocessError, BOPError):
                            warnings.append(f'{GROUP_NAMES[name]} refresh failed; saved observations retain their original check time.')
                candidate['warnings'] = warnings; build_switzerland_view(candidate, frequency); self.snapshot = candidate
                try:
                    self.cache_path.parent.mkdir(parents=True, exist_ok=True); temporary = self.cache_path.with_suffix('.tmp')
                    temporary.write_text(json.dumps(candidate, allow_nan=False), encoding='utf-8'); temporary.replace(self.cache_path)
                except OSError: self.snapshot['warnings'].append('Fresh Swiss data is in memory, but its local cache could not be saved.')
                self.last_failure = time.monotonic() if warnings else None; self.last_completed = time.monotonic()
            result = build_switzerland_view(self.snapshot, frequency)
            result.update({'refresh_available': self.enabled,
                           'delivery': 'source_checked' if refresh and not self.snapshot.get('warnings') and (performed or shared) else 'saved_snapshot',
                           'last_attempt_at': self.last_attempt_at, 'refresh_error': None, 'source_warning': None,
                           'refresh_cooldown_seconds': max(0, math.ceil(30 - (time.monotonic() - self.last_failure))) if self.last_failure else 0})
            return result
