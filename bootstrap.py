"""Install a workspace-local toolchain and build the app. Python 3.12+."""
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request
import zipfile

def main():
    import sys
    if sys.version_info<(3,12): raise SystemExit('Python 3.12+ is required by the pinned Yahoo FX dependencies.')
    project=Path(__file__).resolve().parent;runtime=project.parent/'.runtime';runtime.mkdir(exist_ok=True)
    node=shutil.which('node');marker=runtime/'node-path.txt'
    if not node and marker.exists():
        candidate=Path(marker.read_text().strip())/'node.exe'
        if candidate.exists(): node=str(candidate)
    if not node:
        if os.name!='nt': raise SystemExit('Install Node.js 22.12+ and rerun python bootstrap.py.')
        version='v24.22.1';filename=f'node-{version}-win-x64.zip';base=f'https://nodejs.org/dist/{version}/'
        print('Downloading portable Node.js...',flush=True)
        with urllib.request.urlopen(base+filename,timeout=120) as response: archive=response.read()
        with urllib.request.urlopen(base+'SHASUMS256.txt',timeout=30) as response: sums=response.read().decode()
        expected=next(line.split()[0] for line in sums.splitlines() if line.endswith('  '+filename))
        if hashlib.sha256(archive).hexdigest()!=expected: raise SystemExit('Node.js checksum verification failed.')
        with zipfile.ZipFile(io.BytesIO(archive)) as package: package.extractall(runtime)
        directory=runtime/f'node-{version}-win-x64';marker.write_text(str(directory));node=str(directory/'node.exe')
    directory=Path(node).parent
    npm_cli=directory/'node_modules'/'npm'/'bin'/'npm-cli.js'
    env={**os.environ,'PATH':str(directory)+os.pathsep+os.environ.get('PATH','')}
    if npm_cli.exists(): npm=[node,str(npm_cli)]
    else:
        npm_binary=shutil.which('npm',path=env['PATH'])
        if not npm_binary: raise SystemExit('npm was not found next to Node.js.')
        npm=[npm_binary]
    subprocess.run(npm+['ci','--no-audit','--no-fund'],cwd=project,env=env,check=True)
    import sys
    subprocess.run([sys.executable,'-m','pip','install','--target',str(runtime/'python'),'-r',str(project/'backend'/'requirements.lock.txt'),'--upgrade'],cwd=project,check=True)
    subprocess.run([sys.executable,'-m','pip','install','--target',str(runtime/'yfinance'),'-r',str(project/'backend'/'requirements-yahoo.lock.txt'),'--upgrade'],cwd=project,check=True)
    subprocess.run([sys.executable,'-I',str(project/'scripts'/'check-bop.py'),'--offline'],cwd=project,check=True)
    subprocess.run(npm+['run','build'],cwd=project,env=env,check=True)
    print('Ready. Run python launch.py to open JR Trading Workbench.')

if __name__=='__main__': main()
