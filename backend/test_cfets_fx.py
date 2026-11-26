from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from .cfets_fx import CFETSFXSource, URLS, expected_fixing, parse_pricing, parse_volatility
from .fx_option_models import build_cells, estimated_greeks, interpolate_forward, interpolate_vol, normalize_trade
from .fx_options import FXOptionsService, FXOptionTape
from .test_fx_options import Carry, DAILY, NOW as TAPE_NOW, Tape, option

NOW=datetime(2026,11,23,10,tzinfo=timezone.utc)
SNAPSHOTS=Path(__file__).parent/'snapshots'


def fixture(kind):
    return json.loads((SNAPSHOTS/f'cfets_fx_{kind}_fixture.json').read_text(encoding='utf-8'))


class Transport:
    def __init__(self):
        self.documents={kind:fixture(kind) for kind in URLS}; self.calls=[]; self.fail=set()

    def __call__(self,url):
        kind=next(kind for kind,endpoint in URLS.items() if url==endpoint)
        self.calls.append(kind)
        if kind in self.fail:raise RuntimeError('PRIVATE_PROVIDER_DETAIL')
        return deepcopy(self.documents[kind])


def cny_option():
    return option('300',**{'UPI FISN':'NA/O Van Call USD CNY','UPI Underlier Name':'USD CNY',
        'Notional currency-Leg 1':'USD','Notional currency-Leg 2':'CNY','Notional amount-Leg 2':'6,600,000',
        'Call currency':'USD','Put currency':'CNY','Put amount':'6600000',
        'Strike Price':'6.6','Strike price currency/currency pair':'USD/CNY'})


def test_native_cfets_schema_percent_units_and_actual_forward_expiry_dates():
    vol=parse_volatility(fixture('volatility'),NOW); pricing=parse_pricing(fixture('pricing'),NOW)
    assert vol['published_at']=='2026-11-23T08:00:00+00:00'
    assert [node['tenor'] for node in vol['nodes']][-3:]==['18M','2Y','3Y']
    assert vol['nodes'][-2]['volatility_pct']==3.175
    assert pricing['date']=='2026-11-23' and pricing['spot']==6.6643
    assert pricing['nodes'][-2]['forward']==pytest.approx(6.6643-3975*.0001)
    assert pricing['nodes'][-2]['expiry']=='2028-11-23'
    # Representative volatility is published directly, not recomputed as bid/ask midpoint.
    assert vol['nodes'][1]['volatility_pct']!=pytest.approx((vol['nodes'][1]['bid_pct']+vol['nodes'][1]['ask_pct'])/2)


@pytest.mark.parametrize('kind,mutation',[
    ('volatility',lambda d:d['data'].update(ccyPair='USD.CNH')),
    ('volatility',lambda d:d['data'].update(volatilitySurface='7')),
    ('volatility',lambda d:d['data'].update(ccyDate='2026-11-24')),
    ('volatility',lambda d:d['data'].update(ccyTime='17:00')),
    ('volatility',lambda d:d['records'].pop()),
    ('volatility',lambda d:d['records'][0].update(midVolatilityStr='NaN')),
    ('volatility',lambda d:d['records'][0].update(midVolatilityStr='301')),
    ('volatility',lambda d:d['records'][0].update(midVolatilityStr='0')),
    ('volatility',lambda d:d['records'][0].update(bidVolatilityStr='20')),
    ('volatility',lambda d:d['records'][0].update(tenor='1W')),
    ('pricing',lambda d:d['data'].update(spotPriceType='29')),
    ('pricing',lambda d:d['data'].update(volatilitySurface='25D RR')),
    ('pricing',lambda d:d['records'].pop()),
    ('pricing',lambda d:d['records'][0].update(spotPrice='6.9')),
    ('pricing',lambda d:d['records'][0].update(expiryDays='99')),
    ('pricing',lambda d:d['records'][0].update(swapRate='-99999')),
])
def test_rejects_wrong_pair_units_moneyness_truncation_and_corrupt_expiries(kind,mutation):
    data=fixture(kind);mutation(data)
    with pytest.raises((ValueError,KeyError)):
        (parse_volatility if kind=='volatility' else parse_pricing)(data,NOW)


def test_throttle_unchanged_report_keeps_publication_time_and_restart_uses_saved_inputs(tmp_path):
    transport=Transport(); now=[NOW]; path=tmp_path/'cfets.json'
    source=CFETSFXSource(path,transport=transport,clock=lambda:now[0]);source.refresh()
    before=source.snapshot();assert before['usable'] and len(transport.calls)==2
    source.refresh();assert len(transport.calls)==2
    now[0]+=timedelta(seconds=60);source.refresh();after=source.snapshot()
    assert after['volatility']['published_at']==before['volatility']['published_at']
    assert after['checks']['volatility']!=before['checks']['volatility']
    reloaded=CFETSFXSource(path,transport=lambda url:pytest.fail('Saved-only read must not fetch'),clock=lambda:now[0])
    assert reloaded.snapshot()['volatility']==after['volatility']
    assert reloaded.snapshot()['pricing']==after['pricing']


def test_new_fixing_reprices_gamma_and_partial_outage_preserves_independent_source_timestamps(tmp_path):
    transport=Transport();transport.documents['volatility']['data']['ccyTime']='15:00'
    source=CFETSFXSource(tmp_path/'cfets.json',transport=transport,clock=lambda:NOW);source.refresh()
    trade={**normalize_trade(cny_option(),TAPE_NOW),'executed_at':'2026-11-22T10:00:00Z','expiry':'2028-05-23'}
    tape=SimpleNamespace(snapshot=lambda:{'trades':[trade],'checked_at':NOW.isoformat(),'published_at':NOW.isoformat(),
        'error':None,'pending_history_files':0,'pending_live_files':0})
    service=FXOptionsService(tmp_path/'tape.json',Carry(),clock=lambda:NOW,tape=tape,cfets_source=source)
    before=service.view('USD/CNY',saved_only=True)
    assert before['modelled_trades']==1 and before['cells'][0]['column']==6 and before['cells'][0]['gamma']>0
    assert before['market_source']=='CFETS / ChinaMoney' and before['pricing_date']=='2026-11-23'
    transport.documents['volatility']['data']['ccyTime']='16:00'
    row=next(row for row in transport.documents['volatility']['records'] if row['tenor']=='18M')
    row.update(midVolatilityStr='3.2')
    transport.fail.add('pricing');source.refresh(force=True)
    after=service.view('USD/CNY',saved_only=True)
    assert after['cells'][0]['gamma']!=before['cells'][0]['gamma']
    assert after['volatility_published_at']=='2026-11-23T08:00:00+00:00'
    assert after['pricing_date']==before['pricing_date'] and after['status']=='stale'
    assert 'PRIVATE_PROVIDER' not in json.dumps(after)
    transport.fail.clear();source.refresh(force=True)
    assert service.view('USD/CNY',saved_only=True)['status']=='ready'


def test_regressed_and_failed_responses_keep_last_good_values_and_successful_check_times(tmp_path):
    transport=Transport();now=[NOW]
    source=CFETSFXSource(tmp_path/'cfets.json',transport=transport,clock=lambda:now[0]);source.refresh()
    before=source.snapshot();now[0]+=timedelta(seconds=60)
    transport.documents['volatility']['data']['ccyTime']='15:00';transport.fail.add('pricing');source.refresh()
    after=source.snapshot()
    assert after['volatility']==before['volatility'] and after['pricing']==before['pricing'] and after['checks']==before['checks']
    assert len(after['errors'])==2
    now[0]+=timedelta(days=8)
    assert not source.snapshot()['usable'] and any('seven days' in text for text in source.snapshot()['warnings'])


def test_fixing_schedule_handles_weekends_and_ten_minute_publication_grace():
    assert expected_fixing(datetime(2026,11,23,1,30,tzinfo=timezone.utc))==datetime(2026,11,20,8,tzinfo=timezone.utc)
    assert expected_fixing(datetime(2026,11,23,2,5,tzinfo=timezone.utc))==datetime(2026,11,20,8,tzinfo=timezone.utc)
    assert expected_fixing(datetime(2026,11,23,2,10,tzinfo=timezone.utc))==datetime(2026,11,23,2,tzinfo=timezone.utc)


def test_cfets_long_dated_gamma_matches_price_finite_difference_and_never_extrapolates():
    spot=6.6643;forward=6.2668;strike=6.5;years=2.;sigma=.03175;notional=1e6
    normal=lambda value:.5*(1+math.erf(value/math.sqrt(2)))
    def price(f):
        d1=(math.log(f/strike)+.5*sigma*sigma*years)/(sigma*math.sqrt(years))
        return f*normal(d1)-strike*normal(d1-sigma*math.sqrt(years))
    h=1e-4;gamma=(price(forward+h)-2*price(forward)+price(forward-h))/(h*h)
    result=estimated_greeks(spot,6.46,strike,years,sigma,'call',notional,1/spot,forward=forward)
    assert result['gamma']==pytest.approx(notional*gamma*forward**2*.01/spot,rel=1e-5)
    nodes=parse_pricing(fixture('pricing'),NOW)['nodes']
    assert interpolate_forward(spot,NOW.date(),nodes,date(2028,11,23))==pytest.approx(6.2668)
    assert interpolate_forward(spot,NOW.date(),nodes,date(2030,1,1)) is None
    assert interpolate_vol([(1,.02),(3,.04)],2) is None
    assert interpolate_vol([(1,.02),(3,.04)],2,max_years=3)>0
    assert interpolate_vol([(1,.02),(3,.04)],3.01,max_years=3) is None


def test_legacy_dtcc_cache_is_reparsed_for_onshore_cny_without_losing_existing_trades(tmp_path):
    transport=Tape();path=tmp_path/'tape.json'
    tape=FXOptionTape(path,transport=transport,clock=lambda:TAPE_NOW);tape.refresh(TAPE_NOW)
    saved=json.loads(path.read_text())
    for item in saved['files'].values():item.pop('pair_scope')
    path.write_text(json.dumps(saved))
    transport.rows[DAILY].append(cny_option());transport.daily[0]['rowCount']=2
    migrated=FXOptionTape(path,transport=transport,clock=lambda:TAPE_NOW)
    assert len(migrated.snapshot()['trades'])==2
    migrated.refresh(TAPE_NOW)
    assert len(migrated.snapshot()['trades'])==3
    assert any(trade['pair']=='USD/CNY' for trade in migrated.snapshot()['trades'])
    assert not FXOptionTape.needs_parse(migrated.files,DAILY)
    offshore=cny_option();offshore.update({'UPI FISN':'NA/O Van Call USD CNH','UPI Underlier Name':'USD CNH'})
    assert normalize_trade(offshore,TAPE_NOW) is None


def test_independent_worker_loads_publications_without_browser_requests_and_stops(tmp_path):
    transport=Transport();transport.documents['volatility']['data']['ccyTime']='15:00'
    now=[NOW];source=CFETSFXSource(tmp_path/'cfets.json',transport=transport,clock=lambda:now[0])
    checked=Event();release=Event();updated=Event();waits=[]
    def wait(seconds):
        waits.append(seconds)
        if len(waits)==1:checked.set();assert release.wait(3);return False
        updated.set();return True
    source.stop.wait=wait
    try:
        source.start();source.start();assert checked.wait(3)
        assert source.snapshot()['usable'] and len(transport.calls)==2
        transport.documents['volatility']['data']['ccyTime']='16:00';now[0]+=timedelta(seconds=60)
        release.set();assert updated.wait(3)
        assert source.snapshot()['volatility']['published_at']=='2026-11-23T08:00:00+00:00'
        assert len(transport.calls)==4
    finally:
        release.set();source.close()
    assert not source.thread.is_alive()
