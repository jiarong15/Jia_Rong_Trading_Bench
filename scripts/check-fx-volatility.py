"""Read-only connection/coverage check. Prints source data, never credentials."""
from datetime import datetime, timezone
from pathlib import Path
import json
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT.parent/'.runtime'/'python'))
sys.path.insert(0,str(PROJECT))
from backend.config import load_backend_env
from backend.fx_volatility import FXVolatilitySource


def main():
    load_backend_env(PROJECT)
    source = FXVolatilitySource(PROJECT/'data'/'saxo-fx-volatility.json')
    try:
        source.refresh(datetime.now(timezone.utc),force=True)
        snapshot = source.snapshot(datetime.now(timezone.utc))
        print(json.dumps(snapshot,indent=2))
        print('Saxo report only. The app checks for new reports every minute; values retain their publication time.')
        return 0 if snapshot['quotes'] else 1
    finally: source.close()


if __name__ == '__main__': raise SystemExit(main())
