"""Start the app without changing PowerShell execution policy."""
import argparse
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser
from backend.config import load_backend_env, project_id


def read_json(url):
    with urllib.request.urlopen(url,timeout=2) as response:return json.load(response)


def stop_owned_server(health,pid_file,project,terminate=None):
    """Refuse to stop an unrelated app, another checkout, or a stale PID."""
    try:stored=int(pid_file.read_text().strip())
    except (OSError,ValueError):raise ValueError('Cannot verify the running server PID. Stop that server manually before restarting.') from None
    if (health.get('mode')!='synthetic-paper' or health.get('project_id')!=project_id(project)
            or type(health.get('process_id')) is not int or stored!=health['process_id'] or stored<=1 or stored==os.getpid()):
        raise ValueError('The running server does not match this project and saved PID. Stop that server manually before restarting.')
    (terminate or os.kill)(stored,signal.SIGTERM)


def show_fx_status(url):
    try:
        status=read_json(url+'/api/fx/status')
        yahoo=status['yahoo'];twelve=status['twelvedata']
        print(f"FX primary: {status['primary_provider']}. Yahoo: {'enabled' if yahoo['enabled'] else 'disabled'}, yfinance {'installed' if yahoo['package_present'] else 'missing'}; no API key required.")
        print(f"Twelve Data: {'enabled' if twelve['enabled'] else 'disabled'}; credentials {'configured' if twelve['configured'] else 'not configured (optional backup)' }.")
        if not twelve['configured']:print('To configure the backup on this computer: python scripts/configure-fx.py')
        print('These are local configuration checks. Test source access with: python scripts/check-fx.py')
    except (OSError,ValueError,KeyError,TypeError):print('FX startup status unavailable. Use --restart to load current code and settings.')


def show_bop_status(url):
    try:
        status=read_json(url+'/api/fx/bop/status');countries=status['countries']
        ready=sum(country['saved_charts_ready'] for country in countries)
        print(f"BOP: {ready}/{len(countries)} country chart sets ready; live refresh {'enabled' if status['live_refresh_enabled'] else 'disabled'}; public sources require no API key.")
        if not status['live_refresh_enabled']:print('Set BOP_ENABLED=true in backend settings and restart to enable source checks.')
        print('Verify fresh official downloads here with: python scripts/check-bop.py')
    except (OSError,ValueError,KeyError,TypeError):print('BOP startup status unavailable. Restart the server, then run python scripts/check-bop.py.')

def main():
    args=argparse.ArgumentParser(description='Start JR Trading Workbench locally')
    args.add_argument('--port',type=int,default=8000)
    args.add_argument('--no-browser',action='store_true')
    args.add_argument('--restart',action='store_true',help='Restart this project\'s verified server to load new code and local credentials')
    options=args.parse_args()
    if not 1024<=options.port<=65535: args.error('Port must be between 1024 and 65535')
    project=Path(__file__).resolve().parent;runtime=project.parent/'.runtime'
    try:load_backend_env(project)
    except (OSError,ValueError) as exc:raise SystemExit(str(exc)) from None
    if not (project/'dist'/'index.html').exists(): raise SystemExit('Run python bootstrap.py first.')
    runtime.mkdir(exist_ok=True);url=f'http://127.0.0.1:{options.port}'
    pid_file=runtime/('server-pid.txt' if options.port==8000 else f'server-{options.port}-pid.txt')
    def healthy():
        try:
            health=read_json(url+'/api/health')
            return health if health.get('mode')=='synthetic-paper' else None
        except (OSError,ValueError): return False
    existing=healthy()
    if options.restart and existing:
        try:stop_owned_server(existing,pid_file,project)
        except (OSError,ValueError) as exc:raise SystemExit(str(exc)) from None
        for _ in range(40):
            if not healthy():break
            time.sleep(.25)
        else:raise SystemExit('The server has not stopped yet. Wait and retry; no second server was started.')
    elif existing:print('Reusing the running server. Use --restart after changing code or credentials.')
    if not healthy():
        env={**os.environ,'PYTHONPATH':os.pathsep.join([str(runtime/'python'),str(runtime/'yfinance')])}
        log=(runtime/'server.log').open('a',encoding='utf-8')
        flags=(subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP) if os.name=='nt' else 0
        server=subprocess.Popen([sys.executable,'-m','uvicorn','backend.app:app','--host','127.0.0.1','--port',str(options.port)],cwd=project,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=flags,start_new_session=os.name!='nt')
        log.close();pid_file.write_text(str(server.pid))
        for _ in range(120):
            if healthy(): break
            if server.poll() is not None: raise SystemExit(f'Server exited. Inspect {runtime / "server.log"}')
            time.sleep(.5)
        else: raise SystemExit(f'Startup timed out. Inspect {runtime / "server.log"}')
    print(f'JR Trading Workbench is running at {url}')
    show_fx_status(url)
    show_bop_status(url)
    if not options.no_browser: webbrowser.open(url)

if __name__=='__main__': main()
