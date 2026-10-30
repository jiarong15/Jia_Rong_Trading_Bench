from datetime import date, timedelta
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .data import DemoProvider
from .risk import portfolio_risk, tail_loss

def state(cash=0):
    return {'cash':cash,'positions':{'AAPL':1},'costs':{'AAPL':243.16},'realized_pnl':0}

def known_path():
    provider=DemoProvider()
    values=[100,90,81,100]+[100]*60
    bars=[{'date':(date(2026,1,1)+timedelta(days=n)).isoformat(),'close':v,'open':v} for n,v in enumerate(values)]
    provider.histories['AAPL']=[dict(x) for x in bars]
    provider.histories['SPX']=[dict(x) for x in bars]
    return provider

def test_empirical_var_and_fractional_expected_shortfall():
    losses=[i/100 for i in range(1,101)]
    var,es=tail_loss(losses,.95)
    assert var==pytest.approx(.95)
    assert es==pytest.approx(.98)
    var,es=tail_loss([.1,.2,.3],.5)
    assert var==pytest.approx(.2)
    assert es==pytest.approx((.3+.5*.2)/1.5)
    assert tail_loss([-.1,-.2],.95)==(0,0)

def test_known_drawdown_and_identical_benchmark():
    risk=portfolio_risk(state(),known_path(),.95,63,'SPX')
    assert risk['max_drawdown_pct']==pytest.approx(-19)
    assert risk['current_drawdown_pct']==pytest.approx(0)
    assert risk['tracking_error_pct']==pytest.approx(0,abs=1e-10)
    assert risk['expected_shortfall_pct']==pytest.approx(100*.2/3.15)
    assert risk['observations']==63
    assert len(risk['series'])==64

def test_cash_dilutes_losses_and_drawdown():
    provider=known_path()
    full=portfolio_risk(state(),provider,.95,63,'SPX')
    half=portfolio_risk(state(243.16),provider,.95,63,'SPX')
    assert half['expected_shortfall_pct']==pytest.approx(full['expected_shortfall_pct']/2)
    assert half['max_drawdown_pct']==pytest.approx(-9.75)
    assert half['cash_weight']==pytest.approx(.5)

def test_cash_only_portfolio_has_no_market_loss():
    risk=portfolio_risk({'cash':1000,'positions':{},'costs':{}},DemoProvider(),.99,252,'balanced')
    assert risk['var_amount']==risk['expected_shortfall_amount']==risk['max_drawdown_pct']==0
    assert risk['tracking_error_pct']>0

def test_missing_dates_fail_without_zero_imputation():
    provider=known_path();del provider.histories['AAPL'][30]
    with pytest.raises(ValueError,match='Insufficient aligned history'):
        portfolio_risk(state(),provider,.95,63,'SPX')

def test_future_prices_ignored_and_bad_prices_rejected():
    provider=known_path();baseline=portfolio_risk(state(),provider,.95,63,'SPX')
    provider.histories['AAPL'].append({'date':'2027-01-01','close':999999})
    assert portfolio_risk(state(),provider,.95,63,'SPX')==baseline
    provider.histories['AAPL'][20]['close']=float('nan')
    with pytest.raises(ValueError,match='positive historical marks'):
        portfolio_risk(state(),provider,.95,63,'SPX')

def test_risk_api_settings_and_scaling(tmp_path):
    client=TestClient(create_app(tmp_path/'risk.sqlite3'))
    result=client.get('/api/portfolio/risk?confidence=0.99&lookback=126&benchmark=SPX')
    assert result.status_code==200,result.text
    risk=result.json()
    assert risk['observations']==126 and risk['confidence']==.99
    assert risk['var_amount']==pytest.approx(risk['var_pct']/100*risk['nav'])
    assert risk['expected_shortfall_pct']>=risk['var_pct']
    assert client.get('/api/portfolio/risk?confidence=0.9').status_code==422
    assert client.get('/api/portfolio/risk?benchmark=unknown').status_code==422
    assert client.get('/api/portfolio/risk?lookback=100').status_code==409
