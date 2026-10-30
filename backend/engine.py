"""Deterministic research, sizing and risk. Model/tool outputs never place orders."""
from math import isfinite, sqrt
from statistics import pstdev
from .data import AS_OF

INITIAL_CASH = 250_000.0
INITIAL_POSITIONS = {'AAPL':1000, 'MSFT':300, 'NVDA':1000, 'UST5':200000, 'UST10':100000}
DEFAULT_LIMITS = {'max_position':.30,'max_gross':.90,'order_budget':.05}

def summarize(state, provider):
    positions=[]
    for key,qty in state['positions'].items():
        i=provider.instrument(key)
        if not qty: continue
        value=qty*i['price']*i['multiplier']
        cost=state['costs'].get(key,i['price'])
        positions.append({**i,'quantity':qty,'value':round(value,2),'cost':cost,
          'pnl':round(qty*(i['price']-cost)*i['multiplier'],2),
          'day_pnl':round(value*(1-1/(1+i['change_pct']/100)),2),
          'dv01':round(value*i['duration']*.0001,2)})
    nav=state['cash']+sum(p['value'] for p in positions)
    for p in positions: p['weight']=p['value']/nav
    return {'nav':round(nav,2),'cash':round(state['cash'],2),'positions':positions,
      'gross':sum(abs(p['value']) for p in positions)/nav,
      'day_pnl':round(sum(p['day_pnl'] for p in positions),2),
      'unrealized_pnl':round(sum(p['pnl'] for p in positions),2),
      'realized_pnl':round(state.get('realized_pnl',0),2),
      'dv01':round(sum(p['dv01'] for p in positions),2)}

def research(provider, asset, question, horizon):
    records=[i for i in provider.instruments() if i['asset_class']==asset]
    matches=[i for i in records if i['id'].lower() in question.lower().split()]
    if matches: records=matches
    window=20 if horizon=='1M' else 60
    signals=[]
    for i in records:
        h=provider.series(i['id']); values=[x['close'] for x in h]
        momentum=values[-1]/values[-window-1]-1
        daily=[values[n]/values[n-1]-1 for n in range(len(values)-window,len(values))]
        volatility=pstdev(daily)*sqrt(252)
        score=max(-1,min(1,momentum/max(volatility,.01)*3))
        signals.append({'instrument_id':i['id'],'name':i['name'],'symbol':i['symbol'],
          'score':round(score,3),'direction':'bullish' if score>.1 else 'bearish' if score<-.1 else 'neutral',
          'momentum':round(momentum*100,3),'volatility':round(volatility*100,3),'as_of':AS_OF,
          'horizon':horizon,'source_ids':['demo-market'],'evidence_ids':[f"demo-v1:{i['id']}:{h[-window-1]['date']}",f"demo-v1:{i['id']}:{h[-1]['date']}"],
          'model_version':'momentum-risk-v1','paper_eligible':i['paper_eligible'],
          'reasoning':f"{window}-session change is {momentum:+.2%}; annualized realized volatility is {volatility:.2%}. This is a rule-based directional score, not a probability of profit."})
    return sorted(signals,key=lambda s:abs(s['score']),reverse=True)

def check_order(state,provider,instrument,side,qty,price):
    if state['halted']: raise ValueError('Paper execution is halted. Resume it in Portfolio before proposing or executing orders.')
    if not instrument['paper_eligible']: raise ValueError('This instrument is monitoring-only. Paper execution currently supports USD cash bonds and selected equities.')
    if not isfinite(qty) or qty<=0 or qty%instrument['quantity_step']!=0: raise ValueError(f"Quantity must be a positive multiple of {instrument['quantity_step']}.")
    if not isfinite(price) or price<=0: raise ValueError('A valid execution mark is required.')
    if instrument['quality']!='synthetic' or instrument['vintage']!='demo-v1': raise ValueError('Paper replay requires a recognized snapshot.')
    new_qty=state['positions'].get(instrument['id'],0)+(qty if side=='buy' else -qty)
    if new_qty<0: raise ValueError('Short selling is not enabled for this paper mandate.')
    notional=qty*price*instrument['multiplier']; fee=round(notional*.0001,2)
    cash=state['cash']-notional-fee if side=='buy' else state['cash']+notional-fee
    if cash<0: raise ValueError('Insufficient cash after fees.')
    values={k:v*provider.instrument(k)['price']*provider.instrument(k)['multiplier'] for k,v in state['positions'].items()}
    # Revalue the projected book at the current reference marks; adverse fills
    # reduce cash/NAV rather than also inflating the marked position.
    values[instrument['id']]=new_qty*instrument['price']*instrument['multiplier']
    nav=cash+sum(values.values())
    current=summarize(state,provider)
    gross=sum(abs(v) for v in values.values())/nav
    weight=values[instrument['id']]/nav
    before_weight=state['positions'].get(instrument['id'],0)*instrument['price']*instrument['multiplier']/current['nav']
    if weight>state['limits']['max_position']+1e-9 and weight>=before_weight: raise ValueError('Projected position exceeds the concentration limit.')
    if gross>state['limits']['max_gross']+1e-9 and gross>=current['gross']: raise ValueError('Projected gross exposure exceeds the mandate limit.')
    return {'cash_after':round(cash,6),'projected_weight':weight,'projected_gross':gross,'notional':round(notional,2),'fee':fee,'quantity_after':new_qty}

def backtest(provider,key):
    instrument=provider.instrument(key)
    if not instrument or not instrument['paper_eligible']: raise ValueError('Select an eligible USD cash bond or equity for this replay.')
    h=provider.series(key); cash=100000.; qty=0; fee_total=0; curve=[]; trades=[]; peak=100000.; drawdown=0
    start=len(h)-126; mult=instrument['multiplier']; step=instrument['quantity_step']; reference=h[start-1]['close']
    for n in range(start,len(h)):
        # Form signal and quantity using T-1 only; execution observes the next bar's open.
        prior=h[n-1]['close']; trend=prior/h[n-21]['close']-1
        buy_qty=int((cash*.75/(prior*mult))//step)*step if trend>0 and qty==0 else 0
        sell_qty=qty if trend<0 and qty>0 else 0
        trade_qty=buy_qty or sell_qty
        if trade_qty:
            side='buy' if buy_qty else 'sell'; fill=h[n]['open']*(1.0005 if buy_qty else .9995)
            notional=trade_qty*fill*mult; fee=notional*.0001
            if side=='sell' or notional+fee<=cash:
                cash+=(-notional-fee if buy_qty else notional-fee); qty+=(trade_qty if buy_qty else -trade_qty);fee_total+=fee
                trades.append({'decision_date':h[n-1]['date'],'fill_date':h[n]['date'],'side':side,'quantity':trade_qty,'price':round(fill,5)})
        nav=cash+qty*h[n]['close']*mult; peak=max(peak,nav);drawdown=min(drawdown,nav/peak-1)
        curve.append({'date':h[n]['date'],'strategy':round(nav,2),'benchmark':round(100000*h[n]['close']/reference,2)})
    return {'instrument_id':key,'initial_capital':100000,'return_pct':round((curve[-1]['strategy']/100000-1)*100,2),
      'benchmark_return_pct':round((curve[-1]['benchmark']/100000-1)*100,2),'max_drawdown_pct':round(drawdown*100,2),
      'fees':round(fee_total,2),'curve':curve,'trades':trades,'source':'demo-market','model':'20-session trend / 75% allocation',
      'assumptions':'Synthetic weekday bars; T-1 decision, next-open full fill; 5 bp slippage + 1 bp fees per trade. No coupon, financing, holiday or settlement model. Price-return research only.'}
