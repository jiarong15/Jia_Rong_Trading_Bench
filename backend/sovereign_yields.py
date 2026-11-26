"""Country-specific sovereign yields from official public statistical releases."""
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import io
import json
import math
import os
from pathlib import Path
import re
import ssl
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile
from .treasury_yields import TENORS, TreasuryYieldService, validate_rows
from . import china_bond_api

JP = 'https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme_all.csv'
JP_CURRENT = 'https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/jgbcme.csv'
CN = 'https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery'
UK = 'https://www.bankofengland.co.uk/-/media/boe/files/statistics/yield-curves/'
CA = 'https://www.bankofcanada.ca/valet/observations/group/'
SG = 'https://eservices.mas.gov.sg/Statistics/fdanet/BenchmarkPricesAndYields.aspx'
KEYS = [item[0] for item in TENORS]


def definition(code, name, label, currency, prefix, source, url, basis, ids, note):
    return dict(code=code, name=name, label=label, currency=currency, prefix=prefix,
                source=source, url=url, basis=basis, coverage_note=note,
                tenors=[(key, tenor, identifier, years) for (key, tenor, _, years), identifier in zip(TENORS, ids)])


COUNTRIES = [
    definition('USA', 'United States', 'USA', 'USD', 'US', 'FRED · Federal Reserve H.15',
               'https://www.federalreserve.gov/releases/h15/', 'Constant-maturity Treasury yields',
               [item[2] for item in TENORS], ''),
    definition('CHN', 'China', 'China', 'CNY', 'China', 'ChinaBond · CCDC API',
               'https://yield.chinabond.com.cn/cbweb-mn/yield_main?locale=en_US', 'Government bond yield-to-maturity curve',
               ['ChinaBond 3M', 'ChinaBond 2Y', 'ChinaBond 5Y', 'ChinaBond 10Y', 'ChinaBond 30Y'], ''),
    definition('JPN', 'Japan', 'Japan', 'JPY', 'Japan', 'Japan Ministry of Finance',
               'https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/index.htm', 'Daily JGB constant-maturity yields; 3M bill auction yields',
               ['MOF 3M bill auction', 'JGB 2Y', 'JGB 5Y', 'JGB 10Y', 'JGB 30Y'],
               '3M uses the published Treasury Discount Bill auction average-price yield (roughly weekly); 2Y–30Y use daily JGB constant-maturity yields. The full curve and 3m10s use matching auction dates only. These are published releases, not intraday quotes.'),
    definition('GBR', 'United Kingdom', 'UK', 'GBP', 'UK', 'Bank of England · UK DMO XML API',
               'https://www.bankofengland.co.uk/statistics/yield-curves', 'BoE nominal gilt spot yields · continuously compounded; 3M DMO bill tender yield',
               ['DMO 3M bill tender', 'BoE spot 2Y', 'BoE spot 5Y', 'BoE spot 10Y', 'BoE spot 30Y'],
               'UK data only: 3M is the DMO Treasury-bill average accepted yield (roughly weekly); 2Y–30Y are Bank of England fitted nominal gilt spot yields, continuously compounded and normally published by noon on the following business day. The full curve and 3m10s require matching tender dates. Published yields, not intraday quotes.'),
    definition('CAN', 'Canada', 'Canada', 'CAD', 'Canada', 'Bank of Canada · Valet JSON API',
               'https://www.bankofcanada.ca/rates/interest-rates/canadian-bonds/', 'Daily Government of Canada benchmark yields; 3M secondary-market Treasury-bill yield',
               ['TB.CDN.90D.MID', 'BD.CDN.2YR.DQ.YLD', 'BD.CDN.5YR.DQ.YLD', 'BD.CDN.10YR.DQ.YLD', 'BD.CDN.LONG.DQ.YLD'],
               'Canada data only: all five maturities use daily Bank of Canada Valet API observations. 3M is the average of sampled secondary-market Treasury-bill yields. The 30Y slot uses the designated long-term government benchmark bond; its remaining maturity varies. All calculations require matching dates. Published daily yields, not intraday quotes.'),
    definition('SGP', 'Singapore', 'Singapore', 'SGD', 'Singapore', 'MAS · official SGS benchmarks',
               SG, 'SGS benchmark closing yields',
               [None, 'SGS 2Y', 'SGS 5Y', 'SGS 10Y', 'SGS 30Y'],
               'Singapore data only: 2Y, 5Y, 10Y and 30Y use MAS daily SGS benchmark buying yields quoted by government securities dealers. The final 3M Treasury bill was issued on 20 June 2013; 3M and 3m10s are unavailable. Published daily closing yields, not intraday quotes.'),
]
BY_CODE = {item['code']: item for item in COUNTRIES}


def public_country(item):
    return {key: item[key] for key in ('code', 'name', 'label', 'currency', 'prefix', 'source', 'url', 'basis', 'coverage_note')}


def fetch_bytes(url, data=None):
    if urlsplit(url).hostname not in {'www.mof.go.jp', 'yield.chinabond.com.cn', 'www.bankofengland.co.uk',
                                     'www.bankofcanada.ca', 'eservices.mas.gov.sg', 'www.dmo.gov.uk'}:
        raise ValueError('Unexpected sovereign-yield source host.')
    context = ssl.create_default_context()
    configured = os.environ.get('BONDS_CA_FILE', '').strip()
    if configured:
        path = Path(configured).expanduser()
        context.load_verify_locations(cafile=path if path.is_absolute() else Path(__file__).resolve().parents[1]/path)
    headers = {'User-Agent':'Mozilla/5.0 (compatible; JRTradingWorkbench/1.0)',
               'Cache-Control':'no-cache', 'Accept':'*/*'}
    if data is not None: headers['Content-Type'] = 'application/x-www-form-urlencoded'
    limit = 50_000_000 if url == UK+'glcnominalddata.zip' else 12_000_000
    with urlopen(Request(url, data=data, headers=headers), context=context, timeout=25) as response:
        body = response.read(limit+1)
    if len(body) > limit: raise ValueError('Official yield download exceeded its size limit.')
    return body


def value(raw):
    if raw is None or str(raw).strip() in ('', '-', '–', '.', 'NA', 'N/A', '#N/A'): return None
    result = float(raw)
    if not math.isfinite(result): raise ValueError('Non-finite official yield.')
    return result


def blank(stamp):
    return {'date': stamp, **dict.fromkeys(KEYS)}


class Tables(HTMLParser):
    """Only tabular text and hidden form fields; scripts are never evaluated."""
    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.tables = []; self.fields = {}; self.table = None; self.row = None; self.cell = None
        self.feed(body.decode('utf-8-sig') if isinstance(body, bytes) else body)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('type') == 'hidden' and attrs.get('name'):
            self.fields[attrs['name']] = attrs.get('value', '')
        if tag == 'table': self.table = []
        elif tag == 'tr' and self.table is not None: self.row = []
        elif tag in ('td', 'th') and self.row is not None: self.cell = []
        elif tag == 'br' and self.cell is not None: self.cell.append(' ')

    def handle_data(self, text):
        if self.cell is not None: self.cell.append(text)

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split())); self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.table.append(self.row); self.row = None
        elif tag == 'table' and self.table is not None:
            self.tables.append(self.table); self.table = None


def parse_japan(body):
    try: text = body.decode('utf-8-sig')
    except UnicodeDecodeError: text = body.decode('cp932')
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith('Date,1Y,2Y,'))
    rows = []
    for record in csv.DictReader(lines[start:]):
        if not re.fullmatch(r'\d{4}/\d{1,2}/\d{1,2}',record['Date'] or ''): continue
        year, month, day = map(int, record['Date'].split('/'))
        if year < 2000: continue
        row = blank(date(year, month, day).isoformat())
        for key, tenor in [('y2','2Y'), ('y5','5Y'), ('y10','10Y'), ('y30','30Y')]: row[key] = value(record[tenor])
        rows.append(row)
    return rows


def parse_china(body, first, last):
    rows = []
    for table in Tables(body).tables:
        if not table or [cell.replace(' ','') for cell in table[0]] != ['YieldCurveName','Date','3M','6M','1Y','3Y','5Y','7Y','10Y','30Y']: continue
        for cells in table[1:]:
            if len(cells) != 10 or cells[0] != 'ChinaBond Government Bond Yield Curve': raise ValueError('Unexpected ChinaBond curve or schema.')
            if not first <= cells[1] <= last: raise ValueError('ChinaBond returned dates outside the requested period.')
            row = blank(date.fromisoformat(cells[1]).isoformat())
            for key, column in [('m3',2),('y5',6),('y10',8),('y30',9)]: row[key] = value(cells[column])
            rows.append(row)
    if not rows: raise ValueError('No government curve in the ChinaBond response.')
    return rows


def parse_canada(bonds, bills):
    rows = {}
    for body, identifiers in [(bonds, {'y2':'BD.CDN.2YR.DQ.YLD','y5':'BD.CDN.5YR.DQ.YLD','y10':'BD.CDN.10YR.DQ.YLD','y30':'BD.CDN.LONG.DQ.YLD'}),
                              (bills, {'m3':'V80691344'})]:
        dataset = json.loads(body)
        for identifier in identifiers.values():
            if identifier not in dataset['seriesDetail']: raise ValueError('Canadian yield series missing.')
        seen = set()
        for record in dataset['observations']:
            stamp = date.fromisoformat(record['d']).isoformat()
            if stamp in seen: raise ValueError('Duplicate Canadian yield date.')
            seen.add(stamp)
            row = rows.setdefault(stamp, blank(stamp))
            for key, identifier in identifiers.items(): row[key] = value(record.get(identifier, {}).get('v'))
    return list(rows.values())


def parse_singapore(body):
    rows = []
    months = {month:i+1 for i,month in enumerate(['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'])}
    for table in Tables(body).tables:
        if not table or '2-Year Bond Yield' not in ' '.join(table[0]) or '30-Year Bond Yield' not in ' '.join(table[0]): continue
        if not all(label in ' '.join(table[0]) for label in ['3-Month T-Bill Yield','5-Year Bond Yield','10-Year Bond Yield']):
            raise ValueError('MAS benchmark columns changed.')
        year = month = None
        for cells in table[1:]:
            if len(cells) != 8: raise ValueError('Unexpected MAS daily table schema.')
            if cells[0]: year = int(cells[0])
            if cells[1]: month = months[cells[1]]
            stamp = date(year, month, int(cells[2])).isoformat()
            row = blank(stamp)
            for key, column in [('y2',4),('y5',5),('y10',6),('y30',7)]: row[key] = value(cells[column])
            rows.append(row)
    if not rows: raise ValueError('MAS returned no daily benchmark yields.')
    return rows


XML = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'


def parse_uk_workbook(body):
    """Read only published cached cells at exact maturities; never fit/interpolate."""
    rows = {}
    with ZipFile(io.BytesIO(body)) as archive:
        infos = archive.infolist()
        if len(infos) > 1000 or sum(item.file_size for item in infos) > 80_000_000: raise ValueError('Oversized UK workbook.')
        def xml(name):
            content = archive.read(name)
            if b'<!DOCTYPE' in content.upper() or b'<!ENTITY' in content.upper(): raise ValueError('Unexpected XML declaration.')
            return ET.fromstring(content)
        strings = [''.join(item.itertext()) for item in xml('xl/sharedStrings.xml')] if 'xl/sharedStrings.xml' in archive.namelist() else []
        links = {item.attrib['Id']: item.attrib['Target'] for item in xml('xl/_rels/workbook.xml.rels') if item.attrib.get('TargetMode') != 'External'}
        sheets = {item.attrib['name']: links[item.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']] for item in xml('xl/workbook.xml').find(XML+'sheets')}
        for sheet, targets in [('4. spot curve', {'y2':2,'y5':5,'y10':10,'y30':30})]:
            if sheet not in sheets and '4. nominal spot curve' in sheets: sheet = '4. nominal spot curve'
            target = sheets[sheet]
            path = target.lstrip('/') if target.startswith('/') else 'xl/'+target
            if '..' in path or not path.startswith('xl/'): raise ValueError('Invalid UK sheet path.')
            columns = None
            for element in xml(path).findall(XML+'sheetData/'+XML+'row'):
                cells = {}
                for cell in element.findall(XML+'c'):
                    raw = cell.findtext(XML+'v'); kind = cell.attrib.get('t')
                    if kind == 's': raw = strings[int(raw)]
                    elif kind == 'e': raw = None
                    cells[re.match(r'[A-Z]+', cell.attrib['r'])[0]] = raw
                if element.attrib['r'] == '4':
                    columns = {key:next((column for column, raw in cells.items() if raw is not None and re.fullmatch(r'\d+(?:\.\d+)?',raw) and abs(float(raw)-years)<1e-7),None) for key,years in targets.items()}
                    if not all(columns[key] for key in ('y2','y5','y10')): raise ValueError('UK spot maturity headers changed.')
                    continue
                raw_date = cells.get('A')
                if not columns or not raw_date or not re.fullmatch(r'\d+(?:\.0+)?',raw_date): continue
                stamp = (date(1899,12,30)+timedelta(days=int(float(raw_date)))).isoformat()
                if stamp < '2000-01-01': continue
                row = rows.setdefault(stamp, blank(stamp))
                for key, column in columns.items(): row[key] = value(cells.get(column))
    return list(rows.values())


class CountryDownload:
    def __init__(self, definition, cache_dir, transport=None):
        self.definition = definition; self.cache_dir = Path(cache_dir); self.transport = transport or fetch_bytes
        self.uk_history = None; self.uk_mtime = None

    def cached(self, name, url, validator):
        path = self.cache_dir/name
        if path.exists() and time.time()-path.stat().st_mtime < 86400: return path.read_bytes()
        body = self.transport(url,data=b'') if url.startswith(china_bond_api.API+'?') else self.transport(url)
        validator(body)
        self.cache_dir.mkdir(parents=True,exist_ok=True)
        temporary = path.with_suffix('.tmp'); temporary.write_bytes(body); temporary.replace(path)
        return body

    def __call__(self, today):
        try:
            return self.download(today)
        except (BadZipFile, ET.ParseError, StopIteration, IndexError, OverflowError) as exc:
            raise ValueError('Official sovereign-yield response failed validation.') from None

    def download(self, today):
        code = self.definition['code']
        if code == 'JPN':
            from .japan_bond_data import download
            return download(self,today)
        elif code == 'CAN':
            from .canada_bond_api import download
            return download(self,today)
        elif code == 'CHN':
            return china_bond_api.download(self,today)
        elif code == 'SGP':
            from .singapore_bond_data import download
            return download(self,today)
        elif code == 'GBR':
            from .uk_bond_data import download
            return download(self,today)
        else: raise ValueError('Unsupported sovereign yield source.')
        required = [key for key,_,identifier,_ in self.definition['tenors'] if identifier]
        return validate_rows(rows,today,required), self.definition['source']


class SovereignYields:
    def __init__(self, us_service, directory, enabled=True, transport=None):
        self.services = {'USA':us_service}
        for item in COUNTRIES[1:]:
            downloader = CountryDownload(item,Path(directory)/'sources',transport)
            self.services[item['code']] = TreasuryYieldService(Path(directory)/(item['code'].lower()+'.json'),
                enabled=enabled,api_key='',definition=item,downloader=downloader)

    def view(self, country='USA', refresh=False, saved_only=False):
        item = BY_CODE[country]
        result = self.services[country].view(refresh,saved_only=saved_only)
        if country == 'JPN':
            result['frequency'] = 'Daily JGB yields; auction-date 3M bill yields'
            for source in result['sources']:
                if source['tenor']=='3M': source['url']='https://www.mof.go.jp/english/policy/jgbs/auction/past_auction_results/index.html'
        if country == 'GBR':
            result['frequency'] = 'Daily gilt spot yields; tender-date 3M bill yields'
            for source in result['sources']:
                if source['tenor']=='3M': source['url']='https://www.dmo.gov.uk/data/treasury-bills/tender-results/'
        if country == 'CAN':
            for source in result['sources']:
                if source['tenor']=='3M': source['url']='https://www.bankofcanada.ca/rates/interest-rates/money-market-yields/'
        return {**result, 'country':public_country(item), 'countries':[public_country(item) for item in COUNTRIES],
                'coverage_note':item['coverage_note'], 'yield_basis':item['basis'],
                'notice':'Official sovereign yields on their published dates. Spreads and butterflies require same-date legs; missing maturities remain unavailable. Unweighted yield butterflies, not DV01-neutral returns.'}
