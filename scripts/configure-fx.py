"""Validate and save a local backend key without echoing it or changing Git."""
from getpass import getpass, GetPassWarning
import os
from pathlib import Path
import re
import sys
import warnings

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from backend.config import load_backend_env, save_twelvedata_key
from backend.twelvedata import TwelveDataService, TwelveDataError


def configure(project=PROJECT,prompt=None,probe=None):
    override=bool(os.environ.get('TWELVEDATA_API_KEY','').strip())
    try:load_backend_env(project)
    except (OSError,ValueError):
        print('Could not read local .env settings. Check syntax and permissions before setup.');return 1
    print('Yahoo FX requires no key. Twelve Data is an optional backup and supplies the pair directory.')
    if override:print('A process TWELVEDATA_API_KEY is set and will override .env. Update that setting as well if replacing it.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',GetPassWarning)
            key=(prompt or getpass)('Twelve Data API key (hidden; Enter to leave unchanged): ').strip()
    except GetPassWarning:
        print('Hidden input is unavailable. Run this command in an interactive terminal; no settings were changed.');return 1
    if not key:
        print('No changes. Start with python launch.py --restart.');return 0
    if not re.fullmatch(r'[A-Za-z0-9_-]+',key):
        print('Invalid key format. No settings were changed.');return 1
    try:
        if probe:probe(key)
        else:TwelveDataService(key_getter=lambda:key).initialize()
        save_twelvedata_key(key,project)
    except TwelveDataError as exc:
        print(f'Key was not saved: {exc}');return 1
    except (OSError,ValueError):
        print('Could not read or save local settings. Check .env syntax and file permissions; the key was not printed.');return 1
    print('Twelve Data accepted the key. Saved in this project\'s ignored .env file.')
    print('Keep .env private; configure each computer separately. Restart with: python launch.py --restart')
    return 0


if __name__=='__main__':
    try:sys.exit(configure())
    except (EOFError,KeyboardInterrupt):print('\nSetup cancelled; no key was saved.');sys.exit(1)
