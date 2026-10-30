"""Refresh current ABS/BIS sources and retain native files for independent audit."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.bop_australia import AustraliaBOPService, SNAPSHOT, fetch_australia


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--snapshot', action='store_true'); args = parser.parse_args()
    audit = ROOT.parent / '.runtime' / 'aus-bop-research' / 'live-inputs'
    audit.mkdir(parents=True, exist_ok=True)
    def download(url):
        body = fetch_australia(url); key = sha256(url.encode()).hexdigest()
        (audit / (key + '.bin')).write_bytes(body)
        (audit / (key + '.url')).write_text(url, encoding='utf-8')
        return body
    service = AustraliaBOPService(ROOT / 'data' / 'bop-cache' / 'australia.json', transport=download)
    result = service.view(refresh=True)
    print(json.dumps({'latest_quarter': result['latest_quarter']['period'],
                      'current_account_quarterly_aud_bn': result['latest_quarter']['current_account'],
                      'current_account_4q_aud_bn': result['latest']['current_account'],
                      'validation': result['validation'], 'warnings': result['macro']['warnings'],
                      'sources': [{key: item[key] for key in ('group', 'checked_at', 'latest_observation')} for item in result['sources']]}, indent=2))
    if result['macro']['warnings'] or len(result['sources']) != 7:
        raise SystemExit('Australian refresh was incomplete; inspect source warnings.')
    if args.snapshot:
        SNAPSHOT.write_text(json.dumps(service.snapshot, allow_nan=False), encoding='utf-8')
        print('Saved verified Australian baseline:', SNAPSHOT)


if __name__ == '__main__': main()
