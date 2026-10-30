"""Reproduce the BOP audit with Decimal arithmetic and raw source files.

Example: python scripts/audit-bop.py --source-dir ../.runtime/bop-audit
Use --download to retrieve new inputs. The dated BEA fixture tests the release
reviewed for this audit; later source revisions can legitimately change it.
"""
import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.bop import (CENSUS_URL, FIELDS, FRED_URL, MONTHS, SERIES, SNAPSHOT,
                         CensusRows, aggregate, build_view, fetch_text)


def dec(value):
    return Decimal(str(value).replace(',', '').strip())


def audit(snapshot, fred_csv, census_html, reference):
    raw = list(csv.DictReader(io.StringIO(fred_csv)))
    direct = {r['observation_date']: r for r in raw}
    failures = []
    counts = {'fred_source_values': 0, 'census_quarter_values': 0, 'derived_values': 0, 'rolling_values': 0, 'independent_bea_reference_values': 0}
    def compare(category, key, actual, expected, tolerance=Decimal('0.00000001')):
        counts[category] += 1
        if actual is None or abs(dec(actual) - expected) > tolerance:
            failures.append({'check': category, 'field': key, 'actual': actual, 'expected': str(expected)})
    parser = CensusRows()
    parser.feed(census_html)
    months = {}
    for cells in parser.rows:
        if len(cells) != 4:
            continue
        match = re.fullmatch(r'([A-Za-z]+)\s+(\d{4})', cells[0])
        if match and match[1] in MONTHS:
            months[f'{match[2]}-{MONTHS[match[1]]:02d}-01'] = [dec(value) for value in cells[1:]]
    by_date = {row['date']: row for row in snapshot['series']}
    for row in snapshot['series']:
        at = row['date']
        for field, (series_id, _) in SERIES.items():
            value = direct[at][series_id]
            if value not in ('', '.'):
                compare('fred_source_values', at + ':' + field, row[field], dec(value) / 1000)
        year, month = int(at[:4]), int(at[5:7])
        keys = [f'{year}-{m:02d}-01' for m in range(month, month + 3)]
        if all(key in months for key in keys):
            for index, field in enumerate(('customs_exports', 'customs_imports', 'customs_goods')):
                expected = sum((months[key][index] for key in keys), Decimal(0)) / 1000
                compare('census_quarter_values', at + ':' + field, row[field], expected)
            compare('derived_values', at + ':goods_gap', row['goods_gap'], dec(row['customs_goods']) - dec(row['goods']))
        compare('derived_values', at + ':net_financial_inflow', row['net_financial_inflow'], -dec(row['financial_account']))
    rolling = {row['date']: row for row in aggregate(snapshot['series'], 'trailing4q')}
    for i in range(3, len(snapshot['series'])):
        window = snapshot['series'][i - 3:i + 1]
        result = rolling.get(window[-1]['date'])
        if result is None:
            continue
        for field in FIELDS:
            if all(row[field] is not None for row in window):
                expected = sum((dec(row[field]) for row in window), Decimal(0))
                compare('rolling_values', result['period'] + ':' + field, result[field], expected)
    for field, observations in reference['series_usd_millions'].items():
        for at, value in zip(reference['dates'], observations):
            # Census monthly data retain tenths of millions; BEA rounds its table
            # to whole millions. Allow one million for that cross-source check.
            tolerance = Decimal('.001') if field.startswith('customs_') else Decimal('0.00000001')
            compare('independent_bea_reference_values', at + ':' + field, by_date[at][field], dec(value) / 1000, tolerance)
    for field, value in reference['latest_quarter_usd_millions'].items():
        compare('independent_bea_reference_values', field, by_date[reference['dates'][-1]][field], dec(value) / 1000)
    view = build_view(snapshot)
    latest = view['latest']
    prior = next(row for row in view['series'] if row['period'] == f'{int(latest["period"][:4]) - 1}{latest["period"][4:]}')
    return {
        'audited_at': datetime.now(timezone.utc).isoformat(), 'status': 'passed' if not failures else 'differences_found',
        'source_files_sha256': {'fred': hashlib.sha256(fred_csv.encode()).hexdigest(), 'census': hashlib.sha256(census_html.encode()).hexdigest()},
        'checks': counts, 'total_values_checked': sum(counts.values()), 'failures': failures,
        'independent_bea_sources': reference['sources'], 'accounting_checks': view['validation'],
        'latest_quarter': view['latest_quarter'], 'rolling_4q': latest,
        'contributing_quarters': [row['period'] for row in view['contributing_quarters']],
        'insight_inputs': {'services_offset_percent': latest['services'] / abs(latest['goods']) * 100,
                           'current_account_change_yoy_usd_billions': round(latest['current_account'] - prior['current_account'], 6)},
        'interpretation': 'Quarterly seasonally adjusted flows are not annualized. Rolling 4Q sums Q3 2025–Q2 2026. Source revisions and independent published rounding are preserved. Annual unadjusted series or earlier vintages are not like-for-like comparisons.'
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-dir', type=Path, default=ROOT.parent / '.runtime' / 'bop-audit')
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT / 'data' / 'bop-audit-2026-10-29.json')
    args = parser.parse_args()
    args.source_dir.mkdir(parents=True, exist_ok=True)
    fred = args.source_dir / 'fred-all-fresh.csv'
    census = args.source_dir / 'census-fresh.html'
    if args.download:
        fred.write_text(fetch_text(FRED_URL), encoding='utf-8')
        census.write_text(fetch_text(CENSUS_URL), encoding='utf-8')
    snapshot = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
    reference = json.loads((SNAPSHOT.parent / 'usa_bop_reference_2026-09-24.json').read_text(encoding='utf-8'))
    report = audit(snapshot, fred.read_text(encoding='utf-8'), census.read_text(encoding='utf-8'), reference)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(json.dumps({'status': report['status'], 'values_checked': report['total_values_checked'], 'failures': report['failures'], 'report': str(args.output)}))
    if report['failures']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
