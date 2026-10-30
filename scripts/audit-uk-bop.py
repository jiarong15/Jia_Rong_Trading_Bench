"""Independent Decimal checks of every UK chart against saved native releases.

Run refresh-uk-bop.py first. --live additionally forces and verifies an actual
source refresh through the running dashboard. No production parser or chart
calculation is used to derive expected values.
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
from backend.bop_uk import UKBOPService

D = lambda value: Decimal(str(value))
CORE = {'current_account': 'HBOP', 'goods': 'BOKI', 'services': 'IKBD', 'primary_income': 'HBOJ',
        'secondary_income': 'IKBP', 'capital_account': 'FNVQ', 'income_receipts': 'HBOH',
        'income_payments': 'HBOI', 'statistical_discrepancy': 'HHDH', 'bop_goods_nsa': 'LQCT'}


def qindex(at):
    return int(at[:4]) * 4 + (int(at[5:7]) - 1) // 3


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--live', action='store_true'); args = parser.parse_args()
    started = datetime.now(timezone.utc)
    service = UKBOPService(ROOT / 'data' / 'bop-cache' / 'uk.json', enabled=False)
    raw, monthly, currency, customs, inputs = {}, {}, {}, {}, []
    for path in (ROOT.parent / '.runtime' / 'uk-bop-research' / 'live-inputs').glob('*.url'):
        url = path.read_text(encoding='utf-8'); body = path.with_suffix('.txt').read_text(encoding='utf-8')
        inputs.append({'url': url, 'sha256': sha256(path.with_suffix('.txt').read_bytes()).hexdigest()})
        if 'ukea.csv' in url or 'mret.csv' in url:
            rows = list(csv.reader(io.StringIO(body))); codes = rows[1]
            if 'ukea.csv' in url:
                wanted = set(CORE.values()) | {'YBHA', 'HBNT', 'MU7M', 'HBWV', 'HBWX', 'HHYR', 'LTCV', 'LTEB', 'VTZY', 'ZAVI', 'ZAVB', 'ZAVF',
                                              'N2SV', 'N2SA', 'HBVI', 'XBLW', 'XBMW', 'XBLX', 'XBMM', 'XBMN',
                                              'HBOG', 'KTMS', 'HMBP', 'KTNF', 'FKMJ', 'HBNR', 'HBNS', 'ZPNN'}
                positions = {code: codes.index(code) for code in wanted}
                for row in rows[7:]:
                    match = re.fullmatch(r'(\d{4}) Q([1-4])', row[0])
                    if match:
                        q = int(match[1]) * 4 + int(match[2]) - 1
                        if q < 1997 * 4: continue
                        raw[q] = {code: D(row[index]) / 1000 if row[index] else None for code, index in positions.items()}
            else:
                for row in rows[7:]:
                    match = re.fullmatch(r'(\d{4}) ([A-Z]{3})', row[0])
                    if match:
                        month = int(match[1]) * 12 + 'JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC'.split().index(match[2])
                        if month < 1997 * 12: continue
                        monthly[month] = {field: D(row[codes.index(code)]) / 1000 if row[codes.index(code)] else None
                                          for field, code in [('exports', 'IKBH'), ('imports', 'IKBI'), ('trade_balance', 'IKBJ')]}
        elif 'SeriesCodes=XUQABK82' in url:
            for row in csv.DictReader(io.StringIO(body)):
                day, month, year = row['DATE'].split()
                q = int(year) * 4 + ['Mar', 'Jun', 'Sep', 'Dec'].index(month)
                currency[q] = D(row['XUQABK82'])
        elif 'api.uktradeinfo.com/OTS?' in url:
            from urllib.parse import unquote
            assert 'SuppressionIndex eq 0' in unquote(url), 'Customs query duplicates suppressed products.'
            payload = json.loads(body, parse_float=Decimal)
            assert not payload.get('@odata.nextLink')
            for row in payload['value']:
                month = row['MonthId'] // 100 * 12 + row['MonthId'] % 100 - 1
                customs.setdefault(month, {})[row['FlowTypeId']] = D(row['TotalValue']) / 1_000_000_000
    assert raw and monthly and currency and len(customs) >= 300 and len(inputs) >= 30, 'Saved live inputs are incomplete.'
    counts, failures = {}, []

    def check(actual, expected, context):
        family = context.split('/')[0]; counts[family] = counts.get(family, 0) + 1
        valid = actual is None if expected is None else actual is not None and abs(D(actual) - expected) <= D('.0000011')
        if not valid:
            failures.append({'context': context, 'actual': actual, 'expected': str(expected)})

    def total(q, code, periods):
        values = [raw.get(index, {}).get(code) for index in range(q - periods + 1, q + 1)]
        return sum(values) if all(value is not None for value in values) else None

    def custom_total(q, field, periods):
        values = []
        for month in range((q - periods + 1) * 3, (q + 1) * 3):
            flows = customs.get(month, {})
            if set(flows) != {1, 2, 3, 4}: return None
            exports, imports = flows[2] + flows[4], flows[1] + flows[3]
            values.append(exports if field == 'customs_exports' else imports if field == 'customs_imports' else exports - imports)
        return sum(values)

    def trade_total(q, field, periods):
        values = [monthly.get(month, {}).get(field) for month in range((q - periods + 1) * 3, (q + 1) * 3)]
        return sum(values) if all(value is not None for value in values) else None

    # Native-source conversion is checked separately from derived chart values.
    for name, group in service.snapshot['groups'].items():
        for row in group['rows']:
            q = qindex(row['date']); month = int(row['date'][:4]) * 12 + int(row['date'][5:7]) - 1
            if name == 'accounts':
                for meta in group['series']:
                    if meta['id'] in raw[q]: check(row[meta['field']], raw[q][meta['id']], f'source/{meta["id"]}/{row["date"]}')
            elif name == 'currency': check(row['currency_index'], currency[q], f'source/sterling/{row["date"]}')
            elif name == 'trade':
                for field in ('exports', 'imports', 'trade_balance'): check(row[field], monthly[month][field], f'source/trade/{field}/{row["date"]}')
            else:
                flows = customs[month]
                for field, value in [('customs_exports', flows[2] + flows[4]), ('customs_imports', flows[1] + flows[3]), ('customs_goods', flows[2] + flows[4] - flows[1] - flows[3])]:
                    check(row[field], value, f'source/{field}/{row["date"]}')
    for frequency, periods in [('quarterly', 1), ('trailing4q', 4)]:
        view = service.view(frequency)
        for row in view['series']:
            q = qindex(row['date'])
            for field, code in CORE.items(): check(row[field], total(q, code, periods), f'core/{frequency}/{field}/{row["date"]}')
            for field in ('customs_exports', 'customs_imports', 'customs_goods'): check(row[field], custom_total(q, field, periods), f'core/{frequency}/{field}/{row["date"]}')
            customs_goods, goods = custom_total(q, 'customs_goods', periods), total(q, 'LQCT', periods)
            check(row['goods_gap'], customs_goods - goods if customs_goods is not None and goods is not None else None, f'core/{frequency}/goods_gap/{row["date"]}')
            check(row['financial_account'], -total(q, 'HBNT', periods), f'core/{frequency}/financial_account/{row["date"]}')
            check(row['net_financial_inflow'], total(q, 'HBNT', periods), f'core/{frequency}/net_financial_inflow/{row["date"]}')
            check(row['net_lending'], total(q, 'HBOP', periods) + total(q, 'FNVQ', periods), f'core/{frequency}/net_lending/{row["date"]}')
        for row in view['macro']['quarterly']:
            q = qindex(row['date']); ca, gdp = total(q, 'HBOP', periods), total(q, 'YBHA', periods)
            check(row['current_account_gdp_pct'], 100 * ca / gdp if ca is not None and gdp else None, f'macro/{frequency}/CA_GDP/{row["date"]}')
            # Derive investment from outward cash flows and liabilities, not the
            # production net-series mapping, to independently check UK signs.
            for field, asset, liability in [('net_direct', 'N2SV', 'N2SA'), ('net_equity', 'HBVI', 'XBLW'), ('net_debt', 'XBMW', 'XBLX'), ('net_other', 'XBMM', 'XBMN')]:
                a, b = total(q, asset, periods), total(q, liability, periods)
                check(row[field], a + b if a is not None and b is not None else None, f'macro/{frequency}/{field}/{row["date"]}')
            reserve = total(q, 'LTCV', periods)
            check(row['reserve_transactions'], -reserve if reserve is not None else None, f'macro/{frequency}/reserve_flow/{row["date"]}')
            stock = raw.get(q, {}).get('LTEB')
            debt = [raw.get(q, {}).get(code) for code in ('VTZY', 'ZAVI', 'ZAVB', 'ZAVF')]
            debt = sum(debt) if all(value is not None for value in debt) else None
            check(row['reserve_stock'], stock, f'macro/{frequency}/stock/{row["date"]}')
            check(row['short_term_external_debt'], debt, f'macro/{frequency}/debt/{row["date"]}')
            check(row['reserves_debt_pct'], stock / debt * 100 if stock is not None and debt else None, f'macro/{frequency}/coverage/{row["date"]}')
            indices = [currency.get(index) for index in range(q - periods + 1, q + 1)]
            check(row['currency_index'], sum(indices) / periods if all(value is not None for value in indices) else None, f'macro/{frequency}/sterling/{row["date"]}')
        for row in view['macro']['trade']:
            q = qindex(row['date']); exports, imports = trade_total(q, 'exports', periods), trade_total(q, 'imports', periods)
            check(row['trade_balance'], exports - imports if exports is not None and imports is not None else None, f'trade/{frequency}/balance/{row["date"]}')
            for field in ('exports', 'imports'):
                current = trade_total(q, field, periods); previous = trade_total(q - 4, field, periods)
                check(row[field], current, f'trade/{frequency}/{field}/{row["date"]}')
                values = [trade_total(q - lag, field, periods) for lag in range(3)]
                # The shipped history starts in 1997; no pre-1997 inputs enter MA/YoY.
                if q - periods - 1 < 1997 * 4: values = [None]
                check(row[field + '_ma3'], sum(values) / 3 if all(value is not None for value in values) else None, f'trade/{frequency}/{field}_MA3/{row["date"]}')
                if q - periods - 3 < 1997 * 4: previous = None
                check(row[field + '_yoy'], (current / previous - 1) * 100 if current is not None and previous else None, f'trade/{frequency}/{field}_YoY/{row["date"]}')
    live = None
    if args.live:
        with urlopen('http://127.0.0.1:8000/api/fx/bop?country=GBR&refresh=true', timeout=180) as response:
            assert response.headers['Cache-Control'] == 'no-store'
            live = json.load(response)
        assert live['delivery'] == 'source_checked' and live['refresh_available'] and not live['macro']['warnings']
        assert all(datetime.fromisoformat(source['checked_at']) >= started for source in live['sources'])
        assert live['series'] == service.view()['series'] and live['macro']['quarterly'] == service.view()['macro']['quarterly']
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'country': 'GBR', 'checks': sum(counts.values()),
              'checks_by_family': counts, 'failures': failures, 'live_source_refresh_verified': live is not None,
              'latest_quarter': service.view()['latest']['period'], 'sources': service.view()['sources'], 'inputs': inputs}
    destination = ROOT / 'data' / f'uk-bop-audit-{started.date()}.json'
    destination.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('checks', 'checks_by_family', 'failures', 'live_source_refresh_verified', 'latest_quarter')}, indent=2))
    print('Audit report:', destination)
    if failures: raise SystemExit('UK chart audit failed.')


if __name__ == '__main__':
    main()
