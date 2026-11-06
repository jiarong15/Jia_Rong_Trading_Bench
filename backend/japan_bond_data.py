"""MOF release adapter for the Japan bonds JSON API.

The MOF upstream publishes CSV, XLS and HTML, not a keyless JSON yield API.
3M is the actual bill auction average-price yield, never a call/interbank rate.
All other tenors are the MOF's published daily constant-maturity JGB yields.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
import math
import re
from urllib.parse import urljoin, urlsplit
import xlrd
from .sovereign_yields import JP, JP_CURRENT, Tables, blank, parse_japan
from .treasury_yields import validate_rows

AUCTIONS = 'https://www.mof.go.jp/english/policy/jgbs/auction/'
BILLS = AUCTIONS+'past_auction_results/Auction_Results_for_T-bills.xls'
RESULT_PATH = '/english/policy/jgbs/auction/calendar/etbill/etbillresul/eresul'


def parse_bill_history(body, today):
    try:
        book = xlrd.open_workbook(file_contents=body, on_demand=True)
        try:
            if book.datemode != 0: raise ValueError('Unexpected MOF Excel date system.')
            rows = {}; seen = set()
            for sheet in book.sheets():
                if not re.fullmatch(r'FY\d{4}', sheet.name.strip()): continue
                if sheet.nrows > 500 or sheet.ncols > 30: raise ValueError('Unexpected MOF bill history size.')
                header = [' '.join(str(cell).split()) for cell in sheet.row_values(2)]
                if header[:5] != ['Issue Number','Maturity','Auction Date','Issue Date','Maturity Date'] or header[9] != 'Yield at the Average Price(%)':
                    raise ValueError('MOF bill history columns changed.')
                for cells in list(sheet.get_rows())[3:]:
                    if cells[1].value != '3-month': continue
                    if any(cells[i].ctype != xlrd.XL_CELL_DATE for i in (2,3,4)):
                        raise ValueError('Invalid MOF bill auction dates.')
                    auction, issued, matures = [xlrd.xldate_as_datetime(cells[i].value,0).date() for i in (2,3,4)]
                    if not auction <= today or not auction <= issued < matures or not 60 <= (matures-issued).days <= 110:
                        raise ValueError('Invalid 3M bill term or auction date.')
                    level = cells[9].value
                    if type(level) not in (int,float) or not math.isfinite(level) or auction in seen:
                        raise ValueError('Invalid or duplicate 3M bill auction yield.')
                    seen.add(auction)
                    rows[auction.isoformat()] = round(level,8)
            if len(rows) < 2: raise ValueError('Missing MOF 3M auction history.')
            return rows
        finally: book.release_resources()
    except (xlrd.XLRDError, IndexError, AssertionError) as exc:
        raise ValueError('Invalid MOF bill workbook.') from None


class Calendar(HTMLParser):
    def __init__(self, body, year, month):
        super().__init__(convert_charrefs=True)
        self.rows=[]; self.row=None; self.cell=None; self.links=None
        self.feed(body.decode('utf-8-sig'))
        self.results=[]
        for cells in self.rows:
            if len(cells)<4 or not re.fullmatch(r'Treasury Discount Bills \(3-month\)\(\d+\)',cells[1][0]): continue
            auction = datetime.strptime(cells[0][0].replace('.',''),'%b %d, %Y').date()
            if (auction.year,auction.month)!=(year,month): raise ValueError('Wrong MOF auction calendar month.')
            issue = re.search(r'\((\d+)\)$',cells[1][0])[1]
            links = cells[3][1]
            if not links: continue  # Scheduled auction: results have not been released.
            url = urljoin(AUCTIONS+'calendar/',links[0])
            parsed=urlsplit(url)
            if parsed.scheme!='https' or parsed.hostname!='www.mof.go.jp' or parsed.path!=RESULT_PATH+auction.strftime('%Y%m%d')+'.htm' or parsed.query:
                raise ValueError('Unexpected MOF auction-result link.')
            self.results.append((auction,issue,url))
        if 'Auction Calendar' not in body.decode('utf-8-sig'):
            raise ValueError('Missing MOF auction calendar.')

    def handle_starttag(self, tag, attrs):
        if tag=='tr': self.row=[]
        elif tag in ('th','td') and self.row is not None: self.cell=[]; self.links=[]
        elif tag=='a' and self.cell is not None:
            link=dict(attrs).get('href')
            if link: self.links.append(link)
        elif tag=='br' and self.cell is not None: self.cell.append(' ')

    def handle_data(self, text):
        if self.cell is not None: self.cell.append(text)

    def handle_endtag(self, tag):
        if tag in ('th','td') and self.cell is not None:
            self.row.append((' '.join(''.join(self.cell).split()),self.links)); self.cell=None
        elif tag=='tr' and self.row is not None: self.rows.append(self.row); self.row=None


def parse_bill_result(body, expected_date, expected_issue):
    for table in Tables(body).tables:
        if not table or len(table[0])!=12 or table[0][0]!='Issue Number': continue
        if table[0][1:4]!=['Auction Date','Issue Date','Maturity Date'] or table[0][10]!='Yield at the Average Price':
            raise ValueError('MOF bill result columns changed.')
        if len(table)!=2 or len(table[1])!=12: raise ValueError('Unexpected MOF bill result count.')
        cells=table[1]
        auction,issued,matures=[datetime.strptime(cells[i],'%m/%d/%Y').date() for i in (1,2,3)]
        if auction!=expected_date or cells[0]!=expected_issue or not auction<=issued<matures or not 60<=(matures-issued).days<=110:
            raise ValueError('Wrong MOF bill issue, auction date or maturity.')
        if not re.fullmatch(r'-?\d+(?:\.\d+)?%',cells[10]): raise ValueError('Invalid MOF bill yield unit.')
        return float(cells[10][:-1])
    raise ValueError('Missing MOF bill auction result.')


def download(downloader, today):
    previous=today.replace(day=1)-timedelta(days=1)
    months=[(previous.year,previous.month),(today.year,today.month)]
    jobs=[lambda:downloader.cached('japan-jgb-history.csv',JP,lambda body:validate_rows(parse_japan(body),today,['y2','y5','y10','y30'])),
          lambda:downloader.transport(JP_CURRENT),
          lambda:downloader.cached('japan-bills.xls',BILLS,lambda body:parse_bill_history(body,today))]
    jobs += [lambda y=y,m=m:Calendar(downloader.transport(AUCTIONS+f'calendar/{y%100:02d}{m:02d}e.htm'),y,m).results for y,m in months]
    with ThreadPoolExecutor(max_workers=3) as pool: history,current,bills,*calendars=list(pool.map(lambda job:job(),jobs))
    rows={row['date']:row for body in (history,current) for row in parse_japan(body)}
    bill_rows=parse_bill_history(bills,today)
    releases=[item for entries in calendars for item in entries if item[0]<=today]
    if len({item[0] for item in releases})!=len(releases): raise ValueError('Duplicate MOF auction release.')
    newest=max((item[0] for item in releases),default=None)
    def result(item):
        auction,issue,url=item
        validate=lambda body:parse_bill_result(body,auction,issue)
        body=downloader.transport(url) if auction==newest else downloader.cached('japan-bill-'+auction.isoformat()+'.html',url,validate)
        return auction.isoformat(),validate(body)
    with ThreadPoolExecutor(max_workers=3) as pool: bill_rows.update(pool.map(result,releases))
    for stamp,level in bill_rows.items(): rows.setdefault(stamp,blank(stamp))['m3']=level
    return validate_rows(list(rows.values()),today),downloader.definition['source']
