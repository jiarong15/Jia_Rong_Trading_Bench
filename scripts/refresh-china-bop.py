"""Refresh all official China sources and retain reproducible raw audit inputs."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.bop_china import ChinaBOPService,SNAPSHOT,fetch_china_bytes


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--snapshot',action='store_true',help='Also update the verified baseline shipped with the dashboard.')
    args=parser.parse_args()
    audit=ROOT.parent/'.runtime'/'china-bop-research'/'live-inputs'
    audit.mkdir(parents=True,exist_ok=True)
    def download(url,payload=None):
        body=fetch_china_bytes(url,payload)
        request={'url':url,'payload':payload}
        key=sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
        (audit/(key+'.bin')).write_bytes(body)
        (audit/(key+'.json')).write_text(json.dumps(request),encoding='utf-8')
        return body
    service=ChinaBOPService(ROOT/'data'/'bop-cache'/'china.json',transport=download)
    view=service.view(refresh=True)
    print(json.dumps({'checked_at':datetime.now(timezone.utc).isoformat(),
                      'latest_quarter':view['latest']['period'],'current_account_usd_bn':view['latest']['current_account'],
                      'macro_latest':view['macro']['quarterly'][-1],
                      'validation':view['validation'],
                      'sources':[{key:source[key] for key in ('group','checked_at','latest_observation')} for source in view['sources']],
                      'warnings':view['macro']['warnings']},indent=2))
    if view['macro']['warnings'] or len(view['sources'])!=7:
        raise SystemExit('China source refresh was incomplete; inspect the warnings.')
    if args.snapshot:
        SNAPSHOT.write_text(json.dumps(service.snapshot,allow_nan=False),encoding='utf-8')
        print('Saved verified China baseline:',SNAPSHOT)


if __name__=='__main__':main()
