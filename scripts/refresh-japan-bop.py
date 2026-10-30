"""Refresh Japan's official sources and optionally update the bundled baseline."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.bop_japan import JapanBOPService, SNAPSHOT, fetch_japan_text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', action='store_true', help='Also save the verified baseline shipped with the app.')
    args = parser.parse_args()
    audit = ROOT.parent / '.runtime' / 'japan-bop-research' / 'live-inputs'
    audit.mkdir(parents=True, exist_ok=True)

    def download(url):
        text = fetch_japan_text(url)
        from hashlib import sha256
        key = sha256(url.encode()).hexdigest()
        (audit / (key + '.txt')).write_text(text, encoding='utf-8')
        (audit / (key + '.url')).write_text(url, encoding='utf-8')
        return text

    service = JapanBOPService(ROOT / 'data' / 'bop-cache' / 'japan.json', transport=download)
    view = service.view(refresh=True)
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'latest_quarter': view['latest']['period'],
              'current_account_jpy_bn': view['latest']['current_account'], 'validation': view['validation'],
              'sources': [{key: source[key] for key in ('group', 'checked_at', 'latest_observation')} for source in view['sources']],
              'warnings': view['macro']['warnings']}
    print(json.dumps(report, indent=2))
    if view['macro']['warnings'] or len(view['sources']) != 5:
        raise SystemExit('Japan source refresh was incomplete; inspect the source warnings.')
    if args.snapshot:
        SNAPSHOT.write_text(json.dumps(service.snapshot, allow_nan=False), encoding='utf-8')
        print('Saved verified Japan baseline:', SNAPSHOT)


if __name__ == '__main__':
    main()
