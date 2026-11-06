"""Singapore API adapter over MAS's official daily SGS benchmark releases.

The upstream benchmark feed is a Web Forms statistical table, not a native
JSON API. 3M SGS T-bills were discontinued in 2013; no other rate replaces them.
"""
from datetime import datetime, timedelta, timezone
import json
from urllib.parse import urlencode
from .sovereign_yields import SG, Tables, parse_singapore
from .treasury_yields import validate_rows

KEYS=['y2','y5','y10','y30']
START='2000-01-01'
PREFIX='ctl00$ContentPlaceHolder1$'


def query(downloader,first,today):
    fields=Tables(downloader.transport(SG)).fields
    if not all(fields.get(key) for key in ('__VIEWSTATE','__EVENTVALIDATION')):
        raise ValueError('Missing official MAS benchmark form fields.')
    fields.update({PREFIX+'StartYearDropDownList':first[:4],PREFIX+'StartMonthDropDownList':str(int(first[5:7])),
                   PREFIX+'EndYearDropDownList':str(today.year),PREFIX+'EndMonthDropDownList':str(today.month),
                   PREFIX+'FrequencyDropDownList':'D',PREFIX+'DisplayButton':'Display'})
    # Keep the discontinued 3M column only to validate the table layout. The
    # parser deliberately never imports its historical values as current data.
    for field in ['ThreeMonthTreasuryBillYield','TwoYearBondYield','FiveYearBondYield','TenYearBondYield','ThirtyYearBondYield']:
        fields[PREFIX+field+'CheckBox']='on'
    body=downloader.transport(SG,data=urlencode(fields).encode())
    rows=validate_rows(parse_singapore(body),today,KEYS)
    if any(not first<=row['date']<=today.isoformat() or row['m3'] is not None for row in rows):
        raise ValueError('MAS returned dates or maturities outside the requested benchmark range.')
    return rows


def download(downloader,today):
    path=downloader.cache_dir/'singapore-sgs-history.json'
    saved=None
    try:
        candidate=json.loads(path.read_text(encoding='utf-8'))
        checked=datetime.fromisoformat(candidate['checked_at'])
        age=(datetime.now(timezone.utc)-checked).total_seconds()
        if candidate['schema']!=1 or checked.tzinfo is None or not 0<=age<86400:
            raise ValueError('Saved MAS source history requires a new check.')
        rows=validate_rows(candidate['rows'],today,KEYS)
        if any(row['m3'] is not None for row in rows): raise ValueError('Unexpected current Singapore 3M data.')
        saved=rows
    except (OSError,ValueError,KeyError,TypeError):pass
    if saved is None:
        rows=query(downloader,START,today)
        downloader.cache_dir.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'schema':1,'checked_at':datetime.now(timezone.utc).isoformat(),'rows':rows}),encoding='utf-8')
        temporary.replace(path)
    else:
        first=(today-timedelta(days=90)).replace(day=1).isoformat()
        recent=query(downloader,first,today)
        for key in KEYS:
            before={row['date'] for row in saved if row['date']>=first and row[key] is not None}
            after={row['date'] for row in recent if row[key] is not None}
            if not before.issubset(after): raise ValueError('Truncated recent Singapore benchmark history.')
        rows=[row for row in saved if row['date']<first]+recent
    return validate_rows(rows,today,KEYS),downloader.definition['source']
