"""Read-only DTCC source check/backfill; prints aggregates, never trade records."""
import argparse
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT.parent/'.runtime'/'python'))
sys.path.insert(0, str(PROJECT))
from backend.config import load_backend_env
from backend.dtcc_inflation import DTCCInflationSource


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--batches', type=int, default=1, choices=range(1, 51))
    args = parser.parse_args(); load_backend_env(PROJECT)
    source = DTCCInflationSource(PROJECT/'data'/'dtcc-inflation.json')
    for index in range(args.batches):
        source.refresh(force=True); result = source.snapshot()
        latest = max(result['values'], default=None)
        print({'batch': index+1, 'status': result['status'], 'observations': len(result['values']),
               'history_start': result['history_start'], 'latest_date': latest,
               'latest_percent': result['values'].get(latest), 'last_trade_at': result['last_trade_at'],
               'checked_at': result['checked_at'], 'pending_live_files': result['pending_live_files'],
               'pending_history_files': result['pending_history_files'], 'error': result['error']}, flush=True)
        if result['error'] or not result['pending_history_files'] and not result['pending_live_files']:
            break
    return 0 if result['values'] and not result['error'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
