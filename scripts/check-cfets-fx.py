"""Verify fresh public CFETS inputs without writing the running server's cache."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT.parent/'.runtime'/'python'))
sys.path.insert(0,str(PROJECT))
from backend.cfets_fx import CFETSFXSource
from backend.config import load_backend_env


def main():
    load_backend_env(PROJECT)
    with TemporaryDirectory(prefix='cfets-fx-check-') as directory:
        source=CFETSFXSource(Path(directory)/'inputs.json')
        source.refresh(force=True); state=source.snapshot()
        print(json.dumps({'usable':state['usable'],'checks':state['checks'],
            'volatility_published_at':state['volatility']['published_at'] if state['volatility'] else None,
            'pricing_date':state['pricing']['date'] if state['pricing'] else None,
            'spot':state['pricing']['spot'] if state['pricing'] else None,
            'volatility_nodes':state['volatility']['nodes'] if state['volatility'] else [],
            'forward_nodes':state['pricing']['nodes'] if state['pricing'] else [],
            'warnings':state['warnings']},indent=2))
        return 0 if state['usable'] and not state['errors'] else 1


if __name__=='__main__':raise SystemExit(main())
