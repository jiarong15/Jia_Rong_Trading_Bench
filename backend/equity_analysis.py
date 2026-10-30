"""Transparent statement, valuation and implementation calculations; missing stays unknown."""
from datetime import date
from math import floor, isfinite

FIELDS=['net_income','cfo','assets','beginning_assets','current_assets','current_liabilities','long_term_debt','revenue','gross_profit','shares','book_equity','ebit','invested_capital','cash','total_debt']
PAPERS=[
 {'title':'Piotroski · Financial strength','url':'https://www.ivey.uwo.ca/media/3775523/value_investing_the_use_of_historical_financial_statement_information.pdf','application':'Nine accounting tests, with explicit missing-data coverage; originally studied within high book-to-market stocks.'},
 {'title':'Measuring the Moat','url':'https://www.morganstanley.com/im/publication/insights/articles/article_measuringthemoat.pdf','application':'ROIC versus WACC and evidence about barriers to competition; a positive spread alone does not establish a moat.'},
 {'title':'Competitive Advantage Period','url':'https://www.morganstanley.com/content/dam/im/assets/publication/thought-leadership/consilient-observer/article_theneglectedvaluedriver_ltr.pdf','application':'Explore valuation sensitivity to the duration of excess returns using a simplified economic-profit scenario model.'},
 {'title':'Value and Momentum Everywhere','url':'https://www.aqr.com/Insights/Research/Journal-Article/Value-and-Momentum-Everywhere','application':'Combine positive book-to-market and 12–1 momentum ranks. The equal-percentile blend here is a research tool, not a reproduction of the paper’s portfolios.'},
 {'title':'Putting an Academic Factor into Practice','url':'https://spinup-000d1a-wp-offload-media.s3.amazonaws.com/faculty/wp-content/uploads/sites/3/2021/08/Putting-and-Academic-Factor-Into-Practice.pdf','application':'Inspect buffers, turnover, spread, fees, impact assumptions and taxable gains before converting a signal into a proposed trade.'},
]

def ratio(n,d):return n/d if n is not None and d is not None and d>0 else None
def score_financials(document,price=None):
    c=(document or {}).get('current',{});p=(document or {}).get('prior',{})
    a0=p.get('assets');a1=p.get('beginning_assets');a2=c.get('assets')
    avg_c=(a2+a0)/2 if a2 is not None and a0 is not None else None
    avg_p=(a0+a1)/2 if a0 is not None and a1 is not None else None
    roa=ratio(c.get('net_income'),a0);prior_roa=ratio(p.get('net_income'),a1)
    cash_roa=ratio(c.get('cfo'),a0)
    leverage=ratio(c.get('long_term_debt'),avg_c);prior_leverage=ratio(p.get('long_term_debt'),avg_p)
    liquid=ratio(c.get('current_assets'),c.get('current_liabilities'));prior_liquid=ratio(p.get('current_assets'),p.get('current_liabilities'))
    margin=ratio(c.get('gross_profit'),c.get('revenue'));prior_margin=ratio(p.get('gross_profit'),p.get('revenue'))
    turn=ratio(c.get('revenue'),a0);prior_turn=ratio(p.get('revenue'),a1)
    def compare(x,y,op):return int(op(x,y)) if x is not None and y is not None else None
    tests=[('Positive ROA',compare(roa,0,lambda x,y:x>y),'NI / beginning assets > 0'),
      ('Positive operating cash flow',compare(cash_roa,0,lambda x,y:x>y),'CFO / beginning assets > 0'),
      ('Improving ROA',compare(roa,prior_roa,lambda x,y:x>y),'Current ROA > prior ROA'),
      ('Cash earnings quality',compare(cash_roa,roa,lambda x,y:x>y),'CFO / beginning assets > ROA'),
      ('Lower leverage',compare(leverage,prior_leverage,lambda x,y:x<y),'LT debt / average assets declines'),
      ('Improving liquidity',compare(liquid,prior_liquid,lambda x,y:x>y),'Current ratio increases'),
      ('No equity issuance',int(not c['equity_issued']) if isinstance(c.get('equity_issued'),bool) else None,'No common-equity issuance'),
      ('Improving gross margin',compare(margin,prior_margin,lambda x,y:x>y),'Gross profit / revenue increases'),
      ('Improving asset turnover',compare(turn,prior_turn,lambda x,y:x>y),'Revenue / beginning assets increases')]
    known=[point for _,point,_ in tests if point is not None]
    market_cap=c.get('shares')*price if c.get('shares') and price else None
    return {'score':sum(known) if len(known)==9 else None,'known_points':sum(known),'coverage':len(known),
        'possible_range':[sum(known),sum(known)+9-len(known)],'tests':[{'name':n,'point':v,'formula':f} for n,v,f in tests],
        'roa':roa,'cash_roa':cash_roa,'gross_margin':margin,'asset_turnover':turn,
        'book_to_market':ratio(c.get('book_equity'),market_cap),'earnings_yield':ratio(c.get('net_income'),market_cap),
        'notes':'Annual, comparable statements required. Long-term debt includes its current portion. Inputs use USD millions and millions of shares. Shares must be split-consistent with the current price. Banks/insurers need sector-specific interpretation. No missing test is scored as a failure.'}

def validate_financials(document,now):
    if not isinstance(document,dict):raise ValueError('Financials must be an object.')
    if not isinstance(document.get('source'),str) or len(document['source'].strip())<3:raise ValueError('A financial-statement source is required.')
    dates={}
    for key in ['period_end','filed_at','prior_period_end','prior_filed_at']:
        try:dates[key]=date.fromisoformat(document[key])
        except (ValueError,KeyError,TypeError):raise ValueError(f'{key} must be an ISO date.') from None
        if dates[key]>now:raise ValueError('Future financial-statement dates cannot be used.')
    if dates['period_end']<=dates['prior_period_end'] or dates['filed_at']<dates['period_end'] or dates['prior_filed_at']<dates['prior_period_end']:
        raise ValueError('Annual period and filing dates are inconsistent.')
    for period in ['current','prior']:
        row=document.get(period)
        if not isinstance(row,dict):raise ValueError(f'{period} financials must be an object.')
        for key,value in row.items():
            if key not in FIELDS+['equity_issued']:raise ValueError(f'Unknown financial field: {key}.')
            if value is None:continue
            if key=='equity_issued':
                if not isinstance(value,bool):raise ValueError('Equity issuance must be true, false or unknown.')
            elif isinstance(value,bool) or not isinstance(value,(int,float)) or not isfinite(value):raise ValueError(f'{key} must be finite or null.')
            elif key in ['assets','beginning_assets','shares','invested_capital'] and value<=0:raise ValueError(f'{key} must be positive when supplied.')
            elif key in ['current_assets','current_liabilities','long_term_debt','revenue','cash','total_debt'] and value<0:raise ValueError(f'{key} cannot be negative.')
    return {k:document[k] for k in ['source','period_end','filed_at','prior_period_end','prior_filed_at','current','prior']}

def valuation(document,price,wacc=.09,tax=.21,forward_roic=.15,growth=.03,cap=10):
    c=(document or {}).get('current',{});p=(document or {}).get('prior',{})
    required=['ebit','invested_capital','shares','cash','total_debt']
    missing=[k for k in required if c.get(k) is None]
    if missing:return {'available':False,'missing':missing}
    nopat=c['ebit']*(1-tax);ic=c['invested_capital'];shares=c['shares']
    avg_ic=(ic+p['invested_capital'])/2 if p.get('invested_capital') else ic
    historical_roic=nopat/avg_ic
    def ev(years,k):
        whole=floor(years);fraction=years-whole
        profits=sum((forward_roic-k)*ic*(1+growth)**(t-1)/(1+k)**t for t in range(1,whole+1))
        profits+=fraction*(forward_roic-k)*ic*(1+growth)**whole/(1+k)**(whole+1)
        return ic+profits
    def equity(years,k):return (ev(years,k)+c['cash']-c['total_debt'])/shares
    market_ev=price*shares+c['total_debt']-c['cash']
    implied=None;reason=''
    if forward_roic<=wacc:reason='Positive excess-return CAP is undefined when assumed forward ROIC does not exceed WACC.'
    elif market_ev<ev(0,wacc):reason='Market enterprise value is below the invested-capital base in this model.'
    elif market_ev>ev(50,wacc):reason='Market value requires more than the 50-year search range under these assumptions.'
    else:
        low,high=0.,50.
        for _ in range(60):
            mid=(low+high)/2
            if ev(mid,wacc)<market_ev:low=mid
            else:high=mid
        implied=(low+high)/2
    fair=equity(cap,wacc)
    return {'available':True,'nopat':nopat,'historical_roic_pct':historical_roic*100,'roic_spread_pct':(historical_roic-wacc)*100,
        'fair_value_per_share':fair,'gap_pct':(fair/price-1)*100,'implied_cap_years':implied,'implied_cap_note':reason,
        'sensitivity':[{'cap':t,**{f'wacc_{round(k*100,2)}':equity(t,k) for k in [max(.01,wacc-.02),wacc,wacc+.02]}} for t in [0,5,10,15,20,30]],
        'curve':[{'years':t,'fair_value':equity(t,wacc),'market_price':price} for t in range(0,31)],
        'assumptions':{'wacc':wacc,'tax':tax,'forward_roic':forward_roic,'capital_growth':growth,'cap_years':cap},
        'methodology':'Simplified economic-profit model: invested capital plus discounted (forward ROIC − WACC) × beginning capital during CAP, then zero excess returns. Capital grows at the assumed rate. Net debt bridges enterprise to equity value. User assumptions are not forecasts. Historical ROIC uses average supplied invested capital, or ending capital when no prior balance exists. No impairment, intangible-capital adjustment or full DCF is modeled.'}

def implementation(row,portfolio_value,current_shares,target_weight,buffer=.01,fee_bps=1,impact_bps=5,cost_basis=None,tax_rate=None):
    price=row.get('price')
    if not price:raise ValueError('A valid market price is required.')
    current_weight=current_shares*price/portfolio_value;delta_weight=target_weight-current_weight
    hold=abs(delta_weight)<=buffer
    desired=floor(target_weight*portfolio_value/price);delta=0 if hold else desired-current_shares
    notional=abs(delta)*price;spread=row.get('spread_bps');half_spread=spread/2 if spread is not None else None
    explicit=notional*(fee_bps+impact_bps)/10000
    spread_cost=notional*half_spread/10000 if half_spread is not None else None
    tax=max(0,price-cost_basis)*abs(delta)*tax_rate if delta<0 and cost_basis is not None and tax_rate is not None else None
    total=explicit+(spread_cost or 0)+(tax or 0)
    adv=row.get('average_daily_dollar_volume')
    return {'action':'hold' if not delta else 'buy' if delta>0 else 'sell','quantity':abs(delta),'notional':notional,'current_weight':current_weight,
        'turnover_pct':notional/portfolio_value*100,'fee_and_impact_cost':explicit,'spread_cost':spread_cost,'estimated_tax':tax,
        'estimated_total':total,'cost_is_partial':half_spread is None or (delta<0 and tax is None),
        'one_way_hurdle_bps':total/notional*10000 if notional else 0,'iex_adv_participation_pct':notional/adv*100 if adv else None,
        'notes':'Analysis only; no Alpaca order is sent. Impact and fees are user assumptions. The observed IEX spread is not NBBO. ADV is IEX-only, so participation is not a consolidated capacity estimate. Taxes require an entered basis and tax rate; no wash-sale, lot selection or jurisdiction model is applied.'}
