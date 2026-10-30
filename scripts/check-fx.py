"""Fresh source diagnostics; no key values or raw authenticated responses."""
import argparse
import json
import os
from pathlib import Path
import sys

PROJECT=Path(__file__).resolve().parents[1]
RUNTIME=PROJECT.parent/'.runtime'
sys.path[:0]=[str(PROJECT),str(RUNTIME/'python'),str(RUNTIME/'yfinance')]
from backend.config import load_backend_env
from backend.credentials import twelvedata_status
from backend.fx_sources import FXSources
from backend.twelvedata import TwelveDataService, TwelveDataError, pair_from
from backend.yahoo_fx import YahooFXService


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pair',default='EUR/USD')
    parser.add_argument('--all-intervals',action='store_true')
    parser.add_argument('--check-backup',action='store_true',help='Also validate Twelve Data account access (uses an account-usage request)')
    options=parser.parse_args()
    try:load_backend_env(PROJECT);pair=pair_from(options.pair)
    except (OSError,ValueError) as exc:parser.error(str(exc))
    yahoo=YahooFXService(RUNTIME/'yahoo-check-cache',enabled=os.environ.get('YAHOO_FX_ENABLED','true').lower()!='false')
    twelve=TwelveDataService(enabled=os.environ.get('TWELVEDATA_ENABLED','true').lower()!='false')
    sources=FXSources(yahoo,twelve,primary=os.environ.get('FX_PRIMARY_PROVIDER','yahoo'))
    result={'pair':pair,'primary_provider':sources.primary,'yahoo_enabled':yahoo.enabled,
            'twelvedata':{'enabled':twelve.enabled,**twelvedata_status()},'history':[]};failed=False
    for interval in (['1day','1h','5min'] if options.all_intervals else ['1day']):
        try:
            history=sources.history(pair,interval)
            result['history'].append({'interval':interval,'provider':history['provider'],'observations':len(history['bars']),
                'latest':history['bars'][-1]['date'],'retrieved_at':history['fetched_at'],'fallback':history['fallback'],
                'fallback_reason':history.get('fallback_reason'),'unavailable_ohlc':history.get('data_quality',{}).get('unavailable_ohlc_count',0)})
        except TwelveDataError as exc:result['history'].append({'interval':interval,'error':str(exc)});failed=True
    if options.check_backup:
        try:twelve.initialize();result['backup_check']='Account access accepted'
        except TwelveDataError as exc:result['backup_check']=str(exc);failed=True
    print(json.dumps(result,indent=2));return 1 if failed else 0


if __name__=='__main__':sys.exit(main())
