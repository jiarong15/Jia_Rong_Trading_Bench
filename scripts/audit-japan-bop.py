"""Independent Decimal audit of all seven Japan charts against downloaded sources.

Run refresh-japan-bop.py first. --live also verifies a fresh source check through
the running dashboard API. Calculations here do not call production transforms.
"""
import argparse
import csv
from datetime import datetime, timezone
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
from backend.bop_japan import JapanBOPService

D = lambda value: Decimal(str(value))
FIELDS = {
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    started = datetime.now(timezone.utc)
    checks, failures = 0, []

    def check(actual, expected, context, tolerance=Decimal('.0000011')):
        nonlocal checks
        checks += 1
        valid = actual is None if expected is None else actual is not None and abs(D(actual) - expected) <= tolerance
        if not valid:
            failures.append({'context': context, 'actual': actual, 'expected': str(expected)})

    raw, inputs = {}, []
    input_dir = ROOT.parent / '.runtime' / 'japan-bop-research' / 'live-inputs'
    for urlpath in input_dir.glob('*.url'):
        url = urlpath.read_text(encoding='utf-8')
        path = urlpath.with_suffix('.txt')
        text = path.read_text(encoding='utf-8')
        inputs.append({'url': url, 'sha256': sha256(path.read_bytes()).hexdigest()})
        if '/getDataCode?' in url:
            payload = json.loads(text, parse_float=Decimal)
            for series in payload['RESULTSET']:
                frequency = series['FREQUENCY']
                values = {}
                for period, value in zip(series['VALUES']['SURVEY_DATES'], series['VALUES']['VALUES']):
                    year, part = divmod(int(period), 100)
                    month = part if frequency == 'MONTHLY' else (part - 1) * 3 + 1
                    key = year * 12 + month - 1
                    values[key] = D(value) if value is not None else None
                raw[series['SERIES_CODE']] = values
        elif 'JPNNGDP' in url:
            raw['GDP'] = {int(row['observation_date'][:4]) * 4 + (int(row['observation_date'][5:7]) - 1) // 3: D(row['JPNNGDP'])
                          for row in csv.DictReader(io.StringIO(text)) if row['JPNNGDP'] not in ('', '.')}
        elif 'd41ma.csv' in url:
            for cells in csv.reader(io.StringIO(text)):
                if cells and re.fullmatch(r'\d{4}/\d{2}', cells[0].strip()) and D(cells[1]) > 0 and D(cells[2]) > 0:
                    key = int(cells[0][:4]) * 12 + int(cells[0][5:7]) - 1
                    for field, value in zip(('customs_exports', 'customs_imports'), cells[1:]):
                        raw.setdefault(field, {})[key] = D(value) / 1_000_000
    assert len(inputs) == 5 and all(code in raw for code in FIELDS.values()), 'Missing saved live inputs; run refresh-japan-bop.py first.'

    def total(code, months, divisor=10):
        values = [raw.get(code, {}).get(month) for month in months]
        return sum(values) / divisor if values and all(value is not None for value in values) else None

    def difference(left, right):
        return left - right if left is not None and right is not None else None

    service = JapanBOPService(ROOT / 'data' / 'bop-cache' / 'japan.json', enabled=False)
    # Verify every stored BOJ observation against the actual native-unit API response.
    for group in service.snapshot['groups'].values():
        for metadata in group['series']:
            code = metadata['id']
            if code not in raw:
                continue
            divisor = 10 if metadata['native_unit'] == '100 million Yen' else 1
            for row in group['rows']:
                month = int(row['date'][:4]) * 12 + int(row['date'][5:7]) - 1
                value = raw[code].get(month)
                check(row[metadata['field']], value / divisor if value is not None else None, 'source/' + code + '/' + row['date'])

    live_verified = False
    for frequency in ('quarterly', 'trailing4q'):
        if args.live:
            with urlopen('http://127.0.0.1:8000/api/fx/bop?country=JPN&frequency=' + frequency + '&refresh=' + ('true' if frequency == 'quarterly' else 'false'), timeout=90) as response:
                assert response.headers['Cache-Control'] == 'no-store'
                view = json.load(response)
            if frequency == 'quarterly':
                assert view['delivery'] == 'source_checked' and not view['macro']['warnings']
                assert all(datetime.fromisoformat(source['checked_at']) >= started for source in view['sources'])
                live_verified = True
        else:
            view = service.view(frequency)
        count = 4 if frequency == 'trailing4q' else 1
        for row in view['series']:
            quarter = int(row['date'][:4]) * 4 + (int(row['date'][5:7]) - 1) // 3
            months = list(range((quarter - count + 1) * 3, quarter * 3 + 3))
            for field in ('current_account', 'goods', 'services', 'primary_income', 'secondary_income'):
                check(row[field], total(FIELDS[field], months), frequency + '/' + field + '/' + row['date'])
            customs = difference(total('customs_exports', months, 1), total('customs_imports', months, 1))
            check(row['customs_goods'], customs, frequency + '/customs/' + row['date'])
            check(row['goods_gap'], difference(customs, total(FIELDS['goods'], months)), frequency + '/customs-gap/' + row['date'])
        for row in view['macro']['quarterly']:
            quarter = int(row['date'][:4]) * 4 + (int(row['date'][5:7]) - 1) // 3
            months = list(range((quarter - count + 1) * 3, quarter * 3 + 3))
            numerator = total(FIELDS['current_account_sa'], months)
            gdps = [raw['GDP'].get(index) for index in range(quarter - count + 1, quarter + 1)]
            ratio = numerator * 400 / sum(gdps) if numerator is not None and all(value is not None for value in gdps) and sum(gdps) > 0 else None
            check(row['current_account_gdp_pct'], ratio, frequency + '/CA-GDP/' + row['date'])
            for kind in ('direct', 'equity', 'debt', 'other'):
                expected = difference(total(FIELDS[kind + '_liabilities'], months), total(FIELDS[kind + '_assets'], months))
                check(row['net_' + kind], expected, frequency + '/net-' + kind + '/' + row['date'])
            check(row['reserve_transactions'], total(FIELDS['reserve_transactions'], months), frequency + '/reserve-flow/' + row['date'])
            stock = total('BPBP6PA47', [quarter * 3], 1)
            debts = [total(code, [quarter * 3], 1) for code in ('BPBP6GEDC1', 'BPBP6GEDB1', 'BPBP6GEDP1', 'BPBP6GEDO1')]
            expected = stock * 100 / sum(debts) if stock is not None and all(value is not None for value in debts) and sum(debts) > 0 else None
            check(row['reserves_debt_pct'], expected, frequency + '/reserve-coverage/' + row['date'])
            currency = total('FX180110001', months, len(months))
            check(row['currency_index'], currency, frequency + '/yen-index/' + row['date'])
        trade = {}
        for quarter in range(1996 * 4, 2100 * 4):
            months = list(range((quarter - count + 1) * 3, quarter * 3 + 3))
            trade[quarter] = {}
            for field in ('exports', 'imports'):
                goods, services = total(FIELDS['goods_' + field + '_sa'], months), total(FIELDS['services_' + field + '_sa'], months)
                trade[quarter][field] = goods + services if goods is not None and services is not None else None
        for row in view['macro']['trade']:
            quarter = int(row['date'][:4]) * 4 + (int(row['date'][5:7]) - 1) // 3
            for field in ('exports', 'imports'):
                value = trade[quarter][field]
                check(row[field], value, frequency + '/trade-' + field + '/' + row['date'])
                previous = trade.get(quarter - 4, {}).get(field)
                expected = 100 * (value / previous - 1) if value is not None and previous is not None and previous > 0 else None
                check(row[field + '_yoy'], expected, frequency + '/YoY-' + field + '/' + row['date'])
                window = [trade.get(index, {}).get(field) for index in range(quarter - 2, quarter + 1)]
                expected = sum(window) / 3 if all(value is not None for value in window) else None
                check(row[field + '_ma3'], expected, frequency + '/MA3-' + field + '/' + row['date'])
            check(row['trade_balance'], difference(trade[quarter]['exports'], trade[quarter]['imports']), frequency + '/trade-balance/' + row['date'])
    report = {'checked_at': started.isoformat(), 'checks': checks, 'failures': failures,
              'live_refresh_verified': live_verified, 'source_inputs': inputs}
    destination = ROOT / 'data' / ('japan-bop-audit-' + started.date().isoformat() + '.json')
    destination.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'checks': checks, 'failures': len(failures), 'live_refresh_verified': live_verified, 'report': str(destination)}))
    if failures:
        print(json.dumps(failures[:10], indent=2))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
