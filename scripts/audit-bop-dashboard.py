"""Audit the running BOP dashboard against fresh official source downloads.

Independent Decimal calculations cover every source observation and every
plotted field in both frequencies. Raw downloads and API responses are saved
with hashes so a result can be reproduced without contacting the providers.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, getcontext
from html.parser import HTMLParser
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sys
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.bop import CENSUS_URL, FRED_URL, SERIES
from backend.bop_macro import DEBT_INDEX, DEBT_ROOT, GROUPS, source_url

getcontext().prec = 40
D = Decimal
MILLION = D(1000)


def number(value):
    if value is None or str(value).strip() in ('', '.', 'NA', '(NA)'):
        return None
    result = D(str(value).replace(',', '').replace('\u2212', '-').strip())
    if not result.is_finite():
        raise ValueError('Non-finite source observation')
    return result


def qi(at):
    return int(at[:4]) * 4 + (int(at[5:7]) - 1) // 3


def mi(at):
    return int(at[:4]) * 12 + int(at[5:7]) - 1


def qdate(index):
    return f'{index // 4}-{index % 4 * 3 + 1:02d}-01'


def total(values):
    return sum(values, D(0)) if values and all(value is not None for value in values) else None


class Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.links = [], None, None, {}

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row = []
        elif tag in ('td', 'th') and self.row is not None:
            self.cell = []
        elif tag == 'a':
            href = dict(attrs).get('href', '')
            match = re.search(r'/(debtd(\d{4})q([1-4])\.html?)$', href)
            if match:
                self.links[f'{match[2]}-{(int(match[3])-1)*3+1:02d}-01'] = DEBT_ROOT + match[1]

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.row is not None and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split()))
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row, self.cell = None, None


def read_csv(text, keys):
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    if not {'observation_date', *keys}.issubset(reader.fieldnames or []):
        raise ValueError('An official source response omitted requested series')
    result = {}
    for row in reader:
        at = row['observation_date']
        if at in result:
            raise ValueError('Duplicate source date')
        result[at] = {key: number(row[key]) for key in keys}
    return result


def treasury_value(text, expected_date):
    parser = Tables()
    parser.feed(text)
    header = ' '.join(' '.join(row) for row in parser.rows[:8])
    match = re.search(r'as of\s+(March|June|Sept(?:ember)?|December)\s+\d+,?\s+(\d{4})', header, re.I)
    if not match:
        raise ValueError('Unknown Treasury reporting date')
    month = {'march': 1, 'june': 4, 'sept': 7, 'september': 7, 'december': 10}[match[1].lower()]
    actual = f'{match[2]}-{month:02d}-01'
    if actual != expected_date:
        return None, actual
    for index, row in enumerate(parser.rows):
        if any(re.fullmatch(r'Total(?:\s+\d+/)?', cell) for cell in row):
            for following in parser.rows[index + 1:index + 4]:
                if 'Principal' in following:
                    values = [number(cell) for cell in following if re.fullmatch(r'[\d,]+(?:\.\d+)?', cell)]
                    if len(values) == 1 and values[0] > 0:
                        return values[0] / MILLION, actual
    raise ValueError('Treasury total principal unavailable')


def download(url):
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; MeridianLocalDashboard/1.0)', 'Accept': 'text/csv,text/html;q=0.9,*/*;q=0.5', 'Cache-Control': 'no-cache', 'Pragma': 'no-cache'})
    with urlopen(request, timeout=30) as response:
        body = response.read(5_000_001)
    if len(body) > 5_000_000:
        raise ValueError('Oversized response')
    try:
        return body.decode('utf-8-sig')
    except UnicodeDecodeError:
        if url.startswith(DEBT_ROOT):
            return body.decode('cp1252')
        raise


def collect(directory, api):
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {}
    def persist(name, text, url):
        (directory / name).write_bytes(text.encode('utf-8'))
        manifest[name] = {'url': url, 'checked_at': datetime.now(timezone.utc).isoformat(), 'sha256': hashlib.sha256(text.encode()).hexdigest()}
        return text
    def save(name, url):
        return persist(name, download(url), url)
    started = datetime.now(timezone.utc).isoformat()
    before = json.loads(download(api + '/fx/bop?refresh=false'))
    persist('api-before.json', json.dumps(before), api + '/fx/bop?refresh=false')
    quarterly = json.loads(save('api-quarterly.json', api + '/fx/bop?frequency=quarterly&refresh=true'))
    save('api-trailing4q.json', api + '/fx/bop?frequency=trailing4q&refresh=false')
    cache_path = ROOT / 'data' / 'bop-cache' / 'usa-macro.json'
    persist('snapshot-macro.json', cache_path.read_text(encoding='utf-8'), 'Persisted official-source observations from the completed live check')
    print('Live source check:', quarterly.get('delivery'), quarterly.get('refresh_error'), flush=True)
    urls = {'fred-core.csv': FRED_URL, 'census.html': CENSUS_URL, 'treasury-index.html': DEBT_INDEX,
            **{name + '.csv': source_url(ids) for name, (ids, _) in GROUPS.items()}}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(save, name, url): name for name, url in urls.items()}
        for future in as_completed(futures):
            future.result()
            print('Downloaded', futures[future], flush=True)
    links = Tables()
    links.feed((directory / 'treasury-index.html').read_text(encoding='utf-8'))
    if not links.links:
        raise ValueError('No Treasury debt tables discovered')
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(save, 'debt-' + at + '.html', url): at for at, url in links.links.items()}
        for future in as_completed(futures):
            future.result()
    (directory / 'manifest.json').write_text(json.dumps({'started_at': started, 'files': manifest}, indent=2), encoding='utf-8')
    print('Downloaded Treasury tables:', len(links.links), flush=True)


def audit(directory):
    load = lambda name: (directory / name).read_bytes().decode('utf-8')
    manifest = json.loads(load('manifest.json'))
    for name, info in manifest['files'].items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != info['sha256']:
            raise ValueError('Audit input changed after download: ' + name)
    counts, failures, maximum = {}, [], {}
    failure_count = 0
    def check(category, key, actual, expected, tolerance=D('0.00000001')):
        nonlocal failure_count
        counts[category] = counts.get(category, 0) + 1
        try:
            actual_number = number(actual)
            delta = abs(actual_number - expected) if actual_number is not None and expected is not None else D(0)
            valid = (actual_number is None) == (expected is None) and delta <= tolerance
            maximum[category] = max(maximum.get(category, D(0)), delta)
        except (InvalidOperation, ValueError, TypeError):
            valid = False
        if not valid:
            failure_count += 1
            if len(failures) < 100:
                failures.append({'category': category, 'field': key, 'actual': actual, 'expected': str(expected) if expected is not None else None})

    core_raw = read_csv(load('fred-core.csv'), [series_id for series_id, _ in SERIES.values()])
    inputs = {name: read_csv(load(name + '.csv'), ids) for name, (ids, _) in GROUPS.items()}
    census = Tables()
    census.feed(load('census.html'))
    months = {}
    month_names = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']
    for row in census.rows:
        if len(row) == 4:
            match = re.fullmatch(r'([A-Za-z]+)\s+(\d{4})', row[0])
            if match and match[1] in month_names:
                months[int(match[2]) * 12 + month_names.index(match[1])] = [number(value) for value in row[1:]]
    core = {}
    for at, raw in core_raw.items():
        row = {field: raw[series_id] / MILLION if raw[series_id] is not None else None for field, (series_id, _) in SERIES.items()}
        window = [months.get(index) for index in range(mi(at), mi(at) + 3)]
        for column, field in enumerate(('customs_exports', 'customs_imports', 'customs_goods')):
            row[field] = sum((month[column] for month in window), D(0)) / MILLION if all(month is not None for month in window) else None
        row['goods_gap'] = row['customs_goods'] - row['goods'] if row['customs_goods'] is not None and row['goods'] is not None else None
        row['net_financial_inflow'] = -row['financial_account'] if row['financial_account'] is not None else None
        core[qi(at)] = row
    debt, omitted = {}, []
    for filename in sorted(manifest['files']):
        if not (filename.startswith('debt-') and filename.endswith('.html')):
            continue
        path = directory / filename
        at = path.stem[5:]
        value, reported = treasury_value(path.read_text(encoding='utf-8'), at)
        debt[at] = value
        if value is None:
            omitted.append({'linked_quarter': at, 'reported_quarter': reported, 'action': 'Excluded, not relabelled or filled'})
    # Verify the persisted source data independently, including all daily quotes
    # that feed the displayed currency averages.
    cache = json.loads(load('snapshot-macro.json'))
    for name, raw_rows in inputs.items():
        cached = {row['date']: row for row in cache['groups'][name]['rows']}
        for at, values in raw_rows.items():
            for field, expected in values.items():
                check('raw_source_observations', name + ':' + at + ':' + field, cached.get(at, {}).get(field), expected)
        check('source_coverage', name, len(cached), D(len(raw_rows)), D(0))
    cached_debt = {row['date']: row['principal_due_1y'] for row in cache['groups']['debt']['rows']}
    for at, expected in debt.items():
        check('treasury_principal', at, cached_debt.get(at), expected)

    monthly = {mi(at): {'exports': values['BOPTEXP'] / MILLION if values['BOPTEXP'] is not None else None,
                         'imports': values['BOPTIMP'] / MILLION if values['BOPTIMP'] is not None else None} for at, values in inputs['monthly'].items()}
    trade_quarters = {}
    for quarter in range(min(monthly) // 3, max(monthly) // 3 + 1):
        values = {field: total([monthly.get(index, {}).get(field) for index in range(quarter * 3, quarter * 3 + 3)]) for field in ('exports', 'imports')}
        if all(value is not None for value in values.values()):
            trade_quarters[quarter] = values
    now_quarter = qi(manifest['started_at'][:10])
    quotes = {}
    for at, values in inputs['daily'].items():
        if qi(at) < now_quarter:
            group = quotes.setdefault(qi(at), {'DTWEXBGS': [], 'DEXUSEU': []})
            for field, value in values.items():
                if value is not None:
                    group[field].append(value)
    mode_results = {}
    for frequency in ('quarterly', 'trailing4q'):
        api = json.loads(load('api-' + frequency + '.json'))
        if api['frequency'] != frequency or api['macro']['frequency'] != frequency:
            raise ValueError('API frequency mismatch')
        length = 1 if frequency == 'quarterly' else 4
        for row in api['series']:
            quarter = qi(row['date'])
            for field in core[quarter]:
                expected = total([core.get(index, {}).get(field) for index in range(quarter - length + 1, quarter + 1)])
                check('core_' + frequency, row['date'] + ':' + field, row.get(field), expected)
            for field, terms in [
                ('current_account', [('goods', 1), ('services', 1), ('primary_income', 1), ('secondary_income', 1)]),
                ('primary_income', [('income_receipts', 1), ('income_payments', -1)]),
                ('net_lending', [('current_account', 1), ('capital_account', 1)]),
                ('statistical_discrepancy', [('financial_account', 1), ('net_lending', -1)]),
            ]:
                expected = total([number(row.get(key)) * weight if row.get(key) is not None else None for key, weight in terms])
                check('accounting_identities', frequency + ':' + row['date'] + ':' + field, row.get(field), expected, D('.005') * length)
        for row in api['macro']['quarterly']:
            quarter, at = qi(row['date']), row['date']
            window = list(range(quarter - length + 1, quarter + 1))
            ca = total([core.get(index, {}).get('current_account') for index in window])
            gdp = total([inputs['quarterly'].get(qdate(index), {}).get('GDP') for index in window])
            ratio = 100 * ca / (gdp / 4) if ca is not None and gdp is not None and gdp > 0 else None
            check('macro_' + frequency, at + ':current_account_gdp_pct', row['current_account_gdp_pct'], ratio)
            for field, asset, liability in [('net_direct', 'IEAADI', 'IEAIDI'), ('net_equity', 'IEAAPIE', 'IEAIPIE'), ('net_debt', 'IEAAPID', 'IEAIPID'), ('net_other', 'IEAAOI', 'IEAIOI')]:
                assets = total([inputs['quarterly'].get(qdate(index), {}).get(asset) for index in window])
                liabilities = total([inputs['quarterly'].get(qdate(index), {}).get(liability) for index in window])
                expected = (liabilities - assets) / MILLION if assets is not None and liabilities is not None else None
                check('macro_' + frequency, at + ':' + field, row[field], expected)
            flows = total([inputs['quarterly'].get(qdate(index), {}).get('IEAAR') for index in window])
            check('macro_' + frequency, at + ':reserve_transactions', row['reserve_transactions'], flows / MILLION if flows is not None else None)
            stock = inputs['reserve_stock'].get(at, {}).get('IIPRESEQ')
            check('macro_' + frequency, at + ':reserve_stock', row['reserve_stock'], stock / MILLION if stock is not None else None)
            ratio = 100 * stock / MILLION / debt[at] if stock is not None and debt.get(at) is not None else None
            check('macro_' + frequency, at + ':reserves_debt_pct', row['reserves_debt_pct'], ratio)
            imports = total([monthly.get(index, {}).get('imports') for index in range(quarter * 3 - 9, quarter * 3 + 3)])
            cover = stock / MILLION / (imports / 12) if stock is not None and imports is not None and imports > 0 else None
            check('macro_' + frequency, at + ':import_cover_months', row['import_cover_months'], cover)
            for field, key in [('broad_dollar', 'DTWEXBGS'), ('eurusd', 'DEXUSEU')]:
                lists = [quotes.get(index, {}).get(key, []) for index in window]
                values = [value for group in lists for value in group]
                average = sum(values, D(0)) / len(values) if all(lists) and values and all(index in core for index in window) else None
                check('macro_' + frequency, at + ':' + field, row[field], average)
        expected_trade = {}
        for quarter in range(min(trade_quarters), max(trade_quarters) + 1):
            expected_trade[quarter] = {field: total([trade_quarters.get(index, {}).get(field) for index in range(quarter - length + 1, quarter + 1)]) for field in ('exports', 'imports')}
        for row in api['macro']['trade']:
            quarter = qi(row['date'])
            expected = dict(expected_trade[quarter])
            expected['trade_balance'] = expected['exports'] - expected['imports'] if all(value is not None for value in expected.values()) else None
            for field in ('exports', 'imports'):
                mean = total([expected_trade.get(index, {}).get(field) for index in range(quarter - 2, quarter + 1)])
                prior = expected_trade.get(quarter - 4, {}).get(field)
                expected[field + '_ma3'] = mean / 3 if mean is not None else None
                expected[field + '_yoy'] = (expected[field] / prior - 1) * 100 if expected[field] is not None and prior is not None and prior > 0 else None
            for field, value in expected.items():
                check('trade_' + frequency, row['date'] + ':' + field, row[field], value)
        for row in api['macro']['monthly']:
            index = mi(row['date'])
            for field in ('exports', 'imports'):
                value = monthly[index][field]
                check('monthly_source_values', row['date'] + ':' + field, row[field], value)
                previous = monthly.get(index - 12, {}).get(field)
                average = total([monthly.get(n, {}).get(field) for n in range(index - 2, index + 1)])
                check('monthly_derived_values', row['date'] + ':' + field + '_ma3', row[field + '_ma3'], average / 3 if average is not None else None)
                growth = (value / previous - 1) * 100 if previous is not None and previous > 0 else None
                check('monthly_derived_values', row['date'] + ':' + field + '_yoy', row[field + '_yoy'], growth)
            check('monthly_derived_values', row['date'] + ':trade_balance', row['trade_balance'], monthly[index]['exports'] - monthly[index]['imports'])
        mode_results[frequency] = {'latest_bop': api['latest']['period'], 'latest_trade': api['macro']['trade'][-1]['period'], 'latest_core': api['latest'], 'latest_macro': api['macro']['quarterly'][-1], 'latest_trade_values': api['macro']['trade'][-1]}
    before = json.loads(load('api-before.json'))
    fresh = json.loads(load('api-quarterly.json'))
    old_checks = {source['group']: source['checked_at'] for source in before['macro']['sources']}
    checks = fresh['macro']['sources']
    refresh_ok = fresh['refresh_available'] and fresh['delivery'] == 'source_checked' and not fresh['refresh_error'] and len(checks) == 5 and all(source['checked_at'] > old_checks.get(source['group'], '') for source in checks) and not any('source refresh failed' in warning for warning in fresh['macro']['warnings'])
    check('live_refresh', 'All official source families refreshed', int(refresh_ok), D(1), D(0))
    dates = {name: {key: max((at for at, values in rows.items() if values[key] is not None), default=None) for key in next(iter(rows.values()))} for name, rows in inputs.items()}
    return {'audited_at': datetime.now(timezone.utc).isoformat(), 'status': 'passed' if failure_count == 0 else 'differences_found',
            'total_values_checked': sum(counts.values()), 'checks': counts, 'failure_count': failure_count, 'failures': failures,
            'maximum_absolute_differences': {key: str(value) for key, value in maximum.items()},
            'source_files': manifest['files'], 'source_directory': str(directory.resolve()), 'source_latest_observations': dates,
            'live_refresh_verified': bool(refresh_ok), 'source_checks': checks, 'core_last_successful_check': fresh['last_successful_check_at'],
            'known_source_gaps': omitted, 'modes': mode_results,
            'scope': 'All source history, all seven BOP charts, quarterly and rolling-four-quarter calculations, raw monthly trade and live source checks. No synthetic inputs or forward-filled observations.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--api', default='http://127.0.0.1:8000/api')
    day = datetime.now(timezone.utc).date().isoformat()
    parser.add_argument('--source-dir', type=Path, default=ROOT.parent / '.runtime' / 'bop-dashboard-audit' / day)
    parser.add_argument('--output', type=Path, default=ROOT / 'data' / f'bop-dashboard-audit-{day}.json')
    args = parser.parse_args()
    if args.download:
        collect(args.source_dir, args.api)
    report = audit(args.source_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('status', 'total_values_checked', 'failure_count', 'failures', 'live_refresh_verified', 'known_source_gaps')}, indent=2))
    print('Report:', args.output.resolve())
    if report['failure_count']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
