"""Start the app without changing PowerShell execution policy."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

def main():
    args=argparse.ArgumentParser(description='Start Meridian locally')
    args.add_argument('--port',type=int,default=8000)
    args.add_argument('--no-browser',action='store_true')
    options=args.parse_args()
    if not 1024<=options.port<=65535: args.error('Port must be between 1024 and 65535')
    project=Path(__file__).resolve().parent;runtime=project.parent/'.runtime'
    if not (project/'dist'/'index.html').exists(): raise SystemExit('Run python bootstrap.py first.')
    runtime.mkdir(exist_ok=True);url=f'http://127.0.0.1:{options.port}'
    def healthy():
        try:
            with urllib.request.urlopen(url+'/api/health',timeout=1) as response:
                return json.load(response).get('mode')=='synthetic-paper'
        except (OSError,ValueError): return False
    if not healthy():
        env={**os.environ,'PYTHONPATH':os.pathsep.join([str(runtime/'python'),str(runtime/'yfinance')])}
        log=(runtime/'server.log').open('a',encoding='utf-8')
        flags=(subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP) if os.name=='nt' else 0
        server=subprocess.Popen([sys.executable,'-m','uvicorn','backend.app:app','--host','127.0.0.1','--port',str(options.port)],cwd=project,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=flags,start_new_session=os.name!='nt')
        log.close();(runtime/'server-pid.txt').write_text(str(server.pid))
        for _ in range(120):
            if healthy(): break
            if server.poll() is not None: raise SystemExit(f'Server exited. Inspect {runtime / "server.log"}')
            time.sleep(.5)
        else: raise SystemExit(f'Startup timed out. Inspect {runtime / "server.log"}')
    print(f'Meridian is running at {url}')
    if not options.no_browser: webbrowser.open(url)

if __name__=='__main__': main()
