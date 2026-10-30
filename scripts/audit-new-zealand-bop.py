"""Independent Decimal audit against native Stats NZ and BIS downloads.

Run refresh-new-zealand-bop.py first. --live verifies a fresh source pull through
the running dashboard. Expected chart values do not use production transforms.
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
from backend.bop_new_zealand import NewZealandBOPService
D = lambda value: Decimal(str(value))
CHART_CODES = {
    'current_account': 'BOPQ.S06AC100000000D', 'goods': 'BOPQ.S06AC1000000D11', 'services': 'BOPQ.S06AC1000000D12',
    'primary_income': 'BOPQ.S06AC1000000D21', 'secondary_income': 'BOPQ.S06AC1000000D22',
    'income_receipts': 'BOPQ.S06AC1000000A21', 'income_payments': 'BOPQ.S06AD1000000A21',
    'capital_account': 'BOPQ.S06AC000000000E', 'statistical_discrepancy': 'BOPQ.S06AC000000000G',
    'financial_inflow': 'BOPQ.S06AC100000000F', 'reserve_transactions': 'BOPQ.S06AD10000000C5',
    'reserve_stock': 'IIPQ.S06AA00000000P5',
    'direct_assets': 'BOPQ.S06AD10000000C1', 'direct_liabilities': 'BOPQ.S06AC10000000C1',
    'equity_assets': 'BOPQ.S06AD0000000C21', 'equity_liabilities': 'BOPQ.S06AC0000000C21',
    'debt_assets': 'BOPQ.S06AD0000000C22', 'debt_liabilities': 'BOPQ.S06AC0000000C22',
    'other_assets': 'BOPQ.S06AD10000000C4', 'other_liabilities': 'BOPQ.S06AC10000000C4',
    'debt_government': 'IIPQ.S06AL00004X3001', 'debt_central_bank': 'IIPQ.S06AL00004X1001',
    'debt_banks': 'IIPQ.S06AL00004X2001', 'debt_other': 'IIPQ.S06AL00004X4441',
}


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--live', action='store_true'); args = parser.parse_args()
    started = datetime.now(timezone.utc)
    service = NewZealandBOPService(ROOT / 'data' / 'bop-cache' / 'new-zealand.json', enabled=False)
    raw, inputs = {}, []
    needed = {meta['id'] for group in service.snapshot['groups'].values() for meta in group['series']}
    for path in (ROOT.parent / '.runtime' / 'nz-bop-research' / 'live-inputs').glob('*.url'):
        url = path.read_text(encoding='utf-8'); body = path.with_suffix('.bin').read_bytes()
        inputs.append({'url': url, 'sha256': sha256(body).hexdigest()}); text = body.decode('utf-8-sig')
        if '/assets/' in url and url.endswith('.csv'):
            for row in csv.DictReader(io.StringIO(text, newline='')):
                code = row['Series_reference']
                if code not in needed: continue
                year, month = int(row['Period'][:4]), int(row['Period'][5:])
                if year < 1997: continue
                assert month in (3, 6, 9, 12)
                values = raw.setdefault(code, {}); index = year * 4 + month // 3 - 1
                assert index not in values
                assert row['UNITS'] in ('Dollars', 'Percent') and row.get('MAGNITUDE', row.get('MAGNTUDE')) == ('6' if row['UNITS'] == 'Dollars' else '0')
                value = row['Data_value']
                values[index] = None if value == '' or row['STATUS'] in ('C', 'S') or row.get('Suppressed') == 'Y' else D(value) / (1000 if row['UNITS'] == 'Dollars' else 1)
        elif '/ExportDirect.aspx' in url:
            # Independently read the year x twelve-month matrix, including
            # paired status cells. These native values are NZD units.
            for section in text.replace('\r\n', '\n').replace('\r', '\n').split('Series is ')[1:]:
                lines = list(csv.reader(io.StringIO(section, newline=''))); code = lines[0][0]
                assert code in ('EXPM.SCT99F', 'IMPM.SCT99C') and code not in raw
                values = raw.setdefault(code, {})
                for row in lines:
                    if not row or not re.fullmatch(r'\d{4}', row[0]): continue
                    for month in range(1, 13):
                        value, status = row[month * 2 - 1:month * 2 + 1]
                        values[int(row[0]) * 12 + month - 1] = None if value in ('', '..', 'C', 'S') or status in ('C', 'S') else D(value) / 1_000_000_000
        elif '/WS_EER/M.N.B.NZ?' in url:
            for row in csv.DictReader(io.StringIO(text)):
                assert (row['REF_AREA'], row['FREQ'], row['EER_TYPE'], row['EER_BASKET']) == ('NZ', 'M', 'N', 'B')
                if int(row['TIME_PERIOD'][:4]) < 1997: continue
                raw.setdefault('M.N.B.NZ', {})[int(row['TIME_PERIOD'][:4]) * 12 + int(row['TIME_PERIOD'][5:]) - 1] = D(row['OBS_VALUE']) if row['OBS_VALUE'] else None
    assert needed.issubset(raw) and len(inputs) == 5, 'Official input files are incomplete.'
    counts, failures, ratio_comparisons = {}, [], []
    def check(actual, expected, context, tolerance=D('.0000011')):
        family = context.split('/')[0]; counts[family] = counts.get(family, 0) + 1
        valid = actual is None if expected is None else actual is not None and abs(D(actual) - expected) <= tolerance
        if not valid: failures.append({'context': context, 'actual': actual, 'expected': str(expected), 'tolerance': str(tolerance)})
    def qindex(at): return int(at[:4]) * 4 + (int(at[5:7]) - 1) // 3
    def mindex(at): return int(at[:4]) * 12 + int(at[5:7]) - 1
    def total(code, end, periods):
        values = [raw.get(code, {}).get(index) for index in range(end - periods + 1, end + 1)]
        return sum(values) if values and all(value is not None for value in values) else None
    def minus(a, b): return a - b if a is not None and b is not None else None
    def plus(a, b): return a + b if a is not None and b is not None else None
    codes = CHART_CODES
    for group in service.snapshot['groups'].values():
        for row in group['rows']:
            for meta in group['series']:
                index = qindex(row['date']) if meta['frequency'] == 'Quarterly' else mindex(row['date'])
                check(row[meta['field']], raw[meta['id']].get(index), f'source/{meta["id"]}/{row["date"]}')
    for frequency, periods in [('quarterly', 1), ('trailing4q', 4)]:
        view = service.view(frequency)
        for row in view['series']:
            q = qindex(row['date'])
            for field in ('current_account', 'goods', 'services', 'primary_income', 'secondary_income', 'income_receipts', 'income_payments', 'capital_account', 'statistical_discrepancy'):
                check(row[field], total(codes[field], q, periods), f'core/{frequency}/{field}/{row["date"]}')
            exports, imports = total('EXPM.SCT99F', q * 3 + 2, periods * 3), total('IMPM.SCT99C', q * 3 + 2, periods * 3)
            customs = minus(exports, imports)
            for field, expected in [('customs_exports', exports), ('customs_imports', imports), ('customs_goods', customs), ('goods_gap', minus(customs, total(codes['goods'], q, periods)))]:
                check(row[field], expected, f'core/{frequency}/{field}/{row["date"]}')
            inflow = total(codes['financial_inflow'], q, periods)
            check(row['financial_account'], -inflow if inflow is not None else None, f'core/{frequency}/financial_account/{row["date"]}')
            check(row['net_financial_inflow'], inflow, f'core/{frequency}/net_inflow/{row["date"]}')
            check(row['net_lending'], plus(total(codes['current_account'], q, periods), total(codes['capital_account'], q, periods)), f'core/{frequency}/net_lending/{row["date"]}')
        for row in view['macro']['quarterly']:
            q = qindex(row['date']); ca, gdp = total(codes['current_account'], q, periods), total('SNEQ.SG00NAC00B15', q, periods)
            check(row['current_account_gdp_pct'], ca / gdp * 100 if ca is not None and gdp else None, f'macro/{frequency}/CA_GDP/{row["date"]}')
            for category in ('direct', 'equity', 'debt', 'other'):
                check(row['net_' + category], minus(total(codes[category + '_liabilities'], q, periods), total(codes[category + '_assets'], q, periods)), f'macro/{frequency}/net_{category}/{row["date"]}')
            check(row['reserve_transactions'], total(codes['reserve_transactions'], q, periods), f'macro/{frequency}/reserve_flow/{row["date"]}')
            stock = raw[codes['reserve_stock']].get(q)
            parts = [raw[codes[field]].get(q) for field in ('debt_government', 'debt_central_bank', 'debt_banks', 'debt_other')]
            debt = sum(parts) if all(value is not None for value in parts) else None
            for field, expected in [('reserve_stock', stock), ('short_term_external_debt', debt), ('reserves_debt_pct', stock / debt * 100 if stock is not None and debt else None)]:
                check(row[field], expected, f'macro/{frequency}/{field}/{row["date"]}')
            index = total('M.N.B.NZ', q * 3 + 2, periods * 3)
            check(row['currency_index'], index / (periods * 3) if index is not None else None, f'macro/{frequency}/NZD_index/{row["date"]}')
            if frequency == 'trailing4q':
                published = raw['BOPQ.S06AR00CABTOGDP'].get(q)
                if published is not None and ca is not None and gdp:
                    # A BOP-published rounded ratio is a different vintage from
                    # the separately refreshed GDP release. Record discrepancies
                    # explicitly; never alter chart inputs or relax exact tests.
                    difference = abs(D(row['current_account_gdp_pct']) - published)
                    ratio_comparisons.append({'date': row['date'], 'calculated_current_vintage': row['current_account_gdp_pct'],
                                              'published_bop_vintage': str(published), 'absolute_difference_percentage_points': str(difference),
                                              'within_one_decimal_rounding': difference <= D('.051')})
        for row in view['macro']['trade']:
            q = qindex(row['date']); levels = {}
            for field, code in [('exports', 'EXPM.SCT99F'), ('imports', 'IMPM.SCT99C')]:
                current = total(code, q * 3 + 2, periods * 3); previous = total(code, (q - 4) * 3 + 2, periods * 3); levels[field] = current
                check(row[field], current, f'trade/{frequency}/{field}/{row["date"]}')
                values = [total(code, (q - lag) * 3 + 2, periods * 3) for lag in range(3)]
                check(row[field + '_ma3'], sum(values) / 3 if all(value is not None for value in values) else None, f'trade/{frequency}/{field}_MA3/{row["date"]}')
                check(row[field + '_yoy'], (current / previous - 1) * 100 if current is not None and previous else None, f'trade/{frequency}/{field}_YoY/{row["date"]}')
            check(row['trade_balance'], minus(levels['exports'], levels['imports']), f'trade/{frequency}/balance/{row["date"]}')
    live = None
    if args.live:
        with urlopen('http://127.0.0.1:8000/api/fx/bop?country=NZL&refresh=true', timeout=150) as response:
            assert response.headers['Cache-Control'] == 'no-store'; live = json.load(response)
        assert live['delivery'] == 'source_checked' and live['refresh_available'] and not live['macro']['warnings']
        assert all(datetime.fromisoformat(source['checked_at']) >= started for source in live['sources'])
        assert live['series'] == service.view()['series'] and live['macro']['quarterly'] == service.view()['macro']['quarterly'] and live['macro']['trade'] == service.view()['macro']['trade']
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'country': 'NZL', 'checks': sum(counts.values()),
              'checks_by_family': counts, 'failures': failures, 'live_source_refresh_verified': live is not None,
              'tolerances': {'calculated_fields': '.0000011 NZD billions or index/percentage points', 'published_annual_ca_gdp': '.051 percentage points: native ratio rounded to one decimal and GDP revisions have independent vintages'},
              'published_ratio_comparisons': {'count': len(ratio_comparisons),
                  'differences_beyond_rounding': [item for item in ratio_comparisons if not item['within_one_decimal_rounding']],
                  'note': 'Supplementary comparison of different source vintages, excluded from exact chart-value checks. Current GDP and quarter-summed current accounts are independently checked above.'},
              'latest_quarter': service.view()['latest']['period'], 'sources': service.view()['sources'], 'inputs': inputs}
    destination = ROOT / 'data' / f'new-zealand-bop-audit-{started.astimezone(timezone(timedelta(hours=8))).date()}.json'
    destination.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('checks', 'checks_by_family', 'failures', 'live_source_refresh_verified', 'latest_quarter')}, indent=2))
    print('Audit report:', destination)
    if failures: raise SystemExit('New Zealand chart audit failed.')


if __name__ == '__main__': main()
