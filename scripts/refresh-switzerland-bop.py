"""Refresh Swiss official sources; retain JSON and independent native CSV inputs."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.bop_switzerland import (SwitzerlandBOPService, fetch_switzerland, SNAPSHOT,
                                    GROUP_NAMES, SPEC, SNB_ROOT, build_switzerland_view)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--snapshot', action='store_true'); args = parser.parse_args()
    destination = ROOT.parent / '.runtime' / 'ch-bop-research' / 'live-inputs'; destination.mkdir(parents=True, exist_ok=True)
    def transport(url):
        body = fetch_switzerland(url); path = destination / sha256(url.encode()).hexdigest()
        path.with_suffix('.bin').write_bytes(body); path.with_suffix('.url').write_text(url, encoding='utf-8'); return body
    service = SwitzerlandBOPService(ROOT / 'data' / 'bop-cache' / 'switzerland.json', transport=transport)
    def download(name):
        group = service._download(name); print(name, len(group['rows']), group['rows'][-1]['date'], flush=True); return name, group
    with ThreadPoolExecutor(max_workers=4) as pool: groups = dict(pool.map(download, GROUP_NAMES))
    snapshot = {'schema_version': 1, 'country': 'CHE', 'groups': groups, 'warnings': []}
    view = build_switzerland_view(snapshot)
    # The independent audit parses SNB's CSV representation, not app JSON.
    cubes = {definition['cube'] for definitions in SPEC.values() for definition in definitions}
    with ThreadPoolExecutor(max_workers=4) as pool: list(pool.map(transport, [SNB_ROOT + cube + '/data/csv/en' for cube in cubes]))
    service.cache_path.parent.mkdir(parents=True, exist_ok=True)
    service.cache_path.with_suffix('.tmp').write_text(json.dumps(snapshot, allow_nan=False), encoding='utf-8')
    service.cache_path.with_suffix('.tmp').replace(service.cache_path)
    if args.snapshot: SNAPSHOT.write_text(json.dumps(snapshot, allow_nan=False), encoding='utf-8')
    print(json.dumps({'latest_quarter': view['latest']['period'], 'current_account_chf_bn_4q': view['latest']['current_account'], 'validation': view['validation']}, indent=2))


if __name__ == '__main__': main()
