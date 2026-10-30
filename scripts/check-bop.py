"""Check all BOP countries on this computer. Standard-library Python only.

Default: download official releases into a temporary cache, validate sources
and all seven charts in both frequencies. Saved data alone cannot pass live
checks. --offline validates packaged charts without contacting any source.
--dashboard checks fresh source retrieval through the running application.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.client import HTTPException
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from backend.config import load_backend_env
from backend.bop import BOPError
from backend.bop_http import tls_context
from backend.bop_portability import ADAPTERS, CountryCheck, inspect_views, failure_code


def dashboard_check(base_url,code):
    started=datetime.now(timezone.utc);views={}
    try:
        for frequency in ('quarterly','trailing4q'):
            query=urlencode({'country':code,'frequency':frequency,'refresh':'true' if frequency=='quarterly' else 'false'})
            with urlopen(base_url.rstrip('/')+'/api/fx/bop?'+query,timeout=240) as response:
                if response.headers.get('Cache-Control')!='no-store':raise ValueError()
                body=response.read(12_000_001)
                if len(body)>12_000_000:raise ValueError()
                views[frequency]=json.loads(body)
        return inspect_views(code,views,started)
    except (OSError,HTTPException,ValueError,TypeError,KeyError,BOPError) as exc:
        return {'country':code,'status':'failed','check':'running_dashboard','error_code':failure_code(exc)}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--country',action='append',choices=ADAPTERS,help='Limit to one or more country codes; default is all nine.')
    parser.add_argument('--offline',action='store_true')
    parser.add_argument('--dashboard',metavar='BASE_URL')
    parser.add_argument('--json',action='store_true')
    args=parser.parse_args(argv)
    if args.offline and args.dashboard:parser.error('--offline cannot be combined with --dashboard.')
    if args.dashboard:
        parts=urlsplit(args.dashboard)
        if parts.scheme not in ('http','https') or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
            parser.error('Use a dashboard base URL without credentials, queries or fragments.')
    try:load_backend_env(PROJECT);tls_context() if not args.offline and not args.dashboard else None
    except (OSError,ValueError):
        print(json.dumps({'status':'failed','error_code':'local_configuration_error','hint':'Check .env syntax and BOP_CA_FILE; use a trusted PEM file if needed.'}));return 1
    codes=list(dict.fromkeys(args.country or ADAPTERS));reports={}
    enabled=os.environ.get('BOP_ENABLED','true').lower()!='false'
    with tempfile.TemporaryDirectory(prefix='bop-check-') as cache:
        def check(code):
            try:return dashboard_check(args.dashboard,code) if args.dashboard else CountryCheck(code,cache,enabled=enabled).run(live=not args.offline)
            except (OSError,ValueError,TypeError,KeyError,BOPError):return {'country':code,'status':'failed','error_code':'missing_or_invalid_packaged_data'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures={pool.submit(check,code):code for code in codes}
            for future in as_completed(futures):
                code=futures[future];reports[code]=future.result()
                if not args.json:
                    report=reports[code];print(f"{report['status'].upper()}: {code} ({report.get('check','BOP startup')})",flush=True)
                    for source in report.get('sources',[]):
                        if not args.offline:print(f"  {source['group']}: {'fresh' if source['fresh_download'] else 'NOT freshly downloaded'}; {source['checked_at']}",flush=True)
                    for error in report.get('network_errors',[]):print(f"  {error['host']}: {error['error_code']}",flush=True)
                    if report.get('error_code'):print('  '+report['error_code'],flush=True)
    result={'checked_at':datetime.now(timezone.utc).isoformat(),'mode':'offline' if args.offline else 'dashboard' if args.dashboard else 'live',
            'status':'passed' if all(reports[code]['status']=='passed' for code in codes) else 'failed','countries':[reports[code] for code in codes]}
    if args.json:print(json.dumps(result,indent=2))
    else:print('Saved chart checks do not certify live access. Live BOP data updates when official releases change; the visible dashboard checks every five minutes.')
    return 0 if result['status']=='passed' else 1


if __name__=='__main__':sys.exit(main())
