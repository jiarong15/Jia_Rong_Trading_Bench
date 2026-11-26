"""Read-only fresh FRED/DTCC source check for all eleven U.S. Rates panels."""
from pathlib import Path
import sys

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT.parent/'.runtime'/'python'))
sys.path.insert(0,str(PROJECT))
from backend.config import load_backend_env
from backend.us_rates import USRatesService


def main():
    load_backend_env(PROJECT)
    document=USRatesService(PROJECT/'data'/'us-rates.json').view(refresh=True)
    print('U.S. Rates panels:',len(document['charts']))
    failed=[]
    for chart in document['charts']:
        print(chart['title'])
        for series in chart['series']:
            print(' ',series['label'],series['last_value'],series['unit'],series['last_observation_date'],series['status'])
            if series['error']: print('  ',series['error'])
            if series.get('last_observation_at'): print('  Latest eligible trade:',series['last_observation_at'],'; count:',series['trade_count'])
            if series['status'] in ('unavailable','stale'): failed.append(series['series_id'])
    print('Data follows each source publication schedule. Monthly dates are reference periods.')
    return 1 if failed else 0


if __name__=='__main__': raise SystemExit(main())
