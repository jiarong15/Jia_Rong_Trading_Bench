"""Strict input contract for an entitled spot/12M-forward/ATM-vol feed.

Provider-specific adapters must produce this contract. An optional configured
JSON bridge can expose it directly; no feed or entitlements are assumed.
"""
from datetime import date, datetime, timezone
import json
import math
import os
import ssl
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPSHandler, HTTPRedirectHandler, build_opener

PAIRS = {'EUR/USD','GBP/USD','USD/JPY','USD/CHF','USD/CAD','AUD/USD','NZD/USD','USD/SEK','USD/NOK'}


def timestamp(value, now):
    stamp = datetime.fromisoformat(value.replace('Z','+00:00'))
    if stamp.tzinfo is None or (stamp-now).total_seconds()>120: raise ValueError('Invalid feed timestamp.')
    return stamp.astimezone(timezone.utc)


def validate_feed(body, now, tenor='12M'):
    document = json.loads(body) if isinstance(body,str) else body
    if not isinstance(document,dict) or not isinstance(document.get('provider'),str) or not document['provider'].strip():
        raise ValueError('Feed provider identity is required.')
    if len(document['provider'])>120:raise ValueError('Invalid provider name.')
    as_of=timestamp(document['as_of'],now)
    quotes=document['quotes']
    if not isinstance(quotes,list) or not 1<=len(quotes)<=len(PAIRS):raise ValueError('Invalid feed quote list.')
    result={}
    for item in quotes:
        pair=item['pair']
        if pair not in PAIRS or pair in result:raise ValueError('Unexpected or duplicate FX pair.')
        if item['iv_tenor']!=tenor or item['iv_delta']!='ATM':raise ValueError('Unexpected options tenor or moneyness.')
        numbers={key:item[key] for key in ('spot','forward_12m','implied_volatility_pct')}
        if any(type(value) not in (int,float) or not math.isfinite(value) or value<=0 for value in numbers.values()):
            raise ValueError('Invalid spot, forward outright or quoted implied volatility.')
        if numbers['implied_volatility_pct']>300:raise ValueError('Invalid percentage volatility units.')
        # Require source conventions explicitly. No forward points or decimal
        # volatilities are silently interpreted as outright prices/percentages.
        if document.get('volatility_unit')!='percent' or document.get('forward_format')!='outright':
            raise ValueError('Feed units must be explicit.')
        quote_at=timestamp(item['quote_time'],now)
        iv_at=timestamp(item['iv_time'],now)
        if quote_at>as_of or iv_at>as_of:raise ValueError('Quote newer than feed snapshot.')
        spot_day=date.fromisoformat(item['spot_date'])
        forward_day=date.fromisoformat(item['forward_date'])
        days=(forward_day-spot_day).days
        if not 350<=days<=380 or abs((spot_day-quote_at.date()).days)>10:
            raise ValueError('Expected a 12M forward from the quoted spot value date.')
        factor=365/days
        base_carry=(numbers['spot']/numbers['forward_12m']-1)*100*factor
        quote_carry=(numbers['forward_12m']/numbers['spot']-1)*100*factor
        result[pair]={'pair':pair,**numbers,'spot_date':item['spot_date'],'forward_date':item['forward_date'],
                      'quote_time':quote_at.isoformat(),'iv_time':iv_at.isoformat(),'iv_tenor':tenor,'iv_delta':'ATM',
                      'source':document['provider'],'pair_carry_pct':base_carry,
                      'usd_carry_pct':base_carry if pair.startswith('USD/') else quote_carry}
    return result


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        raise HTTPError(req.full_url,code,'Feed redirects are disabled.',headers,fp)


def configured_feed():
    url=os.environ.get('FX_CARRY_FEED_URL','').strip()
    if not url:return None
    parts=urlsplit(url)
    if (parts.scheme!='https' and not(parts.scheme=='http' and parts.hostname in ('localhost','127.0.0.1','::1'))) or not parts.hostname or parts.username or parts.password:
        raise ValueError('Configure an HTTPS feed, or a local HTTP provider bridge.')
    token=os.environ.get('FX_CARRY_FEED_TOKEN','').strip()
    def load():
        context=ssl.create_default_context()
        certificate=os.environ.get('FX_ANALYTICS_CA_FILE','').strip()
        if certificate:
            from pathlib import Path
            path=Path(certificate).expanduser()
            if not path.is_absolute():path=Path(__file__).resolve().parents[1]/path
            context.load_verify_locations(cafile=path)
        headers={'Accept':'application/json','Cache-Control':'no-cache','User-Agent':'MeridianLocalDashboard/1.0'}
        if token:headers['Authorization']='Bearer '+token
        opener=build_opener(NoRedirect(),HTTPSHandler(context=context))
        with opener.open(Request(url,headers=headers),timeout=18) as response:body=response.read(1_000_001)
        if len(body)>1_000_000:raise ValueError('Feed response too large.')
        return body.decode('utf-8-sig')
    return load
