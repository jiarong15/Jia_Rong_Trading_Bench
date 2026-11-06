from copy import deepcopy
from datetime import date,datetime,timedelta,timezone
import json
from urllib.parse import parse_qs,urlsplit

from fastapi.testclient import TestClient
import pytest

from .app import create_app
from .us_rates import CHARTS,SERIES,USRatesService,parse_fred,parse_swap,transformed

NOW=datetime(2026,10,31,12,tzinfo=timezone.utc)


def levels():
    return {f'{2024+i//12}-{i%12+1:02d}-01':float(100+i) for i in range(34)}


class Source:
    def __init__(self): self.values={key:levels() for key in SERIES};self.calls=[];self.fail=set()
    def __call__(self,url,**kwargs):
        query=parse_qs(urlsplit(url).query);key=(query.get('series_id') or query['id'])[0];self.calls.append(key)
        if key in self.fail: raise ValueError('PRIVATE_KEY_'+url)
        values=self.values[key]
        if 'series_id' in query:
            return json.dumps({'count':len(values),'offset':0,'units':'lin','observations':[{'date':d,'value':str(v) if v is not None else '.'} for d,v in values.items()]})
        return 'observation_date,'+key+'\n'+''.join(f'{d},{v if v is not None else "."}\n' for d,v in values.items())


@pytest.fixture(autouse=True)
def isolated_swap(monkeypatch):
    monkeypatch.setenv('RATES_DTCC_ENABLED','false')
    monkeypatch.delenv('RATES_INFLATION_SWAP_URL',raising=False)
    monkeypatch.delenv('RATES_INFLATION_SWAP_TOKEN',raising=False)


def test_seven_panels_correct_definitions_units_and_native_dates(tmp_path):
    source=Source();service=USRatesService(tmp_path/'rates.json',transport=source,clock=lambda:NOW,api_key='fixture')
    result=service.view();assert len(result['charts'])==7 and len(source.calls)==13
    charts={c['key']:c for c in result['charts']}
    assert [s['series_id'] for s in charts['cpi']['series']]==['CUSR0000SAC','CUSR0000SAS']
    assert [s['series_id'] for s in charts['pce']['series']]==['PCEPI','PCEPILFE']
    employment=charts['employment']['series']
    assert employment[0]['unit']=='%' and employment[1]['unit']=='thousand jobs'
    assert employment[1]['last_value']==1 and employment[1]['axis']=='right' and employment[1]['style']=='bar'
    assert charts['wages']['series'][0]['series_id']=='FRBATLWGT3MMAUMHWGO'
    assert charts['jobs']['series'][0]['last_value']==pytest.approx(.133)
    assert charts['jobs']['series'][1]['unit']=='index'
    assert charts['housing']['series'][0]['series_id']=='UNDCONTSA'
    assert charts['housing']['series'][0]['last_value']==133
    swap,breakeven,expected=charts['inflation_expectations']['series']
    assert swap['status']=='disabled' and swap['observations']==[] and swap['last_value'] is None
    assert breakeven['series_id']=='T5YIE' and expected['series_id']=='EXPINF5YR'
    assert all(s['last_observation_date']=='2026-10-01' for c in result['charts'] for s in c['series'] if s['last_value'] is not None)


def test_growth_uses_exact_calendar_month_and_never_bridges_gaps():
    values={'2025-01-01':100,'2025-12-01':120,'2026-01-01':130,'2026-03-01':140}
    yoy={r['date']:r['value'] for r in transformed(values,'yoy')}
    monthly={r['date']:r['value'] for r in transformed(values,'monthly_change')}
    assert yoy['2026-01-01']==30 and monthly['2026-01-01']==10
    assert yoy['2026-03-01'] is None and monthly['2026-03-01'] is None
    assert transformed({'2025-01-01':None,'2026-01-01':100},'yoy')[-1]['value'] is None
    assert transformed({'2025-01-01':100,'2026-01-01':90},'yoy')[-1]['value']==-10


@pytest.mark.parametrize('change',[
    lambda d:d.update(count=999),lambda d:d.update(offset=1),lambda d:d.update(units='pc1'),
    lambda d:d['observations'].append(d['observations'][0]),
    lambda d:d['observations'][0].update(value='NaN'),lambda d:d['observations'][0].update(value='0'),
    lambda d:d['observations'][0].update(date='2027-01-01'),lambda d:d['observations'][0].update(date='2024-01-02'),
])
def test_rejects_invalid_source_payloads(change):
    source=Source();body=json.loads(source('https://api.stlouisfed.org/?series_id=PCEPI'))
    change(body)
    with pytest.raises((ValueError,TypeError)):parse_fred(json.dumps(body),'PCEPI',NOW.date(),True)


def test_polling_revisions_outage_recovery_and_no_secret_leak(tmp_path):
    source=Source();now=[NOW];path=tmp_path/'rates.json'
    service=USRatesService(path,transport=source,clock=lambda:now[0],api_key='PRIVATE_KEY')
    assert not source.calls and service.view(saved_only=True)['charts'][0]['series'][0]['last_value'] is None
    first=service.view();assert len(source.calls)==13
    source.values['CUSR0000SAC']['2026-10-01']=150
    now[0]+=timedelta(seconds=59);service.view();assert len(source.calls)==13
    now[0]+=timedelta(seconds=1);second=service.view();assert len(source.calls)==26
    assert second['charts'][0]['series'][0]['last_value']!=first['charts'][0]['series'][0]['last_value']
    source.fail.add('UNRATE');now[0]+=timedelta(seconds=60);failed=service.view()
    unrate=failed['charts'][2]['series'][0]
    assert unrate['last_value']==first['charts'][2]['series'][0]['last_value'] and unrate['status']=='stale'
    assert unrate['last_checked_at']==second['charts'][2]['series'][0]['last_checked_at']
    assert 'PRIVATE_KEY' not in json.dumps(failed)+path.read_text()
    recovered=USRatesService(path,transport=lambda url:pytest.fail('No source call for saved-only'),clock=lambda:now[0],api_key='')
    assert recovered.view(saved_only=True)['charts'][0]['series'][0]['last_value']==second['charts'][0]['series'][0]['last_value']
    source.fail.clear();now[0]+=timedelta(seconds=60)
    assert service.view()['charts'][2]['series'][0]['status']=='ready'


def test_missing_months_and_negative_breakevens_are_not_filled(tmp_path):
    source=Source();source.values['PAYEMS']['2026-09-01']=None;source.values['T5YIE']['2026-10-01']=-.1
    result=USRatesService(tmp_path/'rates.json',transport=source,clock=lambda:NOW,api_key='').view()
    payroll=result['charts'][2]['series'][1]
    assert payroll['observations'][-1]['value'] is None
    assert payroll['last_observation_date']=='2026-08-01'
    assert result['charts'][4]['series'][1]['last_value']==-.1


def test_truncated_history_keeps_previous_snapshot(tmp_path):
    source=Source();service=USRatesService(tmp_path/'rates.json',transport=source,clock=lambda:NOW,api_key='')
    before=service.view();source.values['PCEPI'].pop('2024-01-01')
    after=service.view(refresh=True)
    assert after['charts'][1]['series'][0]['status']=='stale'
    assert after['charts'][1]['series'][0]['observations']==before['charts'][1]['series'][0]['observations']


def swap_document():
    return {'provider':'Test genuine swap feed','country':'USA','instrument':'zero_coupon_inflation_swap','index':'CPI-U',
            'tenor_years':5,'unit':'percent','as_of':NOW.isoformat(),
            'observations':[{'date':'2026-10-29','value':2.7},{'date':'2026-10-30','value':2.8}]}


@pytest.mark.parametrize('field,value', [('instrument','breakeven'),('index','RPI'),('country','GBR'),('tenor_years',10),('unit','basis_points')])
def test_never_mislabels_a_breakeven_or_other_instrument_as_a_swap(field,value):
    doc=swap_document();doc[field]=value
    with pytest.raises(ValueError):parse_swap(doc,NOW)


def test_swap_observations_cannot_exceed_the_source_snapshot_date():
    document=swap_document();document['as_of']='2026-10-29T12:00:00Z'
    with pytest.raises(ValueError):parse_swap(document,NOW)


def test_configured_swap_requires_true_instrument_and_keeps_native_dates(tmp_path,monkeypatch):
    monkeypatch.setenv('RATES_INFLATION_SWAP_URL','https://example.test/swaps')
    monkeypatch.setenv('RATES_INFLATION_SWAP_TOKEN','PRIVATE_KEY_SWAP')
    fred=Source()
    def source(url,**kwargs):
        if url=='https://example.test/swaps':
            assert kwargs['token']=='PRIVATE_KEY_SWAP';return swap_document()
        return fred(url)
    result=USRatesService(tmp_path/'rates.json',transport=source,clock=lambda:NOW,api_key='').view()
    swap=result['charts'][4]['series'][0]
    assert swap['last_value']==2.8 and swap['last_observation_date']=='2026-10-30' and swap['status']=='ready'
    assert swap['source']=='Test genuine swap feed' and result['status']=='ready'
    assert 'PRIVATE_KEY' not in json.dumps(result)+(tmp_path/'rates.json').read_text()


def test_api_no_store_and_disabled_mode_make_no_requests(tmp_path):
    app=create_app(tmp_path/'app.sqlite3')
    with TestClient(app) as client:
        response=client.get('/api/rates/us?saved_only=true')
        assert response.status_code==200 and response.headers['cache-control']=='no-store'
        assert response.json()['status']=='disabled' and len(response.json()['charts'])==7
