"""Verify fresh downloads, country isolation and all displayed sovereign analytics."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from urllib.request import urlopen

FORMULAS = {'2s5s': {'y5':1,'y2':-1}, '2s10s':{'y10':1,'y2':-1},'3m10s':{'y10':1,'m3':-1},'5s30s':{'y30':1,'y5':-1},
            '5s10s':{'y10':1,'y5':-1},'10s30s':{'y30':1,'y10':-1},
            '2s5s10s':{'y5':2,'y2':-1,'y10':-1},'2s10s30s':{'y10':2,'y2':-1,'y30':-1}}
COUNTRIES = ['USA','CHN','JPN','GBR','CAN','SGP']


def check(code):
    with urlopen(f'http://127.0.0.1:8000/api/bonds/treasury?country={code}&refresh=true',timeout=180) as response:
        result = json.load(response)
        assert response.headers['Cache-Control']=='no-store'
    assert result['country']['code']==code
    assert result['status']=='ready' and not result['error'], f"{code}: {result.get('error')}"
    assert len(result['countries'])==6 and len(result['quotes'])==5 and len(result['metrics'])==len(FORMULAS)
    checked=0
    for row in result['rows']:
        for key,weights in FORMULAS.items():
            if any(row[leg] is None for leg in weights): assert row[key] is None
            else:
                expected=sum(Decimal(str(row[leg]))*weight for leg,weight in weights.items())*100
                assert abs(expected-Decimal(str(row[key])))<=Decimal('0.000001')
                checked+=1
    for quote in result['quotes']:
        if quote['series_id'] is None: assert quote['value'] is None
    return {'country':code,'source':result['source_route'],'latest':result['latest_observation_date'],
            'observations':len(result['rows']),'numeric_checks':checked,
            'yields':{quote['label']:quote['value'] for quote in result['quotes']},'checked_at':result['last_checked_at']}


if __name__=='__main__':
    with ThreadPoolExecutor(max_workers=6) as executor:
        results=list(executor.map(check,COUNTRIES))
    print(json.dumps(results,indent=2))
    print('PASS:',sum(item['numeric_checks'] for item in results),'same-date calculations across six official country feeds.')
