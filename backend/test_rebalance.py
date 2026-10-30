from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .data import DemoProvider
from .rebalance import current_weights, preview, recommendation

@pytest.fixture
def client(tmp_path): return TestClient(create_app(tmp_path/'rebalance.sqlite3'))

def scenario(client):
    book=client.get('/api/workspace').json()
    weights={p['id']:p['weight'] for p in book['portfolio']['positions']}
    weights['AAPL']-=.05;weights['MSFT']+=.03
    return {'targets':weights,'strength':1,'confidence':.95,'lookback':126,'benchmark':'balanced'}

def reviewed(client):
    body=scenario(client);response=client.post('/api/rebalance/preview',json=body)
    assert response.status_code==200,response.text
    p=response.json();assert p['can_execute'],p['violations']
    return {**body,'baseline_fingerprint':p['fingerprint'],'request_id':str(uuid4())},p

def test_preview_is_pure_and_shares_risk_settings(client):
    before=client.get('/api/workspace').json();body,p=reviewed(client);after=client.get('/api/workspace').json()
    assert before==after
    assert p['before']['observations']==p['after']['observations']==126
    assert p['before']['benchmark_id']==p['after']['benchmark_id']=='balanced'
    assert p['before']['var_pct']!=p['after']['var_pct']
    assert p['nav_after']==pytest.approx(before['portfolio']['nav']-p['estimated_cost'],abs=.02)
    assert p['orders'][0]['side']=='sell'

def test_zero_strength_has_no_trades_or_costs(client):
    body=scenario(client);body['strength']=0
    p=client.post('/api/rebalance/preview',json=body).json()
    assert p['orders']==[] and p['estimated_cost']==0 and not p['can_execute']
    assert p['before']==p['after']

def test_allocation_constraints_and_monitor_only(client):
    assert client.post('/api/rebalance/preview',json={'targets':{'EURUSD':.1}}).status_code==409
    assert client.post('/api/rebalance/preview',json={'targets':{'AAPL':.31}}).status_code==409
    assert client.post('/api/rebalance/preview',json={'targets':{'AAPL':.3,'MSFT':.3,'NVDA':.3,'JPM':.1}}).status_code==409
    assert client.post('/api/rebalance/preview',json={'targets':{'AAPL':-.1}}).status_code==409

def test_batch_fill_accounting_reconciliation_and_retry(client):
    body,p=reviewed(client)
    first=client.post('/api/rebalance/execute',json=body)
    assert first.status_code==200,first.text
    w=client.get('/api/workspace').json()
    assert w['cash']==pytest.approx(p['cash_after'])
    assert w['portfolio']['nav']==pytest.approx(p['nav_after'])
    assert w['ledger_verified']
    assert client.post('/api/rebalance/execute',json=body).json()==first.json()
    assert client.get('/api/workspace').json()==w
    # The next pre-trade reconciliation independently rebuilds the batch book.
    assert client.post('/api/rebalance/preview',json=scenario(client)).status_code==200
    assert client.post('/api/rebalance/execute',json={**body,'strength':.5}).status_code==409

def test_stale_and_halted_scenarios_are_blocked(client):
    body,_=reviewed(client)
    client.post('/api/portfolio/halt',json={'halted':True})
    assert client.post('/api/rebalance/execute',json=body).status_code==409
    assert not client.post('/api/rebalance/preview',json=scenario(client)).json()['can_execute']
    assert not any(e['kind']=='rebalance.completed' for e in client.get('/api/workspace').json()['events'])

def test_concurrent_retry_fills_once(client):
    body,p=reviewed(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:client.post('/api/rebalance/execute',json=body),range(2)))
    assert all(r.status_code==200 for r in results)
    assert results[0].json()==results[1].json()
    assert len([e for e in client.get('/api/workspace').json()['events'] if e['kind']=='order.filled'])==len(p['orders'])

def test_policy_persists_and_no_trade_region_holds(client):
    assert client.get('/api/rebalance/policy').json()['status']=='policy_setup'
    body=scenario(client);book=client.get('/api/workspace').json()
    body['targets']={p['id']:p['weight'] for p in book['portfolio']['positions']}
    result=client.put('/api/rebalance/policy',json={'targets':body['targets'],'band':.02,'pace':.5,'max_turnover':.1})
    assert result.status_code==200
    assert result.json()['status']=='hold'
    assert client.get('/api/rebalance/policy').json()['policy']['saved']

def test_partial_boundary_and_turnover_budget(client):
    body=scenario(client)
    client.put('/api/rebalance/policy',json={'targets':body['targets'],'band':.01,'pace':.5,'max_turnover':.01})
    r=client.get('/api/rebalance/policy').json();assert r['status']=='partial_rebalance'
    book=client.get('/api/workspace').json();current={p['id']:p['weight'] for p in book['portfolio']['positions']}
    turnover=sum(abs(r['targets'].get(k,0)-v) for k,v in current.items())
    assert turnover<=.01+1e-10
    assert body['targets']['AAPL']<r['targets']['AAPL']<current['AAPL']

def test_cost_can_block_fully_invested_basket(client):
    with client.app.state.store.transaction() as (_,state):
        # Pure simulation test: no fill or book mutation is submitted here.
        original=deepcopy(state)
    original['limits']['max_gross']=1
    original['limits']['max_position']=1
    # Largest supported lot can exceed residual fees; post-cost violations must be surfaced.
    p=preview(original,DemoProvider(),{'AAPL':1},1,.95,126,'SPX')
    assert not p['feasible'] or p['cash_after']>=0

def test_paper_atomic_rollback_on_midbasket_failure(client,monkeypatch):
    import backend.app as app_module
    body,p=reviewed(client);before=client.get('/api/workspace').json();original=app_module.check_order;calls=[]
    def fail_second(*args,**kwargs):
        calls.append(1)
        if len(calls)==2:raise ValueError('Simulated second-order rejection')
        return original(*args,**kwargs)
    monkeypatch.setattr(app_module,'check_order',fail_second)
    assert client.post('/api/rebalance/execute',json=body).status_code==409
    assert client.get('/api/workspace').json()==before

def test_recommendation_changes_when_market_marks_move(client):
    body=scenario(client);w=client.get('/api/workspace').json()
    targets={p['id']:p['weight'] for p in w['portfolio']['positions']}
    client.put('/api/rebalance/policy',json={'targets':targets,'band':.01,'pace':.5,'max_turnover':.1})
    next(i for i in client.app.state.provider.records if i['id']=='AAPL')['price']*=1.1
    advice=client.get('/api/rebalance/policy').json()
    assert advice['status']=='partial_rebalance'
    assert any(b['instrument_id']=='AAPL' for b in advice['breaches'])

def test_new_instrument_uses_identical_scenario_dates(client):
    state=client.app.state.store.read();provider=client.app.state.provider
    provider.histories['JPM'].pop()  # new holding has no final-day price
    targets=current_weights(state,provider);targets['JPM']=.02;targets['AAPL']-=.03
    p=preview(state,provider,targets,1,.95,126,'balanced')
    assert [x['date'] for x in p['before']['series']]==[x['date'] for x in p['after']['series']]
    assert p['before']['end_date']==p['after']['end_date']
