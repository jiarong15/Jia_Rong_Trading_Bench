import sqlite3
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .data import DemoProvider
from .engine import backtest

@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path/'workspace.sqlite3'))

def make_order(client):
    result=client.post('/api/research',json={'asset_class':'equities','question':'Compare trailing momentum and risk','horizon':'1M'})
    assert result.status_code==200
    run=result.json()
    eligible=[s for s in run['signals'] if s['paper_eligible'] and s['direction']=='bullish']
    assert eligible
    response=client.post('/api/orders',json={'run_id':run['id'],'instrument_id':eligible[0]['instrument_id']})
    assert response.status_code==200,response.text
    return response.json(),run

def test_market_contract_and_valuation(client):
    w=client.get('/api/workspace').json()
    assert len(w['instruments'])==30
    assert set(i['asset_class'] for i in w['instruments'])=={'bonds','rates','fx','equities','commodities'}
    p=w['portfolio']
    assert p['nav']==pytest.approx(p['cash']+sum(x['value'] for x in p['positions']))
    bond=next(p for p in p['positions'] if p['id']=='UST5')
    assert bond['value']==pytest.approx(bond['quantity']*bond['price']/100)
    assert client.post('/api/data/validate').status_code==200

def test_research_evidence_and_persistent_state(client):
    r=client.post('/api/research',json={'asset_class':'fx','question':'Analyze EURUSD momentum and volatility','horizon':'3M'}).json()
    assert len(r['signals'])==1
    assert r['signals'][0]['evidence_ids']
    assert r['signals'][0]['horizon']=='3M'
    assert client.get('/api/workspace').json()['runs'][0]['id']==r['id']

def test_fill_accounting_and_idempotency(client):
    order,run=make_order(client)
    # Repeated proposal returns the same pending order.
    assert client.post('/api/orders',json={'run_id':run['id'],'instrument_id':order['instrument_id']}).json()['id']==order['id']
    before=client.get('/api/workspace').json()
    result=client.post(f"/api/orders/{order['id']}/execute")
    assert result.status_code==200,result.text
    fill=result.json();after=client.get('/api/workspace').json()
    instrument=next(i for i in before['instruments'] if i['id']==order['instrument_id'])
    assert after['cash']==pytest.approx(before['cash']-fill['quantity']*fill['fill_price']*instrument['multiplier']-fill['fee'],abs=.00001)
    assert after['positions'][instrument['id']]==before['positions'].get(instrument['id'],0)+fill['quantity']
    assert client.post(f"/api/orders/{order['id']}/execute").json()==fill
    assert client.get('/api/workspace').json()['cash']==after['cash']
    assert after['ledger_verified']

def test_halt_blocks_pending_fill_without_mutating_cash(client):
    order,_=make_order(client);before=client.get('/api/workspace').json()['cash']
    client.post('/api/portfolio/halt',json={'halted':True})
    assert client.post(f"/api/orders/{order['id']}/execute").status_code==409
    assert client.get('/api/workspace').json()['cash']==before
    client.post('/api/portfolio/halt',json={'halted':False})
    assert client.post(f"/api/orders/{order['id']}/execute").status_code==200

def test_changed_limits_recheck_at_approval(client):
    order,_=make_order(client);before=client.get('/api/workspace').json()['cash']
    assert client.put('/api/limits',json={'max_position':.05,'max_gross':.1,'order_budget':.05}).status_code==200
    response=client.post(f"/api/orders/{order['id']}/execute")
    assert response.status_code==409
    assert client.get('/api/workspace').json()['cash']==before

def test_concurrent_fill_is_at_most_once(client):
    order,_=make_order(client)
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses=list(executor.map(lambda _:client.post(f"/api/orders/{order['id']}/execute"),range(2)))
    assert all(r.status_code==200 for r in responses)
    assert responses[0].json()==responses[1].json()
    w=client.get('/api/workspace').json()
    assert len([e for e in w['events'] if e['kind']=='order.filled'])==1

def test_monitor_only_cannot_trade(client):
    r=client.post('/api/research',json={'asset_class':'rates','question':'Analyze curve momentum','horizon':'1M'}).json()
    assert client.post('/api/orders',json={'run_id':r['id'],'instrument_id':r['signals'][0]['instrument_id']}).status_code==409
    assert client.post('/api/backtests',json={'instrument_id':'EURUSD'}).status_code==409

def test_cancelled_proposal_cannot_fill(client):
    order,_=make_order(client)
    assert client.post(f"/api/orders/{order['id']}/cancel").status_code==200
    assert client.post(f"/api/orders/{order['id']}/execute").status_code==409

def test_journal_tampering_blocks_execution(client):
    order,_=make_order(client)
    with sqlite3.connect(client.app.state.store.path) as db:
        db.execute("UPDATE events SET payload='{}' WHERE seq=1")
    assert client.post(f"/api/orders/{order['id']}/execute").status_code==409

def test_state_book_mismatch_blocks_execution(client):
    order,_=make_order(client)
    with client.app.state.store.transaction() as (db,state): state['cash']+=100
    result=client.post(f"/api/orders/{order['id']}/execute")
    assert result.status_code==409
    assert 'reconcile' in result.json()['detail']

def test_replay_decides_before_fill_and_is_repeatable(client):
    a=backtest(DemoProvider(),'AAPL');b=backtest(DemoProvider(),'AAPL')
    assert a==b
    assert all(t['decision_date']<t['fill_date'] for t in a['trades'])
    assert len(a['curve'])==126
    assert a['fees']>=0
    r=client.post('/api/backtests',json={'instrument_id':'AAPL'})
    assert r.status_code==200
    assert client.get('/api/workspace').json()['backtests'][0]['id']==r.json()['id']

def test_replay_quantity_does_not_use_future_open():
    provider=DemoProvider();result=backtest(provider,'AAPL');assert result['trades']
    first=result['trades'][0];modified=DemoProvider()
    bar=next(x for x in modified.histories['AAPL'] if x['date']==first['fill_date'])
    bar['open']*=1.01
    changed=backtest(modified,'AAPL')
    assert changed['trades'][0]['quantity']==first['quantity']
    assert changed['trades'][0]['price']!=first['price']

def test_invalid_and_cross_origin_requests(client):
    assert client.post('/api/research',json={'asset_class':'bad','question':'Nope'}).status_code==422
    assert client.put('/api/limits',json={'max_position':1,'max_gross':.8,'order_budget':.05}).status_code==422
    assert client.post('/api/portfolio/halt',json={'halted':True},headers={'Origin':'https://untrusted.example'}).status_code==403

def test_database_reopens_with_same_state(tmp_path):
    path=tmp_path/'persistent.sqlite3';a=TestClient(create_app(path));make_order(a)
    b=TestClient(create_app(path))
    assert len(b.get('/api/workspace').json()['orders'])==1
    assert b.get('/api/workspace').json()['ledger_verified']
