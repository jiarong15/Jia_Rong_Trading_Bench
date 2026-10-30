"""Independent Decimal calculations from native Canadian source CSV files."""
import argparse
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import io
import json
from pathlib import Path
import sys
from urllib.request import urlopen
import zipfile
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.bop_canada import CanadaBOPService
D = lambda value: Decimal(str(value))
CORE = {'current_account': 'v61915077', 'goods': 'v61915093', 'services': 'v61915103', 'primary_income': 'v61915151',
        'secondary_income': 'v61915199', 'capital_account': 'v61915228', 'net_lending': 'v61915236',
        'income_receipts': 'v61914850', 'income_payments': 'v61914998', 'financial_account': 'v61913615', 'statistical_discrepancy': 'v61913814'}
FLOWS = {'direct': ('v61913732', 'v61913631'), 'equity': ('v61913772', 'v61913671'),
         'debt': ('v61913748', 'v61913647'), 'other': ('v61913780', 'v61913692')}


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--live', action='store_true'); args = parser.parse_args()
    started = datetime.now(timezone.utc); service = CanadaBOPService(ROOT / 'data' / 'bop-cache' / 'canada.json', enabled=False)
    raw, inputs = {}, []
    def qindex(at): return int(at[:4]) * 4 + (int(at[5:7]) - 1) // 3
    def mindex(at): return int(at[:4]) * 12 + int(at[5:7]) - 1
    for path in (ROOT.parent / '.runtime' / 'ca-bop-research' / 'live-inputs').glob('*.url'):
        url = path.read_text(encoding='utf-8'); body = path.with_suffix('.bin').read_bytes()
        inputs.append({'url': url, 'sha256': sha256(body).hexdigest()})
        if url.endswith('-eng.zip'):
            pid = url.rsplit('/', 1)[-1].split('-')[0]
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                records = csv.DictReader(io.TextIOWrapper(archive.open(pid + '.csv'), encoding='utf-8-sig'))
                for row in records:
                    if row['REF_DATE'] < '1997-01': continue
                    if row['GEO'] != 'Canada' or row['UOM'] != 'Dollars' or row['SCALAR_FACTOR'] != 'millions': continue
                    at = row['REF_DATE'] + '-01'; index = mindex(at) if pid.startswith('12') else qindex(at)
                    values = raw.setdefault(row['VECTOR'], {}); assert index not in values
                    values[index] = D(row['VALUE']) / 1000 if row['VALUE'] else None
        elif '/WS_EER/M.N.B.CA?' in url:
            for row in csv.DictReader(io.StringIO(body.decode('utf-8-sig'))):
                assert (row['REF_AREA'], row['FREQ'], row['EER_TYPE'], row['EER_BASKET']) == ('CA', 'M', 'N', 'B')
                if row['TIME_PERIOD'] >= '1997-01': raw.setdefault('M.N.B.CA', {})[mindex(row['TIME_PERIOD'] + '-01')] = D(row['OBS_VALUE']) if row['OBS_VALUE'] else None
    needed = {meta['id'] for group in service.snapshot['groups'].values() for meta in group['series']}
    assert needed.issubset(raw) and len(inputs) == 8, 'Saved official inputs are incomplete.'
    counts, failures = {}, []
    def check(actual, expected, context):
        family = context.split('/')[0]; counts[family] = counts.get(family, 0) + 1
        valid = actual is None if expected is None else actual is not None and abs(D(actual) - expected) <= D('.0000011')
        if not valid: failures.append({'context': context, 'actual': actual, 'expected': str(expected)})
    def total(code, end, periods):
        values = [raw.get(code, {}).get(index) for index in range(end - periods + 1, end + 1)]
        return sum(values) if values and all(value is not None for value in values) else None
    def minus(a, b): return a - b if a is not None and b is not None else None
    def plus(a, b): return a + b if a is not None and b is not None else None
    for group in service.snapshot['groups'].values():
        for row in group['rows']:
            for meta in group['series']:
                index = qindex(row['date']) if meta['frequency'] == 'Quarterly' else mindex(row['date'])
                check(row[meta['field']], raw[meta['id']].get(index), f'source/{meta["id"]}/{row["date"]}')
    for frequency, periods in [('quarterly', 1), ('trailing4q', 4)]:
        view = service.view(frequency)
        for row in view['series']:
            q = qindex(row['date'])
            for field, code in CORE.items(): check(row[field], total(code, q, periods), f'core/{frequency}/{field}/{row["date"]}')
            exports, imports = total('v87008868', q * 3 + 2, periods * 3), total('v87008752', q * 3 + 2, periods * 3)
            customs = minus(exports, imports)
            for field, expected in [('customs_exports', exports), ('customs_imports', imports), ('customs_goods', customs), ('goods_gap', minus(customs, total('v61915093', q, periods)))]:
                check(row[field], expected, f'core/{frequency}/{field}/{row["date"]}')
            financial = total('v61913615', q, periods)
            check(row['net_financial_inflow'], -financial if financial is not None else None, f'core/{frequency}/net_inflow/{row["date"]}')
        for row in view['macro']['quarterly']:
            q = qindex(row['date']); ca, gdp = total('v61915077', q, periods), total('v62305814', q, periods)
            check(row['current_account_gdp_pct'], ca / gdp * 100 if ca is not None and gdp else None, f'macro/{frequency}/CA_GDP/{row["date"]}')
            for category, (liability, asset) in FLOWS.items():
                check(row['net_' + category], minus(total(liability, q, periods), total(asset, q, periods)), f'macro/{frequency}/net_{category}/{row["date"]}')
            check(row['reserve_transactions'], total('v61913679', q, periods), f'macro/{frequency}/reserve_flow/{row["date"]}')
            stock = raw['v61916494'].get(q); debt = raw['v101870062'].get(q)
            for field, expected in [('reserve_stock', stock), ('short_term_external_debt', debt), ('reserves_debt_pct', stock / debt * 100 if stock is not None and debt else None)]:
                check(row[field], expected, f'macro/{frequency}/{field}/{row["date"]}')
            index = total('M.N.B.CA', q * 3 + 2, periods * 3)
            check(row['currency_index'], index / (periods * 3) if index is not None else None, f'macro/{frequency}/CAD_index/{row["date"]}')
        for row in view['macro']['monthly']:
            month = mindex(row['date'])
            for field, codes in [('exports', ('v87008926', 'v1105277794')), ('imports', ('v87008810', 'v1105277804'))]:
                check(row[field], plus(raw[codes[0]].get(month), raw[codes[1]].get(month)), f'monthly/{field}/{row["date"]}')
        for row in view['macro']['trade']:
            q = qindex(row['date']); levels = {}
            for field, codes in [('exports', ('v87008926', 'v1105277794')), ('imports', ('v87008810', 'v1105277804'))]:
                def level(end): return plus(*(total(code, end * 3 + 2, periods * 3) for code in codes))
                current = level(q); previous = level(q - 4); levels[field] = current
                check(row[field], current, f'trade/{frequency}/{field}/{row["date"]}')
                values = [level(q - lag) for lag in range(3)]
                check(row[field + '_ma3'], sum(values) / 3 if all(value is not None for value in values) else None, f'trade/{frequency}/{field}_MA3/{row["date"]}')
                check(row[field + '_yoy'], (current / previous - 1) * 100 if current is not None and previous else None, f'trade/{frequency}/{field}_YoY/{row["date"]}')
            check(row['trade_balance'], minus(levels['exports'], levels['imports']), f'trade/{frequency}/balance/{row["date"]}')
    live = None
    if args.live:
        with urlopen('http://127.0.0.1:8000/api/fx/bop?country=CAN&refresh=true', timeout=150) as response:
            assert response.headers['Cache-Control'] == 'no-store'; live = json.load(response)
        assert live['delivery'] == 'source_checked' and live['refresh_available'] and not live['macro']['warnings']
        assert all(datetime.fromisoformat(source['checked_at']) >= started for source in live['sources'])
        for key in ('series',): assert live[key] == service.view()[key]
        for key in ('quarterly', 'trade'): assert live['macro'][key] == service.view()['macro'][key]
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'country': 'CAN', 'checks': sum(counts.values()), 'checks_by_family': counts,
              'failures': failures, 'live_source_refresh_verified': live is not None, 'calculated_value_tolerance': '.0000011 CAD billions or percentage/index points',
              'accounting_validation': service.view()['validation'], 'latest_quarter': service.view()['latest']['period'], 'sources': service.view()['sources'], 'inputs': inputs}
    destination = ROOT / 'data' / f'canada-bop-audit-{started.astimezone(timezone(timedelta(hours=8))).date()}.json'
    destination.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('checks', 'checks_by_family', 'failures', 'live_source_refresh_verified', 'latest_quarter')}, indent=2))
    print('Audit report:', destination)
    if failures: raise SystemExit('Canadian chart audit failed.')


if __name__ == '__main__': main()
