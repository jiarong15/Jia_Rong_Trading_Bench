"""China BOP: SAFE workbooks, NBS public API and BIS's renminbi index.

Amounts are SAFE's published USD billions. CA/GDP uses SAFE's separately
published RMB balance and NBS RMB nominal GDP, without an estimated FX rate.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import ssl
import subprocess
from threading import RLock
import time
from urllib.error import URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
from .bop import BOPError, COUNTRIES, FIELDS, aggregate, numeric, quarter_index, quarter_label
from .bop_macro import build_quarterly_trade_view, monthly_index, parse_csv
from .public_workbooks import read_xlsx_sheet

SNAPSHOT = Path(__file__).parent / 'snapshots' / 'china_bop.json'
SAFE_PAGES = {
    'bop': 'https://www.safe.gov.cn/en/2019/0329/1496.html',
    'positions': 'https://www.safe.gov.cn/en/2018/0928/1459.html',
    'debt': 'https://www.safe.gov.cn/en/2018/0329/1412.html',
    'trade': 'https://www.safe.gov.cn/safe/2018/0427/8886.html',
}
NBS_URL = 'https://data.stats.gov.cn/dg/website/publicrelease/web/external/stream/esData'
NBS_IDS = {
    'gdp': {'gdp_cny': 'd22612f09aeb4241bc557ef0ac61b3ba'},
    'customs': {'exports_ytd': 'a349b30535404467b37e68871e99762c', 'imports_ytd': '6545926705124075961bf28602fd50bb'},
}
CURRENCY_URL = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id=NBCNBIS&cosd=1998-01-01'
GROUP_NAMES = {'bop': 'SAFE balance of payments', 'positions': 'SAFE reserve positions',
               'debt': 'SAFE external debt', 'trade': 'SAFE monthly goods and services trade',
               'gdp': 'NBS nominal quarterly GDP', 'customs': 'GACC customs trade via NBS',
               'currency': 'BIS nominal effective renminbi index via FRED'}
BOP_LABELS = {
    'current_account': r'1\. Current account', 'goods': r'1\.A\.a Goods',
    'services': r'1\.A\.b Services', 'primary_income': r'1\.B Primary income',
    'secondary_income': r'1\.C Secondary income', 'capital_account': r'2\.1 Capital account',
    'financial_account': r'2\.2 Financial account', 'statistical_discrepancy': r'3\.\s*Net errors and omissions',
    'direct_assets': r'2\.2\.1\.1\.1 Assets', 'direct_liabilities': r'2\.2\.1\.1\.2 Liabilities',
    'equity_assets': r'2\.2\.1\.2\.1\.1 Equity and investment fund shares',
    'equity_liabilities': r'2\.2\.1\.2\.2\.1 Equity and investment fund shares',
    'debt_assets': r'2\.2\.1\.2\.1\.2 Debt securities',
    'debt_liabilities': r'2\.2\.1\.2\.2\.2 Debt securities',
    'other_assets': r'2\.2\.1\.4\.1 Assets', 'other_liabilities': r'2\.2\.1\.4\.2 Liabilities',
    'reserve_transactions': r'2\.2\.2 Reserve assets',
}


def fetch_china_bytes(url, payload=None):
    allowed = {'www.safe.gov.cn', 'data.stats.gov.cn', 'fred.stlouisfed.org'}
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.netloc not in allowed:
        raise BOPError('China source URL is outside the official-source allowlist.')
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; MeridianLocalDashboard/1.0)',
               'Accept': 'application/json,text/csv,text/html,*/*', 'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}
    if data is not None:
        headers.update({'Content-Type': 'application/json', 'Referer': 'https://data.stats.gov.cn/dg/website/page.html'})
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=30) as response:
            body = response.read(8_000_001)
    except URLError as exc:
        # Schannel uses Windows' trusted certificate store. No TLS verification
        # is disabled, including for SAFE's file-download certificate chain.
        if os.name != 'nt' or parts.netloc != 'www.safe.gov.cn' or not isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise
        command = ['curl.exe', '--fail', '--silent', '--show-error', '--location', '--proto', '=https',
                   '--proto-redir', '=https', '--max-redirs', '3', '--max-time', '30', '--max-filesize', '8000000',
                   '--user-agent', headers['User-Agent'], '--header', 'Cache-Control: no-cache', '--header', 'Pragma: no-cache', url]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise BOPError('The official SAFE download failed HTTPS verification or returned an error.')
        body = result.stdout
    if len(body) > 8_000_000:
        raise BOPError('China source exceeded the download limit.')
    return body


class WorkbookLinks(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.page, self.links = page, []

    def handle_starttag(self, tag, attrs):
        if tag != 'a':
            return
        link = urljoin(self.page, dict(attrs).get('href', ''))
        parts = urlsplit(link)
        if parts.scheme == 'https' and parts.netloc == 'www.safe.gov.cn' and re.fullmatch(r'/(en|safe)/file/file/\d{8}/[a-f0-9]+\.xlsx', parts.path):
            self.links.append(link)


def workbook_link(html, page):
    parser = WorkbookLinks(page)
    parser.feed(html)
    links = list(dict.fromkeys(parser.links))
    if len(links) != 1:
        raise BOPError('The current SAFE workbook link is missing or ambiguous.')
    return links[0]


def clean(value):
    return ' '.join(str('' if value is None else value).replace('\xa0', ' ').split())


def cell_number(value):
    if value is None or clean(value) in ('', '/', '--', '—', '…'):
        return None
    return numeric(value)


def columns(rows, kind='quarter'):
    for index, row in enumerate(rows[:8]):
        found = {}
        for column, value in enumerate(row):
            if value is None:
                continue
            text = clean(value)
            if kind == 'quarter':
                match = re.fullmatch(r'(\d{4})Q([1-4])', text)
                if match:
                    found[column] = f'{match[1]}-{(int(match[2])-1)*3+1:02d}-01'
            elif kind == 'month':
                match = re.fullmatch(r'(\d{4})[.\-/](\d{1,2})(?:-(\d{1,2}))?', text)
                if match:
                    found[column] = date(int(match[1]), int(match[3] or match[2]), 1).isoformat()
            else:
                parsed = None
                for pattern in ('%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%m-%d-%Y', '%m/%d/%Y'):
                    try:
                        parsed = datetime.strptime(text, pattern).date()
                        break
                    except ValueError:
                        pass
                if parsed is None and re.fullmatch(r'\d{5}(?:\.0+)?', text):
                    parsed = (datetime(1899, 12, 30) + timedelta(days=float(text))).date()
                if parsed is not None and parsed.month in (3, 6, 9, 12):
                    import calendar
                    if parsed.day == calendar.monthrange(parsed.year, parsed.month)[1]:
                        found[column] = f'{parsed.year}-{parsed.month-2:02d}-01'
        if found:
            if len(set(found.values())) != len(found):
                raise BOPError('SAFE returned duplicate reporting periods.')
            return index, found
    raise BOPError('SAFE spreadsheet reporting dates are unavailable.')


def select_row(rows, pattern):
    selected = [(i, row) for i, row in enumerate(rows) if row and re.fullmatch(pattern, clean(row[0]), re.I)]
    if len(selected) != 1:
        raise BOPError('A requested SAFE account row is missing or duplicated.')
    return selected[0]


def parse_bop_workbook(body):
    usd = read_xlsx_sheet(body, 'quarterly(USD)')
    rmb = read_xlsx_sheet(body, 'quarterly(RMB)')
    if not any('100 million of US dollars' in clean(cell) for row in usd[:4] for cell in row) or not any('100 million of Renminbi Yuan' in clean(cell) for row in rmb[:4] for cell in row):
        raise BOPError('SAFE BOP units changed.')
    _, dates = columns(usd)
    indices = {field: select_row(usd, pattern)[0] for field, pattern in BOP_LABELS.items()}
    primary_index = indices['primary_income']
    if clean(usd[primary_index + 1][0]) != 'Credit' or clean(usd[primary_index + 2][0]) != 'Debit':
        raise BOPError('SAFE primary-income credit/debit layout changed.')
    indices.update(income_receipts=primary_index + 1, income_payments=primary_index + 2)
    _, cny_dates = columns(rmb)
    _, current_cny = select_row(rmb, BOP_LABELS['current_account'])
    cny = {at: cell_number(current_cny[column]) for column, at in cny_dates.items()}
    rows = []
    for column, at in dates.items():
        row = {'date': at, 'period': quarter_label(at)}
        for field, index in indices.items():
            value = cell_number(usd[index][column]) if column < len(usd[index]) else None
            # SAFE assets/debits are signed as outflows. Canonical acquisition
            # and payments are positive; liabilities keep SAFE's own net sign.
            reverse = field.endswith('_assets') or field in ('financial_account', 'income_payments', 'reserve_transactions')
            row[field] = (-value if reverse else value) / 10 if value is not None else None
        row['current_account_cny'] = cny[at] / 10 if cny.get(at) is not None else None
        if any(row[field] is not None for field in ('current_account', 'goods', 'services', 'primary_income', 'secondary_income')):
            row['net_lending'] = row['current_account'] + row['capital_account'] if row['current_account'] is not None and row['capital_account'] is not None else None
            row['net_financial_inflow'] = -row['financial_account'] if row['financial_account'] is not None else None
            rows.append(row)
    rows.sort(key=lambda row: row['date'])
    validate_china_accounts(rows)
    return rows


def validate_china_accounts(rows):
    identities = {
        'current_account_components': ('current_account', (('goods', 1), ('services', 1), ('primary_income', 1), ('secondary_income', 1))),
        'primary_income': ('primary_income', (('income_receipts', 1), ('income_payments', -1))),
        'financial_account': ('financial_account', (('current_account', 1), ('capital_account', 1), ('statistical_discrepancy', 1))),
    }
    checks = []
    for name, (total, terms) in identities.items():
        differences = [abs(row[total] - sum(row[key] * sign for key, sign in terms)) for row in rows
                       if row.get(total) is not None and all(row.get(key) is not None for key, _ in terms)]
        maximum = max(differences, default=0)
        if maximum > .000001:
            raise BOPError('SAFE BOP accounting identities do not reconcile.')
        checks.append({'name': name, 'observations_checked': len(differences), 'max_difference_usd_millions': maximum * 1000})
    return {'status': 'passed', 'tolerance_usd_millions': .001, 'checks': checks}


def parse_positions_workbook(body):
    rows = read_xlsx_sheet(body, 'Quarterly(USD)')
    if not any('100 million of US dollars' in clean(value) for row in rows[:3] for value in row):
        raise BOPError('SAFE reserve-position units changed.')
    header, dates = columns(rows, 'date')
    _, reserves = select_row(rows, r'5 Reserve assets')
    for column,value in enumerate(reserves):
        if column and cell_number(value) is not None and column not in dates:
            raise BOPError('A SAFE reserve position has an unrecognized reporting date.')
    result = []
    for column, at in dates.items():
        value = cell_number(reserves[column]) if column < len(reserves) else None
        if value is not None:
            if value <= 0:
                raise BOPError('SAFE reserve assets must be a positive stock.')
            result.append({'date': at, 'reserve_stock': value / 10})
    if not result:
        raise BOPError('SAFE reserve stocks are unavailable.')
    return sorted(result, key=lambda row: row['date'])


def parse_debt_workbook(body):
    rows = read_xlsx_sheet(body, 'Sheet1')
    if not any('100 million US dollars' in clean(value) for row in rows[:2] for value in row):
        raise BOPError('SAFE debt units changed.')
    _, dates = columns(rows)
    short = [row for row in rows if len(row) > 1 and clean(row[1]) == 'Short-term']
    if len(short) != 4:
        raise BOPError('SAFE sectoral short-term debt layout changed.')
    result = []
    for column, at in dates.items():
        parts = [cell_number(row[column]) if column < len(row) else None for row in short]
        if all(value is not None for value in parts):
            if any(value < 0 for value in parts) or sum(parts) <= 0:
                raise BOPError('SAFE returned invalid external debt positions.')
            result.append({'date': at, 'short_term_external_debt': sum(parts) / 10})
    if not result:
        raise BOPError('SAFE short-term debt is unavailable.')
    return sorted(result, key=lambda row: row['date'])


def parse_trade_workbook(body):
    rows = read_xlsx_sheet(body, '按美元计值', formatted_header_rows=4)
    if not any('亿美元' in clean(value) for row in rows[:4] for value in row):
        raise BOPError('SAFE monthly trade units changed.')
    header, dates = columns(rows, 'month')
    index, balance = select_row(rows, r'货物和服务贸易差额')
    if clean(rows[index+1][0]) != '贷方' or clean(rows[index+2][0]) != '借方':
        raise BOPError('SAFE monthly trade credit/debit layout changed.')
    result = []
    for column, at in dates.items():
        values = [cell_number(row[column]) if column < len(row) else None for row in (balance, rows[index+1], rows[index+2])]
        if all(value is not None for value in values):
            net, credit, debit = values
            if credit <= 0 or debit >= 0 or abs(net - credit - debit) > .00001:
                raise BOPError('SAFE monthly trade values do not reconcile.')
            label=clean(rows[header][column])
            combined=re.fullmatch(r'(\d{4})\.1-2',label)
            result.append({'date': at, 'period': at[:7], 'span_months':2 if combined else 1,
                           'exports': credit/10, 'imports': -debit/10, 'trade_balance': net/10})
    if not result:
        raise BOPError('SAFE monthly trade is unavailable.')
    return sorted(result, key=lambda row: row['date'])


def china_trade_view(monthly,frequency):
    quarters={}
    for row in monthly:
        end=monthly_index(row['date'])
        span=row.get('span_months',1)
        if span not in (1,2) or span==2 and end%12!=1:
            raise BOPError('Unexpected combined-month SAFE reporting period.')
        indices=set(range(end-span+1,end+1))
        quarter=end//3
        group=quarters.setdefault(quarter,{'months':set(),'exports':0,'imports':0})
        if group['months'] & indices or any(index//3!=quarter for index in indices):
            raise BOPError('SAFE trade reporting periods overlap or cross quarters.')
        group['months'].update(indices)
        for field in ('exports','imports'):group[field]+=row[field]
    complete={quarter:{key:group[key] for key in ('exports','imports')} for quarter,group in quarters.items()
              if group['months']==set(range(quarter*3,quarter*3+3))}
    return build_quarterly_trade_view(complete,frequency)


def nbs_parameters(group):
    now = datetime.now(timezone.utc)
    quarterly = group == 'gdp'
    suffix = 'SS' if quarterly else 'MM'
    end = (now.month-1)//3+1 if quarterly else now.month
    return {'cid': '28d936104e304aa191e338eb82b6dc09' if quarterly else '7e11b47c828d4e4e925f1c5a98305558',
            'rootId': '1b1ce0cfd03646dc9e15103ea4c570f6' if quarterly else '73f0f0fafb31475ab48a309cc25dedd9',
            'indicatorIds': list(NBS_IDS[group].values()), 'daCatalogId': '',
            'das': [{'text': '全国', 'value': '000000000000'}],
            'dts': [f'201001{suffix}-{now.year}{end:02d}{suffix}'], 'showType': 1}


def parse_nbs(body, group):
    payload = json.loads(body)
    if payload.get('success') is not True or not isinstance(payload.get('data'), list):
        raise BOPError('The public NBS request failed.')
    ids = {code: field for field, code in NBS_IDS[group].items()}
    quarterly = group == 'gdp'
    unit, suffix, divisor = ('亿元', 'SS', 10) if quarterly else ('千美元', 'MM', 1_000_000)
    result, seen = [], set()
    for item in payload['data']:
        match = re.fullmatch(r'(\d{4})(\d{2})' + suffix, str(item.get('code', '')))
        if not match:
            raise BOPError('NBS dates do not match the requested native frequency.')
        year, part = int(match[1]), int(match[2])
        if quarterly and part not in (1,2,3,4):
            raise BOPError('NBS returned an invalid quarter.')
        at = date(year, (part-1)*3+1 if quarterly else part, 1).isoformat()
        if at in seen:
            raise BOPError('NBS returned duplicate periods.')
        seen.add(at)
        values = item.get('values', [])
        if len(values) != len(ids) or {value.get('_id') for value in values} != set(ids):
            raise BOPError('A requested NBS indicator is missing or duplicated.')
        row = {'date': at}
        for value in values:
            if value.get('du_name') != unit or value.get('da') != '000000000000':
                raise BOPError('NBS returned different units or a region other than mainland China.')
            label = value.get('i_showname', '')
            if quarterly and '当季值' not in label or not quarterly and '累计值' not in label:
                raise BOPError('NBS changed the quarterly/cumulative indicator definition.')
            number = cell_number(value.get('value'))
            if number is not None and number <= 0:
                raise BOPError('NBS GDP and trade levels must be positive.')
            row[ids[value['_id']]] = number / divisor if number is not None else None
        if any(row[field] is not None for field in ids.values()):
            result.append(row)
    if not result:
        raise BOPError('NBS returned no usable observations.')
    return sorted(result, key=lambda row: row['date'])


def customs_quarters(monthly):
    lookup = {monthly_index(row['date']): row for row in monthly}
    result = {}
    for index, original in lookup.items():
        if index % 3 != 2:
            continue
        previous = lookup.get(index-3, {}) if index % 12 != 2 else {'exports_ytd': 0, 'imports_ytd': 0}
        row = {}
        for kind in ('exports','imports'):
            latest, prior = original.get(kind+'_ytd'), previous.get(kind+'_ytd')
            row['customs_'+kind] = latest-prior if latest is not None and prior is not None else None
            if row['customs_'+kind] is not None and row['customs_'+kind] <= 0:
                raise BOPError('NBS cumulative customs amounts do not form positive quarterly flows.')
        row['customs_goods'] = row['customs_exports']-row['customs_imports'] if all(value is not None for value in row.values()) else None
        at = f'{index//12}-{index%12-1:02d}-01'
        result[at] = row
    return result


def selected_sum(window, field):
    values = [row.get(field) for row in window]
    return sum(values) if values and all(value is not None for value in values) else None


def build_china_view(snapshot, frequency='trailing4q'):
    if frequency not in ('quarterly','trailing4q'):
        raise ValueError('Choose quarterly or trailing4q.')
    groups = snapshot['groups']
    if not groups.get('bop', {}).get('rows'):
        raise BOPError('No verified China BOP data is available. Refresh data to retry SAFE.')
    customs = customs_quarters(groups.get('customs', {}).get('rows', []))
    base = []
    for original in groups['bop']['rows']:
        if all(original.get(key) is not None for key in ('current_account','goods','services','primary_income','secondary_income')):
            row = {**{field: None for field in FIELDS}, **original, **customs.get(original['date'], {})}
            row['goods_gap'] = row['customs_goods']-row['goods'] if row['customs_goods'] is not None else None
            base.append(row)
    validation = validate_china_accounts(base)
    rows = aggregate(base, frequency)
    if not rows:
        raise BOPError('No complete China BOP periods are available.')
    periods = 4 if frequency == 'trailing4q' else 1
    positions = {row['date']: row for row in groups.get('positions', {}).get('rows', [])}
    debts = {row['date']: row for row in groups.get('debt', {}).get('rows', [])}
    gdp = {row['date']: row for row in groups.get('gdp', {}).get('rows', [])}
    currency = {monthly_index(row['date']): row['NBCNBIS'] for row in groups.get('currency', {}).get('rows', [])}
    macro = []
    for i, original in enumerate(base):
        at = original['date']
        window = base[i-periods+1:i+1] if i+1 >= periods else []
        if [quarter_index(row['date']) for row in window] != list(range(quarter_index(at)-periods+1,quarter_index(at)+1)):
            window = []
        row = {'date': at, 'period': quarter_label(at)}
        numerator = selected_sum(window,'current_account_cny')
        denominator = selected_sum([gdp.get(item['date'],{}) for item in window], 'gdp_cny')
        row['current_account_gdp_pct'] = 100*numerator/denominator if numerator is not None and denominator is not None and denominator>0 else None
        for kind in ('direct','equity','debt','other'):
            assets, liabilities = selected_sum(window,kind+'_assets'),selected_sum(window,kind+'_liabilities')
            row['net_'+kind] = liabilities-assets if liabilities is not None and assets is not None else None
        row['reserve_transactions'] = selected_sum(window,'reserve_transactions')
        stock, debt = positions.get(at,{}).get('reserve_stock'),debts.get(at,{}).get('short_term_external_debt')
        row['reserve_stock'],row['short_term_external_debt'] = stock,debt
        row['reserves_debt_pct'] = 100*stock/debt if stock is not None and debt is not None and debt>0 else None
        start = monthly_index(window[0]['date']) if window else 0
        values = [currency.get(index) for index in range(start,start+3*periods)] if window else []
        row['currency_index'] = sum(values)/len(values) if values and all(value is not None for value in values) else None
        macro.append(row)
    latest, contributors = rows[-1],base[-periods:]
    sources = [{'group': name, 'name': GROUP_NAMES[name], 'url': group['url'],
                'download_url': group.get('download_url',group['url']), 'retrieved_at': group['checked_at'],
                'checked_at': group['checked_at'], 'latest_observation': group['rows'][-1]['date'],
                'series': group.get('series',[])} for name,group in groups.items() if group.get('rows')]
    monthly = deepcopy(groups.get('trade',{}).get('rows',[]))
    return {'country': deepcopy(next(item for item in COUNTRIES if item['code']=='CHN')),
            'countries': deepcopy(COUNTRIES), 'frequency': frequency, 'status': 'ready',
            'reporting_currency': 'USD', 'units': 'USD billions', 'seasonal_adjustment': 'Not seasonally adjusted',
            'retrieved_at': groups['bop']['checked_at'], 'last_successful_check_at': groups['bop']['checked_at'],
            'reference_url': SAFE_PAGES['bop'], 'series': rows, 'latest': latest,
            'contributing_quarters': deepcopy(contributors), 'latest_quarter': deepcopy(base[-1]),
            'period_start': contributors[0]['period'], 'period_end': latest['period'],
            'validation': validation, 'insights': [], 'sources': sources,
            'methodology': [
                'SAFE publishes USD and RMB BOP tables. Displayed amounts use published USD billions, without currency conversion. BOP, investment and monthly trade levels are NSA.',
                'SAFE signs asset acquisition as an outflow. Asset acquisition, payments and reserve acquisition are reversed to positive-use conventions. Net inward = liabilities incurred minus acquisition of foreign assets; financial account is assets minus liabilities.',
                'CA/GDP uses SAFE published RMB current account / matching NBS nominal GDP in RMB. NBS levels are actual quarter amounts, not annual rates. No division by four or estimated FX conversion.',
                'GACC customs trade via NBS uses monthly year-to-date USD amounts. Each calendar quarter is quarter-end YTD minus prior same-year quarter-end YTD. January/February combined releases are not split or estimated. Customs imports are CIF, BOP imports FOB; ownership, processing/merchanting, timing and revisions also differ.',
                'Reserve coverage = SAFE IIP total reserve assets including gold / SAFE short-term external debt of government, central bank, banks and other sectors. Original maturity; excludes intercompany lending with no maturity split in the sector table. Stocks are never summed.',
                'The BIS broad nominal effective renminbi index (NBCNBIS, 2020=100) is averaged over three/twelve complete months. Higher means a stronger renminbi. These are not US dollar index observations.',
                'SAFE monthly goods and services form complete calendar quarters, then optional four-quarter sums. Moving averages cover three plotted quarters; YoY compares matching selected totals. Partial quarters and missing inputs remain unavailable.',
            ],
            'macro': {'status':'ready','country_code':'CHN','currency':'USD','frequency':frequency,
                      'quarterly':macro,'monthly':monthly,'trade':china_trade_view(monthly,frequency),
                      'sources':sources,'warnings':snapshot.get('warnings',[])}}


class ChinaBOPService:
    def __init__(self,cache_path,enabled=True,transport=None):
        self.cache_path,self.enabled = Path(cache_path),enabled
        self.transport = transport or fetch_china_bytes
        self.lock = RLock()
        self.snapshot = {'schema_version':1,'country':'CHN','groups':{},'warnings':[]}
        self.last_completed,self.last_failure,self.last_attempt_at = None,None,None
        for path in (self.cache_path,SNAPSHOT):
            try:
                candidate = json.loads(path.read_text(encoding='utf-8'))
                if candidate['schema_version']!=1 or candidate['country']!='CHN':continue
                build_china_view(candidate)
                self.snapshot=candidate
                break
            except (OSError,ValueError,KeyError,TypeError,BOPError):continue

    def _download(self,name):
        metadata,extra = [],{}
        if name in SAFE_PAGES:
            url=SAFE_PAGES[name]
            link=workbook_link(self.transport(url).decode('utf-8-sig'),url)
            body=self.transport(link)
            parser={'bop':parse_bop_workbook,'positions':parse_positions_workbook,'debt':parse_debt_workbook,'trade':parse_trade_workbook}[name]
            rows=parser(body)
            extra['download_url']=link
        elif name in NBS_IDS:
            url=NBS_URL
            params=nbs_parameters(name)
            rows=parse_nbs(self.transport(url,params).decode('utf-8-sig'),name)
            extra['request_parameters']=params
            metadata=[{'field':field,'id':code,'name':GROUP_NAMES[name],'url':url} for field,code in NBS_IDS[name].items()]
        else:
            url=CURRENCY_URL
            rows=parse_csv(self.transport(url).decode('utf-8-sig'),('NBCNBIS',),'monthly')
            metadata=[{'field':'currency_index','id':'NBCNBIS','name':'BIS nominal effective renminbi index, 2020=100','url':'https://fred.stlouisfed.org/series/NBCNBIS'}]
        return {'url':url,'checked_at':datetime.now(timezone.utc).isoformat(),'rows':rows,'series':metadata,**extra}

    def view(self,frequency='trailing4q',refresh=False):
        requested=time.monotonic()
        with self.lock:
            backoff=self.last_failure is not None and time.monotonic()-self.last_failure<30
            shared=self.last_completed is not None and self.last_completed>=requested
            performed=False
            if refresh and self.enabled and not backoff and not shared:
                performed=True
                self.last_attempt_at=datetime.now(timezone.utc).isoformat()
                candidate,warnings=deepcopy(self.snapshot),[]
                with ThreadPoolExecutor(max_workers=3) as executor:
                    futures={name:executor.submit(self._download,name) for name in GROUP_NAMES}
                    for name,future in futures.items():
                        try:
                            group=future.result()
                            old=candidate['groups'].get(name)
                            if old:
                                for field in {key for row in old['rows'] for key in row if key not in ('date','period')}:
                                    before=[row['date'] for row in old['rows'] if row.get(field) is not None]
                                    after=[row['date'] for row in group['rows'] if row.get(field) is not None]
                                    if before and (not after or after[-1]<before[-1] or len(after)<len(before)):
                                        raise BOPError('China source coverage regressed.')
                            candidate['groups'][name]=group
                        except (OSError,HTTPException,ValueError,KeyError,TypeError,subprocess.SubprocessError,BOPError):
                            warnings.append(f'{GROUP_NAMES[name]} refresh failed; saved observations retain their original check time.')
                candidate['warnings']=warnings
                if candidate['groups'].get('bop'):
                    build_china_view(candidate,frequency)
                    self.snapshot=candidate
                    try:
                        self.cache_path.parent.mkdir(parents=True,exist_ok=True)
                        temporary=self.cache_path.with_suffix('.tmp')
                        temporary.write_text(json.dumps(candidate,allow_nan=False),encoding='utf-8')
                        temporary.replace(self.cache_path)
                    except OSError:self.snapshot['warnings'].append('Fresh China data is in memory, but its local cache could not be saved.')
                self.last_failure=time.monotonic() if warnings else None
                self.last_completed=time.monotonic()
            result=build_china_view(self.snapshot,frequency)
            result.update({'refresh_available':self.enabled,
                           'delivery':'source_checked' if refresh and not self.snapshot.get('warnings') and (performed or shared) else 'saved_snapshot',
                           'last_attempt_at':self.last_attempt_at,'refresh_error':None,'source_warning':None,
                           'refresh_cooldown_seconds':max(0,math.ceil(30-(time.monotonic()-self.last_failure))) if self.last_failure else 0})
            return result
