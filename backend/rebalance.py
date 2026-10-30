"""Cost-aware rebalancing scenarios and an explicit band policy; no alpha forecast."""
from copy import deepcopy
from hashlib import sha256
from math import floor, isfinite
from .data import AS_OF
from .engine import summarize, check_order
from .risk import portfolio_risk
from .store import canonical

DEFAULT_POLICY = {'band': .02, 'pace': .5, 'max_turnover': .1, 'min_trade_weight': .0005}
PAPERS = [
    {'title':'Boyd et al. — Multi-Period Trading via Convex Optimization', 'url':'https://web.stanford.edu/~boyd/papers/cvx_portfolio.html', 'use':'Explicit targets, constraints and trading-cost accounting; no return forecast or multi-period optimizer is fitted here.'},
    {'title':'Donohue & Yip — Optimal Portfolio Rebalancing with Transaction Costs', 'url':'https://doi.org/10.3905/jpm.2003.319894', 'use':'Tolerance bands and boundary-directed corrections. The full publisher text was inaccessible; this implementation uses the user-supplied summary, supported by Leland’s public no-trade-region research.'},
    {'title':'Gârleanu & Pedersen — Dynamic Trading', 'url':'https://www.aqr.com/Insights/Research/Journal-Article/Dynamic-Trading-With-Predictable-Returns-and-Transactions-Costs', 'use':'Partial adjustment. Predictive moving aims require calibrated forecasts and signal-decay estimates, which are not supplied.'},
    {'title':'Almgren & Chriss — Optimal Execution', 'url':'https://smallake.kr/wp-content/uploads/2016/03/optliq.pdf', 'use':'Separate allocation from execution urgency. Scheduling remains advisory until spreads, volume and market-impact inputs are connected.'},
]

def current_weights(state, provider):
    return {p['id']: p['weight'] for p in summarize(state, provider)['positions']}

def validate_targets(weights, state, provider):
    if len(weights)>30: raise ValueError('Too many target instruments.')
    for key,value in weights.items():
        instrument=provider.instrument(key)
        if not instrument or not instrument['paper_eligible']:
            raise ValueError(f'{key} is not supported for paper rebalancing.')
        if not isfinite(value) or value<0 or value>state['limits']['max_position']+1e-9:
            raise ValueError(f'{key}: target must be between zero and the position limit.')
    if sum(weights.values())>state['limits']['max_gross']+1e-9:
        raise ValueError('Target allocation exceeds the gross exposure limit; leave room for cash.')
    return dict(weights)

def policy_for(state,provider):
    saved=state.get('rebalance_policy')
    if saved: return deepcopy(saved)
    weights={k:min(w,state['limits']['max_position']) for k,w in current_weights(state,provider).items()}
    gross=sum(weights.values()); cap=min(state['limits']['max_gross'],.99)
    if gross>cap: weights={k:v*cap/gross for k,v in weights.items()}
    return {**DEFAULT_POLICY,'targets':weights,'saved':False}

def fingerprint(state,provider):
    content={'cash':state['cash'],'positions':state['positions'],'costs':state['costs'],
        'limits':state['limits'],'halted':state['halted'],'policy':state.get('rebalance_policy'),
        'snapshot':AS_OF,'marks':[{k:i[k] for k in ('id','price','multiplier','quantity_step','quality','vintage')} for i in provider.instruments()]}
    return sha256(canonical(content).encode()).hexdigest()

def settle(state,instrument,side,quantity,fill,fee):
    key=instrument['id']; old=state['positions'].get(key,0);cost=state['costs'].get(key,fill)
    notional=quantity*fill*instrument['multiplier']
    if side=='buy':
        state['costs'][key]=(old*cost+quantity*fill)/(old+quantity)
        state['positions'][key]=old+quantity;state['cash']=round(state['cash']-notional-fee,6)
    else:
        state['positions'][key]=old-quantity;state['cash']=round(state['cash']+notional-fee,6)
        state['realized_pnl']=state.get('realized_pnl',0)+(fill-cost)*quantity*instrument['multiplier']
    state['realized_pnl']=state.get('realized_pnl',0)-fee

def preview(state,provider,targets,strength=1,confidence=.95,lookback=252,benchmark='balanced'):
    validate_targets(targets,state,provider)
    if not isfinite(strength) or not 0<=strength<=1: raise ValueError('Transition strength must be between zero and one.')
    working=deepcopy(state);book=summarize(state,provider);weights=current_weights(state,provider)
    orders=[]; rows=[]
    for key in sorted(set(weights)|set(targets)):
        i=provider.instrument(key)
        if not i or not i['paper_eligible']: raise ValueError(f'{key} cannot be rebalanced with the current execution adapter.')
        old=weights.get(key,0);target=targets.get(key,0);aim=old+strength*(target-old)
        difference=(aim-old)*book['nav']; step=i['quantity_step'];unit=i['price']*i['multiplier']
        quantity=floor(abs(difference)/(unit*step)+1e-9)*step
        side='buy' if difference>0 else 'sell'
        if side=='sell':quantity=min(quantity,state['positions'].get(key,0))
        if quantity:
            fill=round(i['price']*(1.0005 if side=='buy' else .9995),6)
            fee=round(quantity*fill*i['multiplier']*.0001,2)
            orders.append({'instrument_id':key,'symbol':i['symbol'],'side':side,'quantity':quantity,
                'reference_price':i['price'],'fill_price':fill,'fee':fee,
                'notional':quantity*i['price']*i['multiplier'],
                'slippage_cost':quantity*abs(fill-i['price'])*i['multiplier']})
        rows.append({'instrument_id':key,'symbol':i['symbol'],'asset_class':i['asset_class'],
            'current_weight':old,'target_weight':target,'aim_weight':aim})
    orders.sort(key=lambda o:(o['side']!='sell',o['instrument_id']))
    for order in orders:
        settle(working,provider.instrument(order['instrument_id']),order['side'],order['quantity'],order['fill_price'],order['fee'])
    after_book=summarize(working,provider)
    actual={p['id']:p['weight'] for p in after_book['positions']}
    for row in rows:row['projected_weight']=actual.get(row['instrument_id'],0)
    violations=[]
    if state['halted']:violations.append('Paper execution is halted.')
    if working['cash']<0:violations.append('Insufficient cash after slippage and fees; reduce buy allocations.')
    if after_book['gross']>state['limits']['max_gross']+1e-9:violations.append('Rounded, post-cost gross exposure exceeds the mandate limit.')
    for p in after_book['positions']:
        if p['weight']>state['limits']['max_position']+1e-9:violations.append(f"{p['id']} exceeds the post-cost concentration limit.")
    fees=sum(o['fee'] for o in orders);slippage=sum(o['slippage_cost'] for o in orders)
    turnover=sum(o['notional'] for o in orders)/book['nav']
    alignment=set(weights)|{key for key,quantity in working['positions'].items() if quantity}
    return {'fingerprint':fingerprint(state,provider),'snapshot':AS_OF,'targets':targets,'strength':strength,
        'confidence':confidence,'lookback':lookback,'benchmark':benchmark,'rows':rows,'orders':orders,
        'turnover':turnover,'policy_turnover_exceeded':turnover>policy_for(state,provider)['max_turnover']+1e-9,
        'fees':fees,'slippage':slippage,'estimated_cost':fees+slippage,
        'cash_after':working['cash'],'nav_after':after_book['nav'],'gross_after':after_book['gross'],
        'feasible':not violations,'violations':violations,'can_execute':not violations and bool(orders),
        'before':portfolio_risk(state,provider,confidence,lookback,benchmark,alignment),
        'after':portfolio_risk(working,provider,confidence,lookback,benchmark,alignment),
        'assumptions':'Preview only. Quantities round toward the current position by native lot size. Sells precede buys. Estimated full paper fills include 5 bp adverse slippage and 1 bp fees; no tax, spread, market impact or live execution model.'}

def recommendation(state,provider):
    policy=policy_for(state,provider);weights=current_weights(state,provider);targets=policy['targets']
    if not policy['saved']:
        return {'status':'policy_setup','reason':'Save strategic targets once to anchor drift monitoring. The displayed starting mix is a draft based on current holdings, not an optimized allocation.',
            'policy':policy,'targets':targets,'breaches':[],'papers':PAPERS,
            'monitoring':'After saving, compare each validated snapshot against the same anchored targets. No automatic trading is enabled.',
            'execution':'Review the basket before approving any paper trades.',
            'limitations':'Targets are user-defined; no calibrated expected-return or market-impact model is connected.'}
    # A saved policy can become incompatible with a subsequently tightened mandate.
    try:validate_targets(targets,state,provider)
    except ValueError as exc:
        return {'status':'policy_review','reason':str(exc),'policy':policy,'targets':weights,'breaches':[],'papers':PAPERS,'monitoring':'Update the saved target policy to meet the new mandate.'}
    breaches=[];aim=dict(weights);max_position=state['limits']['max_position']
    book=summarize(state,provider);risk_breach=book['gross']>state['limits']['max_gross']+1e-9
    for key in sorted(set(weights)|set(targets)):
        w=weights.get(key,0);t=targets.get(key,0);drift=w-t
        hard=w>max_position+1e-9
        if abs(drift)>policy['band']+1e-9 or hard:
            boundary=max(0,t+policy['band'] if drift>0 else t-policy['band'])
            new=w+policy['pace']*(boundary-w)
            if hard:new=min(new,max_position*.999)
            aim[key]=new
            breaches.append({'instrument_id':key,'current_weight':w,'target_weight':t,'drift':drift,'boundary':boundary,'hard_limit':hard})
            risk_breach=risk_breach or hard
    if risk_breach:
        # Hard limits override gradual band corrections and turnover preferences.
        aim={k:min(v,max_position*.999) for k,v in aim.items()}
        gross=sum(aim.values());cap=state['limits']['max_gross']*.999
        if gross>cap:aim={k:v*cap/gross for k,v in aim.items()}
    desired_turnover=sum(abs(aim.get(k,0)-weights.get(k,0)) for k in set(aim)|set(weights))
    if desired_turnover>policy['max_turnover'] and not risk_breach:
        scale=policy['max_turnover']/desired_turnover
        aim={k:weights.get(k,0)+(aim.get(k,0)-weights.get(k,0))*scale for k in set(aim)|set(weights)}
    if not risk_breach:
        aim={k:(weights.get(k,0) if abs(v-weights.get(k,0))<policy['min_trade_weight'] else v) for k,v in aim.items()}
    reason=('A hard mandate limit is breached. Prioritize risk reduction; the normal turnover cap may be overridden.' if risk_breach else
        'Allocation drift exceeds the tolerance band. Correct part of the distance to the nearest boundary, subject to the turnover budget.' if breaches else
        'Hold: current allocations are inside the no-trade bands. Avoid paying costs to correct immaterial drift.')
    return {'status':'reduce_risk' if risk_breach else 'partial_rebalance' if breaches else 'hold','reason':reason,
        'policy':policy,'targets':aim,'breaches':breaches,'papers':PAPERS,
        'monitoring':'Re-evaluate on each validated market snapshot and after cash flows or fills. Review targets periodically; hard limits override bands. Recalculate at approval. The UI polls every 30 seconds while visible; no unattended trades are placed.',
        'execution':'Review the generated basket and costs first. For live trading, use liquidity/participation limits and risk-based urgency. An Almgren–Chriss schedule cannot be calibrated without spreads, volume, volatility and impact parameters.',
        'limitations':'Transparent policy heuristic, not a fitted multi-period optimizer. No expected-return, forecast-persistence or impact model is supplied. Band, pace and turnover defaults are tunable starting assumptions, not thresholds prescribed by the papers.'}
