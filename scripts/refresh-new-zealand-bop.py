"""Refresh official New Zealand sources; --snapshot updates the startup baseline."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.bop_new_zealand import NewZealandBOPService, fetch_new_zealand, SNAPSHOT, GROUP_NAMES, build_new_zealand_view


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--snapshot', action='store_true'); args = parser.parse_args()
    destination = ROOT.parent / '.runtime' / 'nz-bop-research' / 'live-inputs'; destination.mkdir(parents=True, exist_ok=True)
    def transport(url):
        body = fetch_new_zealand(url); path = destination / sha256(url.encode()).hexdigest()
        path.with_suffix('.bin').write_bytes(body); path.with_suffix('.url').write_text(url, encoding='utf-8'); return body
    service = NewZealandBOPService(ROOT / 'data' / 'bop-cache' / 'new-zealand.json', transport=transport)
    # Download directly so setup errors retain their exact source context.
    groups = {}
    for name in GROUP_NAMES:
        groups[name] = service._download(name)
        print(name, len(groups[name]['rows']), groups[name]['rows'][-1]['date'], flush=True)
    snapshot = {'schema_version': 1, 'country': 'NZL', 'groups': groups, 'warnings': []}
    view = build_new_zealand_view(snapshot)
    service.cache_path.parent.mkdir(parents=True, exist_ok=True)
    service.cache_path.with_suffix('.tmp').write_text(json.dumps(snapshot, allow_nan=False), encoding='utf-8')
    service.cache_path.with_suffix('.tmp').replace(service.cache_path)
    if args.snapshot: SNAPSHOT.write_text(json.dumps(snapshot, allow_nan=False), encoding='utf-8')
    print(json.dumps({'latest_quarter': view['latest']['period'], 'current_account_nzd_bn_4q': view['latest']['current_account'], 'validation': view['validation']}, indent=2))


if __name__ == '__main__': main()
