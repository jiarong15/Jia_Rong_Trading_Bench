"""Refresh six official Singapore source families and retain native audit inputs."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.bop_singapore import SingaporeBOPService, SNAPSHOT, fetch_singapore_text


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--snapshot', action='store_true'); args = parser.parse_args()
    audit = ROOT.parent / '.runtime' / 'sg-bop-research' / 'live-inputs'
    audit.mkdir(parents=True, exist_ok=True)

    def download(url):
        body = fetch_singapore_text(url)
        key = sha256(url.encode()).hexdigest()
        (audit / (key + '.txt')).write_text(body, encoding='utf-8')
        (audit / (key + '.url')).write_text(url, encoding='utf-8')
        return body

    service = SingaporeBOPService(ROOT / 'data' / 'bop-cache' / 'singapore.json', transport=download)
    result = service.view(refresh=True)
    print(json.dumps({'checked_at': datetime.now(timezone.utc).isoformat(), 'latest_quarter': result['latest_quarter']['period'],
                      'current_account_quarterly_sgd_bn': result['latest_quarter']['current_account'],
                      'current_account_4q_sgd_bn': result['latest']['current_account'], 'validation': result['validation'],
                      'sources': [{key: item[key] for key in ('group', 'checked_at', 'latest_observation')} for item in result['sources']],
                      'warnings': result['macro']['warnings']}, indent=2))
    if result['macro']['warnings'] or len(result['sources']) != 6:
        raise SystemExit('Singapore refresh was incomplete; inspect source warnings.')
    if args.snapshot:
        SNAPSHOT.write_text(json.dumps(service.snapshot, allow_nan=False), encoding='utf-8')
        print('Saved verified Singapore baseline:', SNAPSHOT)


if __name__ == '__main__':
    main()
