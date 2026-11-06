"""Exact published CGB YTM tenors from ChinaBond's public chart JSON API."""
from datetime import date, datetime, timedelta, timezone
import json
import math
from urllib.parse import urlencode
from .treasury_yields import TENORS, validate_rows

CURVE_ID = '2c9081e50a2f9606010a3068cae70001'
API = 'https://yield.chinabond.com.cn/cbweb-mn/yc/queryYz'
CURVE_API = 'https://yield.chinabond.com.cn/cbweb-mn/yc/searchYc'
START = '2006-03-01'
SHANGHAI = timezone(timedelta(hours=8))
TERMS = [('m3','3m','0.25'),('y2','2y','2.0'),('y5','5y','5.0'),('y10','10y','10.0'),('y30','30y','30.0')]


def history_url(first, last):
    return API+'?'+urlencode(dict(bjlx='no',dcq='0.25,3m;2,2y;5,5y;10,10y;30,30y;',
        startTime=first,endTime=last,qxlx='0,',yqqxN='N',yqqxK='K',par='day',ycDefIds=CURVE_ID+',',locale='en_US'))


def parse_history(body, first, last):
    dataset = json.loads(body)
    if not isinstance(dataset,list) or len(dataset)!=5: raise ValueError('ChinaBond must return exactly five YTM series.')
    expected = {CURVE_ID+suffix:(key,term) for key,term,suffix in TERMS}
    rows = {}; seen = set()
    for series in dataset:
        if not isinstance(series,dict) or not isinstance(series.get('ycDefName'),str): raise ValueError('Invalid ChinaBond series metadata.')
        identifier = series['ycDefId']
        if identifier not in expected or identifier in seen: raise ValueError('Wrong or duplicate ChinaBond curve identifier.')
        seen.add(identifier)
        key,term = expected[identifier]
        if series['ycDefName'].casefold() != f'ChinaBond Government Bond Yield Curve(YTM)({term})'.casefold():
            raise ValueError('Unexpected ChinaBond yield basis or maturity.')
        points = series['seriesData']
        if not isinstance(points,list) or len(points)>30000: raise ValueError('Invalid ChinaBond history length.')
        previous = None
        for point in points:
            if not isinstance(point,list) or len(point)!=2: raise ValueError('Invalid daily ChinaBond observation.')
            stamp,level = point
            if type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp!=int(stamp):
                raise ValueError('Invalid ChinaBond observation timestamp.')
            instant = datetime.fromtimestamp(stamp/1000,SHANGHAI)
            at = instant.date().isoformat()
            if instant.time()!=datetime.min.time() or not first<=at<=last or (previous is not None and stamp<=previous):
                raise ValueError('ChinaBond observation dates are invalid or outside the requested daily range.')
            if level is not None and (type(level) not in (int,float) or not math.isfinite(level)):
                raise ValueError('Invalid ChinaBond yield level.')
            previous = stamp
            row = rows.setdefault(at,{'date':at,**{item[0]:None for item in TENORS}})
            row[key] = level
    return validate_rows(list(rows.values()),date.fromisoformat(last))


def download(downloader, today):
    """Daily full-history revision check; recent native data is refreshed each minute."""
    last = today.isoformat()
    validate = lambda body:parse_history(body,START,last)
    full = validate(downloader.cached('china-api-history.json',history_url(START,last),validate))
    first = max(START,(today-timedelta(days=90)).isoformat())
    recent = parse_history(downloader.transport(history_url(first,last),data=b''),first,last)
    recent_dates = {row['date'] for row in recent}
    if not {row['date'] for row in full if row['date']>=first}.issubset(recent_dates):
        raise ValueError('ChinaBond recent history was truncated.')
    rows = [row for row in full if row['date']<first]+recent
    return validate_rows(rows,today),downloader.definition['source']
