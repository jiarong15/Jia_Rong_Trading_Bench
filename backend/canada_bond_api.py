"""Exact daily Canadian sovereign yields from the Bank of Canada Valet JSON API."""
from datetime import date, timedelta
import json
import math
from urllib.parse import urlencode
from .treasury_yields import TENORS, validate_rows

API='https://www.bankofcanada.ca/valet/observations/'
START='2000-01-01'
SERIES=[('m3','TB.CDN.90D.MID','Treasury bills, 3-month'),
        ('y2','BD.CDN.2YR.DQ.YLD','Benchmark bond yield, 2-year'),
        ('y5','BD.CDN.5YR.DQ.YLD','Benchmark bond yield, 5-year'),
        ('y10','BD.CDN.10YR.DQ.YLD','Benchmark bond yield, 10-year'),
        ('y30','BD.CDN.LONG.DQ.YLD','Benchmark bond yield, long-term')]


def history_url(first,last):
    return API+','.join(identifier for _,identifier,_ in SERIES)+'/json?'+urlencode({'start_date':first,'end_date':last})


def parse_history(body,first,last):
    dataset=json.loads(body)
    details=dataset['seriesDetail']
    if not isinstance(details,dict) or set(details)!={identifier for _,identifier,_ in SERIES}:
        raise ValueError('Valet must return exactly the five requested Canadian series.')
    for _,identifier,label in SERIES:
        series=details[identifier]
        if (series.get('label')!=label or series.get('dateFormat')!='daily'
            or series.get('dimension')!={'key':'d','name':'Date'}
            or series.get('metadata',{}).get('frequency')!='Business daily'
            or series.get('metadata',{}).get('unit')!='Percent'):
            raise ValueError('Unexpected Canadian yield maturity, units or publication frequency.')
    points=dataset['observations']
    if not isinstance(points,list) or len(points)>30000: raise ValueError('Invalid Canadian API history length.')
    rows=[];seen=set()
    for point in points:
        stamp=date.fromisoformat(point['d']).isoformat()
        if stamp in seen or not first<=stamp<=last: raise ValueError('Duplicate or out-of-range Canadian observation date.')
        seen.add(stamp)
        row={'date':stamp,**dict.fromkeys(item[0] for item in TENORS)}
        for key,identifier,_ in SERIES:
            item=point.get(identifier)
            raw=item['v'] if item is not None else None
            if raw is not None and not isinstance(raw,str): raise ValueError('Invalid Canadian API yield type.')
            level=None if raw is None or raw.strip() in ('','NA','N/A','-','.') else float(raw)
            if level is not None and not math.isfinite(level): raise ValueError('Non-finite Canadian API yield.')
            row[key]=level
        rows.append(row)
    return validate_rows(rows,date.fromisoformat(last))


def download(downloader,today):
    """Recheck full history daily and the latest 90 days each minute, without filling gaps."""
    last=today.isoformat()
    validate=lambda body:parse_history(body,START,last)
    full=validate(downloader.cached('canada-daily-api.json',history_url(START,last),validate))
    first=max(START,(today-timedelta(days=90)).isoformat())
    recent=parse_history(downloader.transport(history_url(first,last)),first,last)
    # Missing individual tenor cells are also meaningful: never preserve an old
    # value behind a new missing cell, or accept a truncated maturity history.
    for key,_,_ in SERIES:
        before={row['date'] for row in full if row['date']>=first and row[key] is not None}
        after={row['date'] for row in recent if row[key] is not None}
        if not before.issubset(after): raise ValueError('Truncated recent Canadian tenor history.')
    return validate_rows([row for row in full if row['date']<first]+recent,today),downloader.definition['source']
