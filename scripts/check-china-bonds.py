"""Audit live Chinese bond plots against a separate official curve API response."""
from decimal import Decimal
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

FORMULAS={'2s5s':{'y5':1,'y2':-1},'2s10s':{'y10':1,'y2':-1},'3m10s':{'y10':1,'m3':-1},'5s30s':{'y30':1,'y5':-1},
          '5s10s':{'y10':1,'y5':-1},'10s30s':{'y30':1,'y10':-1},
          '2s5s10s':{'y5':2,'y2':-1,'y10':-1},'2s10s30s':{'y10':2,'y2':-1,'y30':-1}}
CURVE='2c9081e50a2f9606010a3068cae70001'


def main():
    with urlopen('http://127.0.0.1:8000/api/bonds/treasury?country=CHN&refresh=true',timeout=120) as response: market=json.load(response)
    assert market['status']=='ready' and not market['error']
    assert market['country']['code']=='CHN' and market['source_route']=='ChinaBond · CCDC API'
    assert market['poll_interval_seconds']==60 and len(market['quotes'])==5 and len(market['metrics'])==len(FORMULAS)
    assert len(market['curve'])==5 and all(item['value'] is not None for item in market['quotes']+market['metrics'])
    checked=0
    for row in market['rows']:
        for key,weights in FORMULAS.items():
            if any(row[leg] is None for leg in weights): assert row[key] is None
            else:
                expected=sum(Decimal(str(row[leg]))*weight for leg,weight in weights.items())*100
                assert abs(expected-Decimal(str(row[key])))<=Decimal('0.000001')
                checked+=1
    query=urlencode(dict(xyzSelect='txy',workTimes=market['curve_date'],dxbj='0',qxll='0,',
        yqqxN='N',yqqxK='K',ycDefIds=CURVE+',',wrjxCBFlag='0',locale='en_US'))
    request=Request('https://yield.chinabond.com.cn/cbweb-mn/yc/searchYc?'+query,data=b'',headers={'User-Agent':'Mozilla/5.0','Accept':'application/json'})
    with urlopen(request,timeout=25) as response: curves=json.load(response)
    assert len(curves)==1 and curves[0]['ycDefId']==CURVE and curves[0]['worktime']==market['curve_date']
    assert curves[0]['ycDefName']=='ChinaBond Government Bond Yield Curve(YTM)'
    points={Decimal(str(years)):Decimal(str(level)) for years,level in curves[0]['seriesData']}
    for quote in market['quotes']:
        assert quote['date']==market['curve_date']
        assert Decimal(str(quote['value']))==points[Decimal(str(quote['years']))]
    print('PASS: all five API yields match the separate official ChinaBond curve, including the exact 2Y point.')
    print('PASS:',checked,'same-date spread and butterfly calculations; one-minute automatic API checks.')
    print('Latest observation:',market['latest_observation_date'],'| API checked:',market['last_checked_at'])
    for item in market['quotes']:print(item['label'],item['value'],'percent')
    for item in market['metrics']:print(item['key'],item['value'],'bp')


if __name__=='__main__':main()
