from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
from urllib.error import HTTPError, URLError
from unittest.mock import Mock
import pytest
from .bop import BOPError, BOPService
from .bop_portability import ADAPTERS, CountryCheck, inspect_views, failure_code
from . import bop_http

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('code',ADAPTERS)
def test_every_packaged_country_cold_starts_with_seven_charts_in_both_modes(tmp_path,code,monkeypatch):
    monkeypatch.setattr(bop_http,'standard_urlopen',Mock(side_effect=AssertionError('No network in offline checks')))
    check=CountryCheck(code,tmp_path,enabled=False);report=check.run(live=False)
    assert report['status']=='passed' and report['check']=='saved_charts_only'
    assert set(report['charts'])=={'quarterly','trailing4q'}
    assert all(len(charts)==7 for charts in report['charts'].values())
    assert all(not source['fresh_download'] for source in report['sources'])
    assert not list(tmp_path.glob('*.json'))


def test_saved_charts_never_pass_a_live_download_check(tmp_path):
    report=CountryCheck('CAN',tmp_path,enabled=False).run(live=True)
    assert report['status']=='failed' and report['error_code']=='incomplete_or_saved_sources'
    assert all(not source['fresh_download'] for source in report['sources'])


def test_partial_source_failure_keeps_original_timestamp_and_fails_live_check(tmp_path):
    check=CountryCheck('CAN',tmp_path);old=deepcopy(check.service.snapshot['groups'])
    def download(name):
        if name=='currency':raise URLError(socket.gaierror('fixture DNS failure'))
        group=deepcopy(old[name]);group['checked_at']=datetime.now(timezone.utc).isoformat();return group
    check.service._download=download
    report=check.run()
    assert report['status']=='failed'
    stale=[source for source in report['sources'] if not source['fresh_download']]
    assert [source['group'] for source in stale]==['currency']
    assert stale[0]['checked_at']==old['currency']['checked_at'] and report['warnings']


def test_success_requires_every_source_to_be_checked_now(tmp_path):
    check=CountryCheck('CAN',tmp_path);old=deepcopy(check.service.snapshot['groups'])
    def download(name):
        group=deepcopy(old[name]);group['checked_at']=datetime.now(timezone.utc).isoformat();return group
    check.service._download=download
    report=check.run()
    assert report['status']=='passed' and all(source['fresh_download'] for source in report['sources'])


def test_usa_companion_failure_cannot_hide_behind_successful_core_accounts(tmp_path):
    check=CountryCheck('USA',tmp_path,enabled=False);started=datetime.now(timezone.utc)
    views={frequency:check.view(frequency) for frequency in ('quarterly','trailing4q')}
    for view in views.values():
        view['delivery']='source_checked';view['refresh_available']=True
        for source in view['sources']:source['retrieved_at']=started.isoformat()
    report=inspect_views('USA',views,started)
    assert report['status']=='failed'
    assert sum(source['fresh_download'] for source in report['sources'])==2
    assert report['data_notes']  # Historical Treasury date mismatch stays a gap.


@pytest.mark.parametrize('defect',['missing_field','wrong_country','wrong_frequency','invalid_number','missing_frequency'])
def test_chart_contract_rejects_broken_or_misdirected_data(tmp_path,defect):
    check=CountryCheck('CAN',tmp_path,enabled=False)
    views={frequency:check.view(frequency) for frequency in ('quarterly','trailing4q')};view=views['quarterly']
    if defect=='missing_field':
        for row in view['macro']['quarterly']:row['reserves_debt_pct']=None
    elif defect=='wrong_country':view['macro']['country_code']='USA'
    elif defect=='wrong_frequency':view['frequency']='trailing4q'
    elif defect=='invalid_number':view['series'][-1]['goods']=float('nan')
    else:views.pop('trailing4q')
    with pytest.raises(BOPError):inspect_views('CAN',views)


def test_fresh_usa_data_survives_an_unwritable_cache(tmp_path):
    from .test_bop import sample_csv, sample_html
    # A file in place of the cache directory reproduces a failed local write.
    parent=tmp_path/'blocked';parent.write_text('fixture')
    def transport(url):return sample_html() if 'census.gov' in url else sample_csv()
    service=BOPService(parent/'usa.json',transport=transport,api_key='')
    service.snapshot=None
    view=service.view(refresh=True)
    assert view['delivery']=='source_checked' and view['refresh_error'] is None
    assert 'Fresh USA data is in memory' in view['source_warning']


def test_custom_ca_keeps_certificate_and_hostname_verification_enabled(tmp_path,monkeypatch):
    ca=tmp_path/'ca.pem';ca.write_text('fixture')
    context=ssl.create_default_context();load=Mock()
    monkeypatch.setenv('BOP_CA_FILE',str(ca));monkeypatch.setattr(bop_http.ssl,'create_default_context',lambda:context)
    monkeypatch.setattr(context,'load_verify_locations',load)
    bop_http._trusted_context.cache_clear()
    assert bop_http.tls_context() is context
    load.assert_called_once_with(cafile=str(ca.resolve()))
    assert context.verify_mode==ssl.CERT_REQUIRED and context.check_hostname
    assert bop_http.https_handler()._context is context


def test_relative_ca_path_is_anchored_to_project_and_missing_ca_fails_closed(tmp_path,monkeypatch):
    monkeypatch.setattr(bop_http,'PROJECT',tmp_path);monkeypatch.chdir(tmp_path.parent)
    monkeypatch.setenv('BOP_CA_FILE','missing.pem')
    request=Mock();monkeypatch.setattr(bop_http,'standard_urlopen',request)
    with pytest.raises(ValueError,match='BOP_CA_FILE'):bop_http.urlopen('https://example.invalid',timeout=1)
    request.assert_not_called()


def test_no_custom_ca_uses_normal_verified_urllib(monkeypatch):
    monkeypatch.delenv('BOP_CA_FILE',raising=False);request=Mock();monkeypatch.setattr(bop_http,'standard_urlopen',request)
    bop_http.urlopen('https://example.invalid',timeout=2)
    request.assert_called_once_with('https://example.invalid',timeout=2)


@pytest.mark.parametrize('error,code',[(URLError(ssl.SSLCertVerificationError('fixture')),'certificate_error'),
    (URLError(socket.gaierror('fixture')),'dns_error'),(URLError(TimeoutError()),'timeout'),
    (HTTPError('https://example.invalid/?api_key=secret',403,'secret',None,None),'http_403')])
def test_network_diagnostics_are_specific_without_echoing_authenticated_urls(error,code):
    assert failure_code(error)==code and 'secret' not in failure_code(error)


def test_cli_relocates_without_runtime_dependencies_credentials_or_existing_data(tmp_path):
    checkout=tmp_path/'Another computer'/'Trading bench';checkout.mkdir(parents=True)
    shutil.copytree(ROOT/'backend',checkout/'backend',ignore=shutil.ignore_patterns('__pycache__','test_*','requirements*'))
    (checkout/'scripts').mkdir();shutil.copy2(ROOT/'scripts'/'check-bop.py',checkout/'scripts'/'check-bop.py')
    env={key:value for key,value in os.environ.items() if key not in ('PYTHONPATH','FRED_API_KEY','TWELVEDATA_API_KEY','BOP_CA_FILE','BOP_ENABLED')}
    result=subprocess.run([sys.executable,'-I',str(checkout/'scripts'/'check-bop.py'),'--offline','--json'],cwd=tmp_path,env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    report=json.loads(result.stdout)
    assert report['status']=='passed' and report['mode']=='offline' and len(report['countries'])==9
    assert not (checkout/'data').exists() and not (checkout.parent/'.runtime').exists()


def test_running_app_exposes_all_country_charts_and_readiness_without_source_requests(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from .app import create_app
    request=Mock(side_effect=AssertionError('Readiness must not contact sources'))
    monkeypatch.setattr(bop_http,'standard_urlopen',request)
    with TestClient(create_app(tmp_path/'cold-start.sqlite3')) as client:
        status=client.get('/api/fx/bop/status')
        assert status.status_code==200 and status.headers['Cache-Control']=='no-store'
        payload=status.json()
        assert not payload['live_refresh_enabled'] and not payload['public_sources_require_key']
        assert len(payload['countries'])==9 and all(country['saved_charts_ready'] and country['chart_count']==7 for country in payload['countries'])
        for code in ADAPTERS:
            views={}
            for frequency in ('quarterly','trailing4q'):
                response=client.get('/api/fx/bop',params={'country':code,'frequency':frequency,'refresh':'false'})
                assert response.status_code==200 and response.headers['Cache-Control']=='no-store'
                views[frequency]=response.json()
                assert views[frequency]['delivery']=='saved_snapshot'
            assert inspect_views(code,views)['status']=='passed'
    request.assert_not_called()
