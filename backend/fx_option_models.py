"""Conservative FX vanilla-trade normalization and explicitly estimated Greeks."""
from bisect import bisect_right
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
import csv
import io
import math
import re
import zipfile

from .fx_exact_feed import PAIRS, timestamp
from .fx_volatility import parse_saxo

EDGES = [.5,.7,.8,.85,.9,.925,.95,.975,1,1.025,1.05,1.075,1.1,1.15,1.2,1.3,1.5]
ROWS = ['<50%','50–70%','70–80%','80–85%','85–90%','90–92.5%','92.5–95%','95–97.5%',
        '97.5–100%','100–102.5%','102.5–105%','105–107.5%','107.5–110%','110–115%',
        '115–120%','120–130%','130–150%','≥150%']
MONTHS = [1,2,3,6,9,12,18]
COLUMNS = ['≤1 month','1–2 months','2–3 months','3–6 months','6–9 months','9–12 months','12–18 months','>18 months']
DEFINITIONS = {
    'notional':'Reported base-currency trade notional × reference spot, converted to USD. This is cumulative unexpired trade activity in the chosen window, not open interest.',
    'delta':'Estimated gross forward delta notional: base notional × |Black forward delta| × model forward, converted to USD. Calls and puts are both measured as positive gross sensitivity.',
    'gamma':'Estimated gamma notional: base notional × Black forward gamma × forward² × 0.01, converted to USD. Measures the delta-notional change for a 1% forward move; it is not half-gamma P&L.',
    'vega':'Estimated vega notional: base notional × Black forward vega per 1 volatility percentage point, converted to USD.',
}
REQUIRED = {'Dissemination Identifier','Original Dissemination Identifier','Action type','Event type','Event timestamp',
    'Execution Timestamp','Expiration Date','First exercise date','UPI FISN','UPI Underlier Name',
    'Notional amount-Leg 1','Notional amount-Leg 2','Notional currency-Leg 1','Notional currency-Leg 2',
    'Call amount','Call currency','Put amount','Put currency','Strike Price','Strike price currency/currency pair',
    'Strike price notation','Package indicator','Non-standardized term indicator'}


def amount(raw):
    text = raw.replace(',', '').strip().rstrip('+')
    value = float(text)
    if not math.isfinite(value) or not 0 < value <= 1e16:
        raise ValueError('Invalid reported FX amount.')
    return value


def normalize_trade(row, now):
    match = re.fullmatch(r'NA/O Van (Call|Put) ([A-Z]{3}) ([A-Z]{3})', row['UPI FISN'])
    if not match or row['Event type'] != 'TRAD' or row['Package indicator'].upper() != 'FALSE' or row['Non-standardized term indicator'].upper() != 'FALSE':
        return None
    direction, first, second = match.groups()
    pair = next((pair for pair in PAIRS if set(pair.split('/')) == {first,second}), None)
    if pair is None or row['UPI Underlier Name'] != f'{first} {second}':
        return None
    base, quote = pair.split('/')
    expiry = date.fromisoformat(row['Expiration Date'])
    # With no explicit style, same first-exercise/expiry screens for European
    # vanillas. Early-exercise, barriers, digitals and targets are excluded.
    if row['First exercise date'] != expiry.isoformat() or row['Strike price notation'] != '1':
        return None
    strike_pair = row['Strike price currency/currency pair']
    if strike_pair not in (pair, quote+'/'+base):
        return None
    raw_strike = amount(row['Strike Price'])
    side = 'call' if (direction == 'Call') == (first == base) else 'put'
    for kind, currency in (('call',row['Call currency']),('put',row['Put currency'])):
        if currency:
            if currency not in (base,quote) or ('call' if (kind=='call')==(currency==base) else 'put') != side:
                return None
    amounts = {}
    for leg in ('1','2'):
        currency = row['Notional currency-Leg '+leg]; raw = row['Notional amount-Leg '+leg]
        if currency and raw:
            if currency not in (base,quote) or currency in amounts:
                return None
            amounts[currency] = amount(raw)
    for kind in ('Call','Put'):
        currency, raw = row[kind+' currency'], row[kind+' amount']
        if currency and raw and currency not in amounts:
            amounts[currency] = amount(raw)
    # Some reporters reverse the pair label while retaining the opposite
    # numerical quote. Verify orientation against the two reported currency
    # legs, rather than silently putting these strikes in the wrong bucket.
    if base not in amounts or quote not in amounts:
        return None
    ratio=amounts[quote]/amounts[base]
    strike=min((raw_strike,1/raw_strike),key=lambda value:abs(math.log(value/ratio)))
    if abs(math.log(strike/ratio))>.01:
        return None
    notional=amounts[base]
    executed = timestamp(row['Execution Timestamp'], now)
    if expiry < executed.date():
        # Some public prints contain inconsistent contract dates. They cannot
        # represent an eligible unexpired trade; retain any correction's
        # lifecycle event with a null trade so it still invalidates its parent.
        return None
    return {'pair':pair,'side':side,'strike':strike,'notional':notional,'expiry':expiry.isoformat(),
            'date':executed.date().isoformat(),'executed_at':executed.isoformat()}


def parse_fx_archive(body, now, expected_rows):
    if not isinstance(body,bytes) or len(body)>20_000_000:
        raise ValueError('FX archive exceeds size limit.')
    events=[]; count=0; seen=set()
    with zipfile.ZipFile(io.BytesIO(body)) as zipped:
        members=zipped.infolist()
        if len(members)!=1 or not members[0].filename.lower().endswith('.csv') or members[0].file_size>180_000_000:
            raise ValueError('Invalid FX archive.')
        with zipped.open(members[0]) as stream:
            reader=csv.DictReader(io.TextIOWrapper(stream,encoding='utf-8-sig'))
            if not reader.fieldnames or not REQUIRED.issubset(reader.fieldnames) or len(reader.fieldnames)!=len(set(reader.fieldnames)):
                raise ValueError('Changed DTCC FX schema.')
            for row in reader:
                count+=1
                if count>500000 or None in row or any(v is None for v in row.values()):
                    raise ValueError('Incomplete FX record.')
                action=row['Action type']
                if action not in ('NEWT','CORR','EROR'):
                    continue
                trade=normalize_trade(row,now) if action!='EROR' else None
                if action=='NEWT' and trade is None:
                    continue
                identifier=row['Dissemination Identifier']; original=row['Original Dissemination Identifier']
                if not identifier.isdigit() or original and not original.isdigit() or identifier in seen:
                    raise ValueError('Invalid FX dissemination ID.')
                if action!='NEWT' and not original:
                    continue
                events.append({'id':identifier,'original':original,'action':action,
                    'at':timestamp(row['Event timestamp'],now).isoformat(),'trade':trade})
                seen.add(identifier)
    if count!=expected_rows:
        raise ValueError('Incomplete FX download.')
    return events


def resolve_trades(files):
    events={}
    for file in files.values():
        for event in file['events']:
            if event['id'] in events and events[event['id']]!=event:
                raise ValueError('Conflicting FX records.')
            events[event['id']]=event
    roots={}; trades={}
    for event in sorted(events.values(),key=lambda item:(item['at'],int(item['id']))):
        identifier=event['id']
        if event['action']=='NEWT':
            roots[identifier]=identifier; trades[identifier]=event['trade']
        elif event['original'] in roots:
            root=roots[event['original']]; roots[identifier]=root
            trades[root]=None if event['action']=='EROR' else event['trade']
    return [trade for trade in trades.values() if trade is not None]


def saxo_surface(body, now):
    if not body:
        return {}, None
    surface={}; published=None
    for tenor, years in [('1W',7/365),('1M',1/12),('3M',.25),('6M',.5),('12M',1)]:
        for pair, observation in parse_saxo(body,now,tenor).items():
            published=observation['iv_time']
            surface.setdefault(pair,[]).append((years,observation['implied_volatility_pct']/100))
    return surface,published


def interpolate_vol(nodes, years):
    if not nodes or not 0<years<=1:
        return None
    if years<=nodes[0][0]:
        return nodes[0][1]
    for (t0,s0),(t1,s1) in zip(nodes,nodes[1:]):
        if years<=t1:
            variance=(s0*s0*t0*(t1-years)+s1*s1*t1*(years-t0))/(t1-t0)
            return math.sqrt(variance/years)
    return None


def estimated_greeks(spot, forward_1y, strike, years, volatility, side, notional, quote_usd):
    if any(not math.isfinite(v) or v<=0 for v in (spot,forward_1y,strike,years,volatility,notional,quote_usd)) or years>1:
        raise ValueError('Invalid FX model inputs.')
    forward=spot*math.exp(math.log(forward_1y/spot)*years)
    root=math.sqrt(years); d1=(math.log(forward/strike)+.5*volatility*volatility*years)/(volatility*root)
    density=math.exp(-.5*d1*d1)/math.sqrt(2*math.pi)
    delta=.5*(1+math.erf(d1/math.sqrt(2)))-(1 if side=='put' else 0)
    gamma=density/(forward*volatility*root)
    return {'delta':notional*abs(delta)*forward*quote_usd,
            'gamma':notional*gamma*forward*forward*.01*quote_usd,
            'vega':notional*forward*density*root*.01*quote_usd}


def expiry_bucket(expiry, today):
    for index,months in enumerate(MONTHS):
        total=today.year*12+today.month-1+months; year,month=total//12,total%12+1
        cutoff=date(year,month,min(today.day,monthrange(year,month)[1]))
        if expiry<=cutoff:
            return index
    return len(MONTHS)


def build_cells(trades, pair, now, window_days, quote, nodes):
    cells={}; modelled=0; selected=0; last_trade=None
    start=now-timedelta(days=window_days)
    spot=quote.get('spot') if quote else None
    if spot is None:
        return [],0,0,None
    quote_usd=1 if pair.endswith('/USD') else 1/spot
    for trade in trades:
        executed=timestamp(trade['executed_at'],now); expiry=date.fromisoformat(trade['expiry'])
        if trade['pair']!=pair or executed<start or expiry<=now.date():
            continue
        selected+=1; last_trade=max(last_trade or executed.isoformat(),executed.isoformat())
        row=bisect_right(EDGES,trade['strike']/spot); column=expiry_bucket(expiry,now.date()); side=trade['side']
        cell=cells.setdefault((row,column,side),{'row':row,'column':column,'side':side,'count':0,
                                               'notional':0.,'delta':0.,'gamma':0.,'vega':0.})
        cell['count']+=1; cell['notional']+=trade['notional']*spot*quote_usd
        years=(expiry-now.date()).days/365; vol=interpolate_vol(nodes,years)
        greeks=estimated_greeks(spot,quote['forward_12m'],trade['strike'],years,vol,side,trade['notional'],quote_usd) if vol is not None else None
        if greeks is not None:
            modelled+=1
        for metric in ('delta','gamma','vega'):
            if greeks is None:
                cell[metric]=None
            elif cell[metric] is not None:
                cell[metric]+=greeks[metric]
    return [{k:round(v,2) if isinstance(v,float) else v for k,v in cell.items()} for cell in cells.values()],selected,modelled,last_trade
