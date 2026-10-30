"""Independent Decimal audit: SNB CSV, SECO native GDP and BIS CHF data.

The application uses SNB JSON; expected audit values use its separate CSV
representation. --live additionally verifies a new official-source refresh.
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
from backend.bop_switzerland import SwitzerlandBOPService
D = lambda value: Decimal(str(value))
CORE = {'current_account': 'S0', 'goods': 'S2', 'services': 'S3', 'primary_income': 'S4', 'secondary_income': 'S7',
        'income_receipts': 'E4', 'income_payments': 'A4', 'capital_account': 'S8', 'statistical_discrepancy': 'SD'}


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--live', action='store_true'); args = parser.parse_args()
    started = datetime.now(timezone.utc)
    service = SwitzerlandBOPService(ROOT / 'data' / 'bop-cache' / 'switzerland.json', enabled=False)
    raw, inputs = {}, []
    for path in (ROOT.parent / '.runtime' / 'ch-bop-research' / 'live-inputs').glob('*.url'):
        url = path.read_text(encoding='utf-8'); body = path.with_suffix('.bin').read_bytes()
        inputs.append({'url': url, 'sha256': sha256(body).hexdigest()})
        if 'data.snb.ch/api/cube/' in url and '/data/csv/' in url:
            rows = list(csv.reader(io.StringIO(body.decode('utf-8-sig'), newline=''), delimiter=';'))
            cube = rows[0][1]; assert rows[0][0] == 'CubeId' and cube == url.split('/cube/')[1].split('/')[0]
            start = next(i for i, row in enumerate(rows) if row and row[0] == 'Date'); header = rows[start]
            for row in rows[start + 1:]:
                if not row: continue
                assert len(row) == len(header)
                period, coords, value = row[0], ','.join(row[1:-1]), row[-1]
                if int(period[:4]) < 1997: continue
                assert re.fullmatch(r'\d{4}-(Q[1-4]|0[1-9]|1[0-2])', period)
                index = int(period[:4]) * 4 + int(period[-1]) - 1 if '-Q' in period else int(period[:4]) * 12 + int(period[5:]) - 1
                key = 'EPB@SNB.' + cube + '{' + coords + '}'; values = raw.setdefault(key, {})
                assert index not in values
                values[index] = D(value) / (1 if cube == 'sddsexd36912q' else 1000) if value else None
        elif url.endswith('ch-seco-gdp.csv'):
            for row in csv.DictReader(io.StringIO(body.decode('utf-8-sig'))):
                if (row['structure'], row['type'], row['seas_adj']) != ('gdp', 'nom', 'na') or int(row['date'][:4]) < 1997: continue
                index = int(row['date'][:4]) * 4 + (int(row['date'][5:7]) - 1) // 3
                values = raw.setdefault('SECO.gdp.nom.na', {}); assert index not in values
                values[index] = D(row['value']) / 1000 if row['value'] else None
        elif '/WS_EER/M.N.B.CH?' in url:
            for row in csv.DictReader(io.StringIO(body.decode('utf-8-sig'))):
                assert (row['REF_AREA'], row['FREQ'], row['EER_TYPE'], row['EER_BASKET']) == ('CH', 'M', 'N', 'B')
                if int(row['TIME_PERIOD'][:4]) < 1997: continue
                raw.setdefault('M.N.B.CH', {})[int(row['TIME_PERIOD'][:4]) * 12 + int(row['TIME_PERIOD'][5:]) - 1] = D(row['OBS_VALUE']) if row['OBS_VALUE'] else None
    needed = {meta['id'] for group in service.snapshot['groups'].values() for meta in group['series']}
    assert needed.issubset(raw) and len(inputs) == 15, 'Saved official inputs are incomplete.'
    counts, failures = {}, []
    def check(actual, expected, context):
        family = context.split('/')[0]; counts[family] = counts.get(family, 0) + 1
        valid = actual is None if expected is None else actual is not None and abs(D(actual) - expected) <= D('.0000011')
        if not valid: failures.append({'context': context, 'actual': actual, 'expected': str(expected)})
    def qindex(at): return int(at[:4]) * 4 + (int(at[5:7]) - 1) // 3
    def mindex(at): return int(at[:4]) * 12 + int(at[5:7]) - 1
    def key(cube, code): return 'EPB@SNB.' + cube + '{' + code + '}'
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
    # Independently confirm the SECO CSV scale against its official workbook.
    workbook_path = ROOT.parent / '.runtime' / 'ch-bop-research' / 'seco-gdp-reference.xlsx'
    if workbook_path.exists():
        try: import openpyxl
        except ImportError:
            sys.path.insert(0, str(ROOT.parent / '.runtime' / 'python')); import openpyxl
        workbook = openpyxl.load_workbook(workbook_path, read_only=True, data_only=True)
        sheet = workbook['nom_q']; sheet.reset_dimensions(); cells = list(sheet.values)
        assert 'In Mio. Swiss Francs, at current prices' in cells[1][0] and cells[3][2] == 'Gross domestic product'
        for row in cells:
            if isinstance(row[0], int) and 1997 <= row[0] <= started.year and row[1] in (1, 2, 3, 4):
                q = row[0] * 4 + row[1] - 1
                check(float(raw['SECO.gdp.nom.na'][q]), D(row[2]) / 1000, f'gdp_workbook/{row[0]}Q{row[1]}')
        workbook.close()
    for frequency, periods in [('quarterly', 1), ('trailing4q', 4)]:
        view = service.view(frequency)
        for row in view['series']:
            q = qindex(row['date'])
            for field, code in CORE.items(): check(row[field], total(key('bopoverq', code), q, periods), f'core/{frequency}/{field}/{row["date"]}')
            exports, imports = total(key('bopcurrq', 'T1,E'), q, periods), total(key('bopcurrq', 'T1,A'), q, periods)
            customs = minus(exports, imports)
            for field, expected in [('customs_exports', exports), ('customs_imports', imports), ('customs_goods', customs), ('goods_gap', minus(customs, total(key('bopoverq', 'S2'), q, periods)))]:
                check(row[field], expected, f'core/{frequency}/{field}/{row["date"]}')
            financial = plus(total(key('bopoverq', 'S9'), q, periods), total(key('bopoverq', 'S14'), q, periods))
            check(row['financial_account'], financial, f'core/{frequency}/full_financial_account/{row["date"]}')
            check(row['net_financial_inflow'], -financial if financial is not None else None, f'core/{frequency}/net_inflow/{row["date"]}')
            check(row['net_lending'], plus(total(key('bopoverq', 'S0'), q, periods), total(key('bopoverq', 'S8'), q, periods)), f'core/{frequency}/net_lending/{row["date"]}')
        for row in view['macro']['quarterly']:
            q = qindex(row['date']); ca, gdp = total(key('bopoverq', 'S0'), q, periods), total('SECO.gdp.nom.na', q, periods)
            check(row['current_account_gdp_pct'], ca / gdp * 100 if ca is not None and gdp else None, f'macro/{frequency}/CA_GDP/{row["date"]}')
            for category, code in [('direct', 'T1'), ('equity', 'T3'), ('debt', 'T25'), ('other', 'T4')]:
                expected = minus(total(key('bopcapbalq', 'NP,' + code), q, periods), total(key('bopcapbalq', 'NA,' + code), q, periods))
                check(row['net_' + category], expected, f'macro/{frequency}/net_{category}/{row["date"]}')
            check(row['reserve_transactions'], total(key('bopcapbalq', 'NA,T7'), q, periods), f'macro/{frequency}/reserve_flow/{row["date"]}')
            stock = raw[key('auvekomq', 'A,T7')].get(q)
            parts = [raw[key('sddsexd36912q', code)].get(q) for code in ('DG_GRS_BP6_XDC', 'DCB_GRS_BP6_XDC', 'DODC_GRS_BP6_XDC', 'DODS_GRS_BP6_XDC')]
            debt = sum(parts) if all(value is not None for value in parts) else None
            for field, expected in [('reserve_stock', stock), ('short_term_external_debt', debt), ('reserves_debt_pct', stock / debt * 100 if stock is not None and debt else None)]:
                check(row[field], expected, f'macro/{frequency}/{field}/{row["date"]}')
            index = total('M.N.B.CH', q * 3 + 2, periods * 3)
            check(row['currency_index'], index / (periods * 3) if index is not None else None, f'macro/{frequency}/CHF_index/{row["date"]}')
        for row in view['macro']['trade']:
            q = qindex(row['date']); levels = {}
            for field, code in [('exports', 'A,GT,WMF'), ('imports', 'E,GT,WMF')]:
                current = total(key('ausshawarm', code), q * 3 + 2, periods * 3)
                previous = total(key('ausshawarm', code), (q - 4) * 3 + 2, periods * 3); levels[field] = current
                check(row[field], current, f'trade/{frequency}/{field}/{row["date"]}')
                values = [total(key('ausshawarm', code), (q - lag) * 3 + 2, periods * 3) for lag in range(3)]
                check(row[field + '_ma3'], sum(values) / 3 if all(value is not None for value in values) else None, f'trade/{frequency}/{field}_MA3/{row["date"]}')
                check(row[field + '_yoy'], (current / previous - 1) * 100 if current is not None and previous else None, f'trade/{frequency}/{field}_YoY/{row["date"]}')
            check(row['trade_balance'], minus(levels['exports'], levels['imports']), f'trade/{frequency}/balance/{row["date"]}')
    live = None
    if args.live:
        with urlopen('http://127.0.0.1:8000/api/fx/bop?country=CHE&refresh=true', timeout=150) as response:
            assert response.headers['Cache-Control'] == 'no-store'; live = json.load(response)
        assert live['delivery'] == 'source_checked' and live['refresh_available'] and not live['macro']['warnings']
        assert all(datetime.fromisoformat(source['checked_at']) >= started for source in live['sources'])
        assert live['series'] == service.view()['series'] and live['macro']['quarterly'] == service.view()['macro']['quarterly'] and live['macro']['trade'] == service.view()['macro']['trade']
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'country': 'CHE', 'checks': sum(counts.values()),
              'checks_by_family': counts, 'failures': failures, 'live_source_refresh_verified': live is not None,
              'calculated_value_tolerance': '.0000011 CHF billions or percentage/index points',
              'accounting_validation': service.view()['validation'], 'gdp_workbook_scale_verified': 'gdp_workbook' in counts,
              'latest_quarter': service.view()['latest']['period'], 'sources': service.view()['sources'], 'inputs': inputs}
    destination = ROOT / 'data' / f'switzerland-bop-audit-{started.astimezone(timezone(timedelta(hours=8))).date()}.json'
    destination.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('checks', 'checks_by_family', 'failures', 'live_source_refresh_verified', 'latest_quarter')}, indent=2))
    print('Audit report:', destination)
    if failures: raise SystemExit('Swiss chart audit failed.')


if __name__ == '__main__': main()
