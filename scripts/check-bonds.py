"""Check a fresh production download and independently recompute chart observations."""
from decimal import Decimal
import argparse
import json
from urllib.request import urlopen

FORMULAS = {'2s10s': {'y10':1,'y2':-1}, '3m10s': {'y10':1,'m3':-1},
            '5s30s': {'y30':1,'y5':-1}, '5s10s': {'y10':1,'y5':-1}, '10s30s': {'y30':1,'y10':-1},
            '2s5s10s': {'y5':2,'y2':-1,'y10':-1}, '2s10s30s': {'y10':2,'y2':-1,'y30':-1}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-api',action='store_true',help='Fail if the authenticated API was not used.')
    args=parser.parse_args()
    with urlopen('http://127.0.0.1:8000/api/bonds/treasury?refresh=true', timeout=40) as response:
        result=json.load(response)
    if result['status']!='ready' or result['error']: raise ValueError('No successful live source check.')
    if args.require_api and result['source_route']!='FRED API': raise ValueError('The authenticated API was not used.')
    if not result['rows'] or len(result['metrics'])!=7 or len(result['quotes'])!=5: raise ValueError('Incomplete source coverage.')
    count=0
    for row in result['rows']:
        for name,weights in FORMULAS.items():
            if any(row[key] is None for key in weights):
                if row[name] is not None: raise ValueError('A missing leg was filled.')
            else:
                expected=sum(Decimal(str(row[key]))*weight for key,weight in weights.items())*100
                if abs(expected-Decimal(str(row[name])))>Decimal('0.000001'):raise ValueError('Calculation does not match its source legs.')
                count+=1
    print('PASS: fresh FRED source check and',count,'same-date spread/butterfly calculations.')
    print('Source connection:',result['source_route'])
    print('Source checked:',result['last_checked_at'],'| latest observation:',result['latest_observation_date'])
    for item in result['quotes']:print(item['series_id'],item['value'],'percent |',item['date'])
    for item in result['metrics']:print(item['key'],item['value'],'bp |',item['date'])
    print('Frequency: official DAILY observations; this does not verify intraday quotes.')


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError,TypeError):raise SystemExit('FAIL: the live source or yield calculations could not be verified.') from None
