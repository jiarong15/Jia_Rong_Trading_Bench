"""Independent openpyxl/Decimal audit of every Australian BOP chart field.

Run refresh-australia-bop.py first. --live verifies an actual new source check
through the running dashboard. The app itself has no openpyxl dependency.
"""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    import openpyxl
except ImportError:
    sys.path.insert(0, str(ROOT.parent / '.runtime' / 'python'))
    import openpyxl
from backend.bop_australia import AustraliaBOPService

D = lambda value: Decimal(str(value))
CORE = {'current_account': 'A3535187L', 'goods': 'A3535205F', 'services': 'A3535211A',
        'primary_income': 'A3535270A', 'secondary_income': 'A3535267L', 'income_receipts': 'A3535271C',
        'income_payments': 'A3535272F', 'capital_account': 'A3535359W', 'statistical_discrepancy': 'A3534001K',
        'bop_goods_nsa': 'A3533926V'}
NEGATIVE = {'A3535272F', 'A2718603V', 'A3532220X', 'A3532221A', 'A3532222C', 'A3532223F', 'A3532224J', 'A3532225K'}


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--live', action='store_true'); args = parser.parse_args()
    started = datetime.now(timezone.utc)
    service = AustraliaBOPService(ROOT / 'data' / 'bop-cache' / 'australia.json', enabled=False)
    raw, inputs = {}, []
    needed = {meta['id'] for group in service.snapshot['groups'].values() for meta in group['series']}
    for path in (ROOT.parent / '.runtime' / 'aus-bop-research' / 'live-inputs').glob('*.url'):
        url = path.read_text(encoding='utf-8'); body = path.with_suffix('.bin').read_bytes()
        inputs.append({'url': url, 'sha256': sha256(body).hexdigest()})
        if url.endswith('.xlsx'):
            workbook = openpyxl.load_workbook(io.BytesIO(body), read_only=True, data_only=True)
            for sheet in workbook:
                if not sheet.title.startswith('Data'): continue
                rows = list(sheet.iter_rows(values_only=True))
                columns = {code: rows[9].index(code) for code in needed if code in rows[9]}
                for code, column in columns.items():
                    values = raw.setdefault(code, {})
                    for row in rows[10:]:
                        at = row[0]
                        if at is None or at.year < 1997: continue
                        index = at.year * 4 + (at.month - 1) // 3 if rows[4][column] == 'Quarter' else at.year * 12 + at.month - 1
                        assert index not in values
                        value = row[column]
                        values[index] = None if value in (None, '', 'na', 'np', 'n.p.') else D(0 if value == '-' else value) / 1000
            workbook.close()
        elif '/WS_EER/M.N.B.AU?' in url:
            for row in csv.DictReader(io.StringIO(body.decode('utf-8-sig'))):
                year, month = int(row['TIME_PERIOD'][:4]), int(row['TIME_PERIOD'][5:])
                if year < 1997: continue
                assert (row['REF_AREA'], row['FREQ'], row['EER_TYPE'], row['EER_BASKET']) == ('AU', 'M', 'N', 'B')
                raw.setdefault('M.N.B.AU', {})[year * 12 + month - 1] = D(row['OBS_VALUE']) if row['OBS_VALUE'] else None
    assert needed.issubset(raw) and len(inputs) == 13, 'Saved official inputs are incomplete.'
    counts, failures = {}, []

    def check(actual, expected, context, tolerance=Decimal('.0000011')):
        family = context.split('/')[0]; counts[family] = counts.get(family, 0) + 1
        valid = actual is None if expected is None else actual is not None and abs(D(actual) - expected) <= tolerance
        if not valid: failures.append({'context': context, 'actual': actual, 'expected': str(expected), 'tolerance': str(tolerance)})

    def qindex(at): return int(at[:4]) * 4 + (int(at[5:7]) - 1) // 3
    def mindex(at): return int(at[:4]) * 12 + int(at[5:7]) - 1
    def total(code, end, periods):
        values = [raw.get(code, {}).get(index) for index in range(end - periods + 1, end + 1)]
        return sum(values) if values and all(value is not None for value in values) else None
    def minus(a, b): return a - b if a is not None and b is not None else None

    for group in service.snapshot['groups'].values():
        for row in group['rows']:
            for meta in group['series']:
                code = meta['id']; index = qindex(row['date']) if meta['frequency'] == 'Quarter' else mindex(row['date'])
                expected = raw[code].get(index)
                if code in NEGATIVE and expected is not None: expected = -expected
                check(row[meta['field']], expected, f'source/{code}/{row["date"]}')
    for frequency, periods in [('quarterly', 1), ('trailing4q', 4)]:
        view = service.view(frequency)
        for row in view['series']:
            q = qindex(row['date'])
            for field, code in CORE.items():
                expected = total(code, q, periods)
                if code in NEGATIVE and expected is not None: expected = -expected
                check(row[field], expected, f'core/{frequency}/{field}/{row["date"]}')
            exports, imports = total('A1827881R', q * 3 + 2, periods * 3), total('A1828721W', q * 3 + 2, periods * 3)
            customs = minus(exports, imports)
            for field, expected in [('customs_exports', exports), ('customs_imports', imports), ('customs_goods', customs), ('goods_gap', minus(customs, total('A3533926V', q, periods)))]:
                check(row[field], expected, f'core/{frequency}/{field}/{row["date"]}')
            financial_inflow = total('A3533747L', q, periods)
            check(row['financial_account'], -financial_inflow if financial_inflow is not None else None, f'core/{frequency}/financial_account/{row["date"]}')
            check(row['net_financial_inflow'], financial_inflow, f'core/{frequency}/net_financial_inflow/{row["date"]}')
            check(row['net_lending'], None, f'core/{frequency}/unmixed_net_lending/{row["date"]}')
        for row in view['macro']['quarterly']:
            q = qindex(row['date']); ca, gdp = total('A3535187L', q, periods), total('A2304418T', q, periods)
            check(row['current_account_gdp_pct'], ca / gdp * 100 if ca is not None and gdp else None, f'macro/{frequency}/CA_GDP/{row["date"]}')
            for field, asset, liability in [('net_direct', 'A3533874C', 'A3533954C'), ('net_equity', 'A3534582V', 'A3534585A'), ('net_debt', 'A3534149F', 'A3534077F'), ('net_other', 'A3533845T', 'A3533830A')]:
                a, b = total(asset, q, periods), total(liability, q, periods)
                components = a + b if a is not None and b is not None else None
                published = {'net_direct': 'A3533979X', 'net_other': 'A3533769A'}.get(field)
                if published:
                    check(row[field], total(published, q, periods), f'macro/{frequency}/{field}/{row["date"]}')
                    # Independently rounded million-AUD totals can differ from the
                    # sum of two rounded components by one million per quarter.
                    check(row[field], components, f'rounding/{frequency}/{field}/{row["date"]}', D('.0010001') * periods)
                else:
                    check(row[field], components, f'macro/{frequency}/{field}/{row["date"]}')
            reserve = total('A3533846V', q, periods)
            check(row['reserve_transactions'], -reserve if reserve is not None else None, f'macro/{frequency}/reserve_flow/{row["date"]}')
            native_stock = raw['A3532225K'].get(q); stock = -native_stock if native_stock is not None else None
            parts = [raw[code].get(q) for code in ('A3484160C', 'A3484161F', 'A3484152C', 'A3484180L', 'A3484172L')]
            debt = sum(parts) if all(value is not None for value in parts) else None
            check(row['reserve_stock'], stock, f'macro/{frequency}/stock/{row["date"]}')
            check(row['short_term_external_debt'], debt, f'macro/{frequency}/debt/{row["date"]}')
            check(row['reserves_debt_pct'], stock / debt * 100 if stock is not None and debt else None, f'macro/{frequency}/coverage/{row["date"]}')
            indices = total('M.N.B.AU', q * 3 + 2, periods * 3)
            check(row['currency_index'], indices / (periods * 3) if indices is not None else None, f'macro/{frequency}/AUD_index/{row["date"]}')
        for row in view['macro']['trade']:
            q = qindex(row['date']); levels = {}
            for field, code, factor in [('exports', 'A2718577A', 1), ('imports', 'A2718603V', -1)]:
                native = total(code, q * 3 + 2, periods * 3); previous = total(code, (q - 4) * 3 + 2, periods * 3)
                current = native * factor if native is not None else None; levels[field] = current
                check(row[field], current, f'trade/{frequency}/{field}/{row["date"]}')
                values = [total(code, (q - lag) * 3 + 2, periods * 3) for lag in range(3)]
                check(row[field + '_ma3'], sum(values) * factor / 3 if all(value is not None for value in values) else None, f'trade/{frequency}/{field}_MA3/{row["date"]}')
                check(row[field + '_yoy'], (native / previous - 1) * 100 if native is not None and previous else None, f'trade/{frequency}/{field}_YoY/{row["date"]}')
            check(row['trade_balance'], minus(levels['exports'], levels['imports']), f'trade/{frequency}/balance/{row["date"]}')
    live = None
    if args.live:
        with urlopen('http://127.0.0.1:8000/api/fx/bop?country=AUS&refresh=true', timeout=120) as response:
            assert response.headers['Cache-Control'] == 'no-store'; live = json.load(response)
        assert live['delivery'] == 'source_checked' and live['refresh_available'] and not live['macro']['warnings']
        assert all(datetime.fromisoformat(source['checked_at']) >= started for source in live['sources'])
        assert live['series'] == service.view()['series'] and live['macro']['quarterly'] == service.view()['macro']['quarterly']
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'country': 'AUS', 'checks': sum(counts.values()),
              'checks_by_family': counts, 'failures': failures, 'live_source_refresh_verified': live is not None,
              'tolerances_aud_bn': {'chart_values': '.0000011', 'published_net_vs_rounded_components': '.0010001 per quarter; .0040004 for Rolling 4Q'},
              'latest_quarter': service.view()['latest']['period'], 'sources': service.view()['sources'], 'inputs': inputs}
    destination = ROOT / 'data' / f'australia-bop-audit-{started.astimezone(timezone(timedelta(hours=8))).date()}.json'
    destination.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('checks', 'checks_by_family', 'failures', 'live_source_refresh_verified', 'latest_quarter')}, indent=2))
    print('Audit report:', destination)
    if failures: raise SystemExit('Australian chart audit failed.')


if __name__ == '__main__': main()
