"""Read-only public DTCC FX backfill and estimated-Greek connection check."""
import argparse
from pathlib import Path
import sys

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT.parent/'.runtime'/'python'))
sys.path.insert(0,str(PROJECT))
from backend.config import load_backend_env
from backend.fx_carry_risk import FXCarryRiskService
from backend.fx_options import FXOptionsService
from backend.investing_forwards import InvestingForwardSource
from backend.fx_volatility import FXVolatilitySource


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--batches',type=int,default=1,choices=range(1,9))
    args=parser.parse_args(); load_backend_env(PROJECT)
    # Reuse source-dated market caches without competing with a running
    # dashboard's report/forward writers. The server refreshes these inputs.
    carry=FXCarryRiskService(PROJECT/'data'/'fx-carry-risk.json',enabled=False,
        forward_source=InvestingForwardSource(PROJECT/'data'/'investing-fx-forwards.json'),
        volatility_source=FXVolatilitySource(PROJECT/'data'/'saxo-fx-volatility.json'))
    service=FXOptionsService(PROJECT/'data'/'fx-option-trades.json',carry)
    for batch in range(args.batches):
        service.refresh(force=True)
        d=service.view(saved_only=True)
        print({'batch':batch+1,'status':d['status'],'basis':d['basis'],'trades':d['included_trades'],
               'modelled_trades':d['modelled_trades'],'cells':len(d['cells']),'checked_at':d['checked_at'],
               'last_trade_at':d['last_trade_at'],'spot':d['spot'],'spot_date':d['spot_date'],
               'volatility_published_at':d['volatility_published_at'],'pending_history_files':d['pending_history_files'],
               'pending_live_files':d['pending_live_files'],'error':d['error']},flush=True)
        if d['error'] or not d['pending_history_files'] and not d['pending_live_files']:
            break
    return 0 if d['cells'] and not d['error'] else 1


if __name__=='__main__':raise SystemExit(main())
