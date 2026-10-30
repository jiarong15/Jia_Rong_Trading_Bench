"""Build the shipped companion snapshot from downloaded official observations."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.bop_macro import (GROUPS, DEBT_INDEX, DebtLinks, DebtDateMismatch, SNAPSHOT,
                               fetch_macro_text, parse_csv, parse_debt, source_url)

root = Path(__file__).resolve().parents[2] / '.runtime' / 'bop-new-panels'
files = {'quarterly': 'quarterly.csv', 'reserve_stock': 'reserve-stock.csv',
         'monthly': 'monthly-trade.csv', 'daily': 'daily.csv'}
groups = {}
root.mkdir(parents=True, exist_ok=True)
for name, filename in files.items():
    path = root / filename
    ids, frequency = GROUPS[name]
    if not path.exists():
        path.write_text(fetch_macro_text(source_url(ids)), encoding='utf-8')
    groups[name] = {'url': source_url(ids), 'checked_at': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                    'rows': parse_csv(path.read_text(encoding='utf-8-sig'), ids, frequency)}
links = DebtLinks()
index_path = root / 'treasury-debt.html'
if not index_path.exists():
    index_path.write_text(fetch_macro_text(DEBT_INDEX), encoding='utf-8')
links.feed(index_path.read_text(encoding='utf-8'))

def debt(item):
    at, url = item
    path = root / ('debt-d-' + at + '.html')
    if not path.exists():
        path.write_text(fetch_macro_text(url), encoding='utf-8')
    try:
        return {'date': at, 'principal_due_1y': parse_debt(path.read_text(encoding='utf-8'), at), 'url': url}
    except DebtDateMismatch:
        return {'date': at, 'gap': True}

rows = []
warnings = []
with ThreadPoolExecutor(max_workers=4) as pool:
    for future in as_completed([pool.submit(debt, item) for item in links.links.items()]):
        row = future.result()
        if row.get('gap'):
            warnings.append(f"Treasury's table linked for {row['date'][:4]} Q{(int(row['date'][5:7]) - 1) // 3 + 1} reports another quarter; that debt-coverage observation is unavailable.")
            continue
        rows.append(row)
        print('Treasury verified', row['date'], row['principal_due_1y'], flush=True)
groups['debt'] = {'url': DEBT_INDEX, 'checked_at': datetime.now(timezone.utc).isoformat(), 'rows': sorted(rows, key=lambda r: r['date']), 'warnings': warnings}
SNAPSHOT.write_text(json.dumps({'schema_version': 1, 'groups': groups, 'warnings': []}, indent=2, allow_nan=False) + '\n', encoding='utf-8')
print('Saved', SNAPSHOT, {key: len(value['rows']) for key, value in groups.items()}, flush=True)
