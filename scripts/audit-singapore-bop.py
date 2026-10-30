"""Independent Decimal audit of Singapore chart values against native downloads.

Run refresh-singapore-bop.py first. --live verifies a new official-source check
through the running local API. Expected values do not use production transforms.
"""
import argparse
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import io
import json
from pathlib import Path
import re
import sys
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.bop_singapore import SingaporeBOPService

D = lambda value: Decimal(str(value))
CORE = {'current_account': '1.1', 'goods': '1.1.1', 'services': '1.1.2',
        'primary_income': '1.1.3', 'secondary_income': '1.1.4',
        'income_receipts': '1.1.3.1', 'income_payments': '1.1.3.2', 'statistical_discrepancy': '1.3'}


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--live', action='store_true'); args = parser.parse_args()
    started = datetime.now(timezone.utc)
    service = SingaporeBOPService(ROOT / 'data' / 'bop-cache' / 'singapore.json', enabled=False)
    raw, inputs = {}, []
    for path in (ROOT.parent / '.runtime' / 'sg-bop-research' / 'live-inputs').glob('*.url'):
        url = path.read_text(encoding='utf-8'); text = path.with_suffix('.txt').read_text(encoding='utf-8')
        inputs.append({'url': url, 'sha256': sha256(path.with_suffix('.txt').read_bytes()).hexdigest()})
        if '/tabledata/' in url:
            data = json.loads(text)['Data']; table = data['id']
            for row in data['row']:
                for cell in row['columns']:
                    year, part = cell['key'].split()
                    if int(year) < 1997: continue
                    if data['frequency'] == 'Quarterly': index = int(year) * 4 + int(part[0]) - 1
                    else: index = int(year) * 12 + 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split().index(part)
                    key = table + ':' + row['seriesNo']; values = raw.setdefault(key, {})
                    assert index not in values, 'Repeated or overlapping native SingStat pages.'
                    value = cell['value']
                    divisor = 1_000_000 if row['uoM'] == 'Thousand Dollars' else 1000
                    values[index] = None if value in ('na', '', None) else D(0 if value == '-' else value) / divisor
        elif '/WS_EER/M.N.B.SG?' in url:
            for row in csv.DictReader(io.StringIO(text)):
                year, month = int(row['TIME_PERIOD'][:4]), int(row['TIME_PERIOD'][5:7])
                if year < 1997: continue
                raw.setdefault('M.N.B.SG', {})[year * 12 + month - 1] = D(row['OBS_VALUE']) if row['OBS_VALUE'] else None
    assert len(inputs) == 14 and all(key in raw for key in ['M060171:1.1', 'M015651:1', 'M700031:1', 'M060221:1.2.3.1', 'M451001:2', 'M451001:3', 'M.N.B.SG'])
    counts, failures = {}, []

    def check(actual, expected, context):
        family = context.split('/')[0]; counts[family] = counts.get(family, 0) + 1
        valid = actual is None if expected is None else actual is not None and abs(D(actual) - expected) <= D('.0000011')
        if not valid: failures.append({'context': context, 'actual': actual, 'expected': str(expected)})

    def qindex(at): return int(at[:4]) * 4 + (int(at[5:7]) - 1) // 3
    def mindex(at): return int(at[:4]) * 12 + int(at[5:7]) - 1

    def total(key, end, periods):
        values = [raw.get(key, {}).get(index) for index in range(end - periods + 1, end + 1)]
        return sum(values) if values and all(value is not None for value in values) else None

    def minus(a, b): return a - b if a is not None and b is not None else None

    for group in service.snapshot['groups'].values():
        for row in group['rows']:
            for meta in group['series']:
                index = qindex(row['date']) if meta['frequency'] == 'Quarterly' else mindex(row['date'])
                check(row[meta['field']], raw[meta['id']].get(index), f'source/{meta["id"]}/{row["date"]}')

    for frequency, periods in [('quarterly', 1), ('trailing4q', 4)]:
        view = service.view(frequency)
        for row in view['series']:
            q = qindex(row['date'])
            for field, code in CORE.items(): check(row[field], total('M060171:' + code, q, periods), f'core/{frequency}/{field}/{row["date"]}')
            exports, imports = total('M451001:3', q * 3 + 2, periods * 3), total('M451001:2', q * 3 + 2, periods * 3)
            goods = minus(exports, imports)
            for field, value in [('customs_exports', exports), ('customs_imports', imports), ('customs_goods', goods), ('goods_gap', minus(goods, total('M060171:1.1.1', q, periods)))]:
                check(row[field], value, f'core/{frequency}/{field}/{row["date"]}')
            for field in ('capital_account', 'financial_account', 'net_lending', 'net_financial_inflow'):
                check(row[field], None, f'core/{frequency}/unavailable_{field}/{row["date"]}')
        for row in view['macro']['quarterly']:
            q = qindex(row['date']); ca, gdp = total('M060171:1.1', q, periods), total('M015651:1', q, periods)
            check(row['current_account_gdp_pct'], 100 * ca / gdp if ca is not None and gdp else None, f'macro/{frequency}/CA_GDP/{row["date"]}')
            for field, asset, liability in [('net_direct', '1.2.2.1.1', '1.2.2.1.2'), ('net_portfolio', '1.2.2.2.1', '1.2.2.2.2'), ('net_other', '1.2.2.4.1', '1.2.2.4.2')]:
                check(row[field], minus(total('M060171:' + liability, q, periods), total('M060171:' + asset, q, periods)), f'macro/{frequency}/{field}/{row["date"]}')
            for field in ('net_equity', 'net_debt'): check(row[field], None, f'macro/{frequency}/{field}/{row["date"]}')
            check(row['reserve_transactions'], total('M060171:2', q, periods), f'macro/{frequency}/reserve_flow/{row["date"]}')
            stock = raw['M700031:1'].get(q * 3 + 2)
            parts = [raw.get('M060221:' + code, {}).get(q) for code in ('1.2.2.1', '1.2.3.1', '1.2.4.1')]
            debt = sum(parts) if raw['M060221:1.2.1'].get(q) == 0 and all(value is not None for value in parts) else None
            check(row['reserve_stock'], stock, f'macro/{frequency}/stock/{row["date"]}')
            check(row['short_term_external_debt'], debt, f'macro/{frequency}/debt/{row["date"]}')
            check(row['reserves_debt_pct'], stock / debt * 100 if stock is not None and debt else None, f'macro/{frequency}/coverage/{row["date"]}')
            indices = total('M.N.B.SG', q * 3 + 2, periods * 3)
            check(row['currency_index'], indices / (periods * 3) if indices is not None else None, f'macro/{frequency}/SGD_index/{row["date"]}')
        for row in view['macro']['trade']:
            q = qindex(row['date']); level = {}
            for field, code in [('exports', '3'), ('imports', '2')]:
                current = total('M451001:' + code, q * 3 + 2, periods * 3)
                previous = total('M451001:' + code, (q - 4) * 3 + 2, periods * 3)
                level[field] = current
                check(row[field], current, f'trade/{frequency}/{field}/{row["date"]}')
                values = [total('M451001:' + code, (q - lag) * 3 + 2, periods * 3) for lag in range(3)]
                check(row[field + '_ma3'], sum(values) / 3 if all(value is not None for value in values) else None, f'trade/{frequency}/{field}_MA3/{row["date"]}')
                check(row[field + '_yoy'], (current / previous - 1) * 100 if current is not None and previous else None, f'trade/{frequency}/{field}_YoY/{row["date"]}')
            check(row['trade_balance'], minus(level['exports'], level['imports']), f'trade/{frequency}/balance/{row["date"]}')
    live = None
    if args.live:
        with urlopen('http://127.0.0.1:8000/api/fx/bop?country=SGP&refresh=true', timeout=120) as response:
            assert response.headers['Cache-Control'] == 'no-store'
            live = json.load(response)
        assert live['delivery'] == 'source_checked' and live['refresh_available'] and not live['macro']['warnings']
        assert all(datetime.fromisoformat(source['checked_at']) >= started for source in live['sources'])
        assert live['series'] == service.view()['series'] and live['macro']['quarterly'] == service.view()['macro']['quarterly']
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'country': 'SGP', 'checks': sum(counts.values()),
              'checks_by_family': counts, 'failures': failures, 'live_source_refresh_verified': live is not None,
              'latest_quarter': service.view()['latest']['period'], 'sources': service.view()['sources'], 'inputs': inputs}
    destination = ROOT / 'data' / f'singapore-bop-audit-{started.astimezone(timezone(timedelta(hours=8))).date()}.json'
    destination.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('checks', 'checks_by_family', 'failures', 'live_source_refresh_verified', 'latest_quarter')}, indent=2))
    print('Audit report:', destination)
    if failures: raise SystemExit('Singapore chart audit failed.')


if __name__ == '__main__': main()
