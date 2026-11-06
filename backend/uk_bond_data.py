"""UK-only sovereign releases: DMO bill XML API and BoE nominal gilt curves.

BoE publishes its daily curve as workbooks, not an upstream API. This adapter
serves those observations with DMO's native XML API through the app's JSON API.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from http.client import HTTPException
import io
import json
import math
import re
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from .sovereign_yields import UK, blank, parse_uk_workbook
from .treasury_yields import validate_rows

BILL_API='https://www.dmo.gov.uk/data/XmlDataReport?reportCode=D2.2D'
BILL_REFERENCE='https://www.dmo.gov.uk/data/treasury-bills/tender-results/'
GILT_KEYS=['y2','y5','y10','y30']


def xml_date(raw):
    if not isinstance(raw,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T00:00:00',raw):
        raise ValueError('Invalid DMO observation date.')
    return datetime.fromisoformat(raw).date()


def parse_bills(body, today):
    if len(body)>12_000_000 or b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
        raise ValueError('Unexpected DMO XML declaration or size.')
    root=ET.fromstring(body)
    if root.tag!='Data' or len(root)>30000: raise ValueError('Invalid DMO bill API schema.')
    rows={}
    for instrument in root:
        if instrument.tag!='di' or 'TBILL_MATURITY_TYPE' not in instrument.attrib:
            raise ValueError('Invalid DMO bill API instrument.')
        if instrument.attrib['TBILL_MATURITY_TYPE']!='3 Months': continue
        maturity=xml_date(instrument.attrib['MATURITY_DATE'])
        tenders=instrument.findall('dd')
        if len(tenders)!=1: raise ValueError('Ambiguous DMO 3M tender.')
        tender=tenders[0]; auction=xml_date(tender.attrib['TENDER_DATE'])
        issues=tender.findall('sdd')
        if len(issues)!=1: raise ValueError('Ambiguous DMO 3M issue.')
        issued=xml_date(issues[0].attrib['ISSUE_DATE'])
        if not auction<=today or not auction<=issued<maturity or not 60<=(maturity-issued).days<=110:
            raise ValueError('Invalid UK 3M bill dates or maturity.')
        results=issues[0].findall('far')
        if len(results)!=1: raise ValueError('Missing or ambiguous DMO 3M tender result.')
        raw=results[0].attrib['AVERAGE_YIELD_PERCENT']
        if not re.fullmatch(r'-?\d+(?:\.\d+)?',raw): raise ValueError('Invalid DMO percentage yield.')
        level=float(raw)
        if not math.isfinite(level) or auction.isoformat() in rows:
            raise ValueError('Non-finite or duplicate DMO 3M yield.')
        if auction.year>=2000: rows[auction.isoformat()]=level
    if len(rows)<2: raise ValueError('Missing UK 3M tender history.')
    return rows


def gilt_history(downloader, today):
    """Daily history revisions plus the latest workbook at every source check."""
    def check_zip(body):
        with ZipFile(io.BytesIO(body)) as archive:
            if not any(re.fullmatch(r'GLC Nominal daily data_\d{4} to present\.xlsx',name) for name in archive.namelist()):
                raise ValueError('UK nominal government archive schema changed.')
    body=downloader.cached('uk-nominal.zip',UK+'glcnominalddata.zip',check_zip)
    modified=(downloader.cache_dir/'uk-nominal.zip').stat().st_mtime_ns
    if downloader.uk_history is None or modified!=downloader.uk_mtime:
        with ZipFile(io.BytesIO(body)) as archive:
            if sum(item.file_size for item in archive.infolist())>100_000_000: raise ValueError('Oversized UK gilt archive.')
            combined={row['date']:row for name in archive.namelist()
                      if (match:=re.fullmatch(r'GLC Nominal daily data_(\d{4}) to (?:\d{4}|present)\.xlsx',name)) and int(match[1])>=2000
                      for row in parse_uk_workbook(archive.read(name))}
        downloader.uk_history=validate_rows(list(combined.values()),today,GILT_KEYS)
        downloader.uk_mtime=modified
    combined={row['date']:dict(row) for row in downloader.uk_history}
    with ZipFile(io.BytesIO(downloader.transport(UK+'latest-yield-curve-data.zip'))) as archive:
        if sum(item.file_size for item in archive.infolist())>30_000_000: raise ValueError('Oversized UK current-month archive.')
        current=parse_uk_workbook(archive.read('GLC Nominal daily data current month.xlsx'))
        if not current: raise ValueError('Missing current UK gilt observations.')
        # A new month can legitimately contain one session; validate the merged history.
        for row in current:combined[row['date']]=row
    return validate_rows(list(combined.values()),today,GILT_KEYS)


def bill_snapshot(downloader, today):
    """Retain verified bill observations without blocking independent gilt updates."""
    path=downloader.cache_dir/'uk-bill-api.json'
    saved=None
    try:
        candidate=json.loads(path.read_text(encoding='utf-8'))
        checked=datetime.fromisoformat(candidate['checked_at'])
        levels=candidate['yields']
        if candidate['schema']!=1 or checked.tzinfo is None or checked>datetime.now(timezone.utc) or not isinstance(levels,dict) or len(levels)<2:
            raise ValueError('Invalid saved DMO API snapshot.')
        for stamp,level in levels.items():
            if not date(2000,1,1)<=date.fromisoformat(stamp)<=today or type(level) not in (int,float) or not math.isfinite(level):
                raise ValueError('Invalid saved DMO bill observation.')
        saved=candidate
    except (OSError,ValueError,KeyError,TypeError): pass
    try:
        levels=parse_bills(downloader.transport(BILL_API),today)
        if saved and not set(saved['yields']).issubset(levels): raise ValueError('Truncated DMO API history.')
        checked=datetime.now(timezone.utc).isoformat()
        candidate={'schema':1,'checked_at':checked,'yields':levels}
        downloader.cache_dir.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(candidate),encoding='utf-8');temporary.replace(path)
        return levels,{'status':'ready','last_checked_at':checked},None
    except (OSError,HTTPException,ValueError,KeyError,TypeError,ET.ParseError):
        warning='The UK DMO 3M bill API is unavailable. '+('Showing its last verified tender observations; daily gilt curves continue to update.' if saved else '3M and 3m10s are unavailable; daily gilt curves continue to update.')
        return (saved['yields'] if saved else {}),{'status':'stale' if saved else 'unavailable','last_checked_at':saved['checked_at'] if saved else None},warning


def download(downloader, today):
    with ThreadPoolExecutor(max_workers=2) as pool:
        gilts=pool.submit(gilt_history,downloader,today)
        bills=pool.submit(bill_snapshot,downloader,today)
        rows={row['date']:row for row in gilts.result()}
        levels,bill_status,warning=bills.result()
        for stamp,level in levels.items():rows.setdefault(stamp,blank(stamp))['m3']=level
    checks={'gilts':{'status':'ready','last_checked_at':datetime.now(timezone.utc).isoformat()},'bills':bill_status}
    return validate_rows(list(rows.values()),today,GILT_KEYS),downloader.definition['source'],{'source_warning':warning,'source_checks':checks}
