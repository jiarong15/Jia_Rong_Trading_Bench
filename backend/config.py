"""Small, backend-only local configuration loader; never executes shell code."""
from hashlib import sha256
import os
from pathlib import Path
import re
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
ALLOWED = {'TWELVEDATA_API_KEY', 'TWELVEDATA_ENABLED', 'YAHOO_FX_ENABLED', 'FX_PRIMARY_PROVIDER',
           'ALPACA_API_KEY', 'ALPACA_SECRET_KEY', 'ALPACA_ENABLED', 'NASDAQ_DATA_LINK_API_KEY', 'NASDAQ_DATA_LINK_ENABLED',
           'FRED_API_KEY', 'BOP_ENABLED', 'SEC_ENABLED', 'SEC_USER_AGENT', 'WORKBENCH_DB',
           'TELEGRAM_NEWS_ENABLED', 'TELEGRAM_NEWS_CA_FILE'}


def project_id(project=PROJECT):
    return sha256(os.path.normcase(str(Path(project).resolve())).encode()).hexdigest()


def read_backend_env(path):
    path = Path(path)
    if not path.exists(): return {}
    if path.stat().st_size > 65_536: raise ValueError('The backend .env file exceeds the configuration size limit.')
    result = {}
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        text = line.strip()
        if not text or text.startswith('#'): continue
        if text.startswith('export '): text = text[7:].lstrip()
        match = re.fullmatch(r'([A-Z][A-Z0-9_]*)\s*=\s*(.*)', text)
        if not match: raise ValueError(f'Invalid backend .env syntax on line {number}; use NAME=value.')
        key, value = match.groups()
        if key not in ALLOWED: continue
        if value.startswith(('"', "'")):
            quoted = re.fullmatch(r'''(["'])(.*?)\1\s*(?:#.*)?''', value)
            if not quoted: raise ValueError(f'Invalid quoted backend .env value on line {number}.')
            value = quoted.group(2)
        else: value = re.split(r'\s+#', value, maxsplit=1)[0].strip()
        if '\x00' in value: raise ValueError(f'Invalid backend .env value on line {number}.')
        result[key] = value
    return result


def load_backend_env(project=PROJECT, environ=None):
    target = os.environ if environ is None else environ
    values = read_backend_env(Path(project) / '.env')
    for key, value in values.items():
        if not target.get(key): target[key] = value
    return set(values)


def save_twelvedata_key(key, project=PROJECT):
    """Write only when invoked by the interactive setup; keep other settings."""
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', key):
        raise ValueError('Enter an API key using only letters, numbers, underscores or hyphens.')
    project = Path(project); path = project / '.env'; read_backend_env(path)
    lines = path.read_text(encoding='utf-8-sig').splitlines() if path.exists() else []
    lines = [line for line in lines if not re.match(r'^\s*(?:export\s+)?TWELVEDATA_API_KEY\s*=', line)]
    lines.append('TWELVEDATA_API_KEY=' + key.strip())
    descriptor, temporary = tempfile.mkstemp(prefix='.fx-config-', dir=project)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as output: output.write('\n'.join(lines) + '\n')
        os.chmod(temporary, 0o600); os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
