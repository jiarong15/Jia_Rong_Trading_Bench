"""Portable startup and credential tests; all keys and providers are fixtures."""
import importlib.util
import os
from pathlib import Path
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from .config import load_backend_env, read_backend_env, save_twelvedata_key, project_id
from .twelvedata import TwelveDataService, TwelveDataError
from .app import create_app
import launch


def test_env_is_anchored_and_nonempty_process_settings_win(tmp_path,monkeypatch):
    project=tmp_path/'project';project.mkdir()
    (project/'.env').write_text('TWELVEDATA_API_KEY="local-fixture" # comment\nYAHOO_FX_ENABLED=true\nFX_PRIMARY_PROVIDER=yahoo\nVITE_SECRET=not-loaded\n',encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    env={'TWELVEDATA_API_KEY':'process-fixture','FX_PRIMARY_PROVIDER':''}
    load_backend_env(project,env)
    assert env=={'TWELVEDATA_API_KEY':'process-fixture','FX_PRIMARY_PROVIDER':'yahoo','YAHOO_FX_ENABLED':'true'}
    empty={};load_backend_env(project,empty);assert empty['TWELVEDATA_API_KEY']=='local-fixture'


def test_env_is_literal_and_diagnostics_never_echo_values(tmp_path):
    path=tmp_path/'.env';path.write_text("export TWELVEDATA_API_KEY='$(literal-value)'\n",encoding='utf-8')
    assert read_backend_env(path)['TWELVEDATA_API_KEY']=='$(literal-value)'
    path.write_text('sensitive-fixture has invalid syntax',encoding='utf-8')
    with pytest.raises(ValueError) as exc:read_backend_env(path)
    assert 'line 1' in str(exc.value) and 'sensitive' not in str(exc.value)


def test_volatility_report_setting_loads_without_broker_credentials(tmp_path):
    (tmp_path/'.env').write_text('FX_SAXO_REPORT_ENABLED=true\nTASTYTRADE_CLIENT_SECRET=ignored\nTASTYTRADE_REFRESH_TOKEN=ignored\nFX_TASTYTRADE_ENABLED=true\nVITE_SECRET=never-load\n',encoding='utf-8')
    env={};load_backend_env(tmp_path,env)
    assert env == {'FX_SAXO_REPORT_ENABLED':'true'}


def test_key_writer_preserves_settings_replaces_duplicates_and_rejects_injection(tmp_path):
    path=tmp_path/'.env';path.write_text('# private\nYAHOO_FX_ENABLED=true\nTWELVEDATA_API_KEY=old\nexport TWELVEDATA_API_KEY=duplicate\n',encoding='utf-8')
    save_twelvedata_key('new-test-key',tmp_path)
    assert path.read_text().count('TWELVEDATA_API_KEY=')==1
    assert read_backend_env(path)=={'YAHOO_FX_ENABLED':'true','TWELVEDATA_API_KEY':'new-test-key'}
    assert not list(tmp_path.glob('.fx-config-*'))
    for value in ['', 'value\nYAHOO_FX_ENABLED=false','key # comment','key"','key\x00']:
        with pytest.raises(ValueError):save_twelvedata_key(value,tmp_path)
    assert read_backend_env(path)['TWELVEDATA_API_KEY']=='new-test-key'


def setup_module_from_file():
    spec=importlib.util.spec_from_file_location('fx_setup',Path(__file__).resolve().parents[1]/'scripts'/'configure-fx.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def test_setup_saves_only_a_validated_key_without_echoing_it(tmp_path,monkeypatch,capsys):
    monkeypatch.delenv('TWELVEDATA_API_KEY',raising=False)
    module=setup_module_from_file();probe=Mock()
    assert module.configure(tmp_path,prompt=lambda _: 'test-sensitive-key',probe=probe)==0
    probe.assert_called_once_with('test-sensitive-key')
    assert read_backend_env(tmp_path/'.env')['TWELVEDATA_API_KEY']=='test-sensitive-key'
    assert 'test-sensitive-key' not in capsys.readouterr().out


def test_setup_failure_and_blank_input_preserve_existing_key(tmp_path,monkeypatch,capsys):
    monkeypatch.delenv('TWELVEDATA_API_KEY',raising=False)
    module=setup_module_from_file();save_twelvedata_key('existing-fixture',tmp_path)
    probe=Mock(side_effect=TwelveDataError('Twelve Data rejected the backend API key.'))
    assert module.configure(tmp_path,prompt=lambda _: 'rejected-fixture',probe=probe)==1
    assert read_backend_env(tmp_path/'.env')['TWELVEDATA_API_KEY']=='existing-fixture'
    assert module.configure(tmp_path,prompt=lambda _: '',probe=probe)==0
    assert probe.call_count==1 and 'rejected-fixture' not in capsys.readouterr().out


def test_missing_credentials_do_not_consume_credits_or_make_requests(monkeypatch):
    request=Mock();monkeypatch.setattr('urllib.request.urlopen',request)
    def missing():raise ValueError('unconfigured')
    fx=TwelveDataService(key_getter=missing)
    with pytest.raises(TwelveDataError,match='configure-fx.py'):fx.initialize()
    assert fx.quota()['minute_used']==0 and fx.quota()['daily_used_observed']==0
    request.assert_not_called()


def test_status_is_read_only_and_never_contains_credentials(tmp_path,monkeypatch):
    monkeypatch.setenv('TWELVEDATA_API_KEY','status-sensitive-fixture')
    app=create_app(tmp_path/'fx.sqlite3');request=Mock(side_effect=AssertionError('No network calls'))
    monkeypatch.setattr('urllib.request.urlopen',request)
    with TestClient(app) as client:
        response=client.get('/api/fx/status');health=client.get('/api/health').json()
    assert response.status_code==200 and response.headers['cache-control']=='no-store'
    assert response.json()['twelvedata']['configured']
    assert response.json()['yahoo']['api_key_required'] is False
    assert 'status-sensitive-fixture' not in response.text
    assert health['process_id']==os.getpid() and health['project_id']==project_id()
    assert not app.state.fx.initialized and app.state.fx.used==0
    request.assert_not_called()


def test_direct_backend_startup_loads_local_settings_before_creating_providers(tmp_path,monkeypatch):
    (tmp_path/'.env').write_text('TWELVEDATA_API_KEY=startup-fixture\nFX_PRIMARY_PROVIDER=twelvedata\nYAHOO_FX_ENABLED=false\n',encoding='utf-8')
    for key in ('TWELVEDATA_API_KEY','FX_PRIMARY_PROVIDER','YAHOO_FX_ENABLED'):monkeypatch.delenv(key,raising=False)
    monkeypatch.setenv('WORKBENCH_DB',str(tmp_path/'direct.sqlite3'))
    monkeypatch.setattr('backend.app.load_backend_env',lambda:load_backend_env(tmp_path))
    app=create_app()
    assert not app.state.yahoo_fx.enabled and app.state.fx_sources.primary=='twelvedata'
    assert app.state.fx.key_getter()=='startup-fixture'


@pytest.mark.parametrize('blob',[[],{'key':None},{'key':42},{'key':''}])
def test_unusable_local_encrypted_credentials_are_reported_as_unconfigured(monkeypatch,blob):
    from .credentials import twelvedata_status
    monkeypatch.delenv('TWELVEDATA_API_KEY',raising=False)
    monkeypatch.setattr('backend.credentials.encrypted_credentials',lambda *args:blob)
    assert twelvedata_status()=={'configured':False,'source':None}


def test_setup_refuses_to_echo_input_when_terminal_cannot_hide_it(tmp_path,monkeypatch,capsys):
    import warnings
    from getpass import GetPassWarning
    module=setup_module_from_file()
    def not_hidden(_):warnings.warn('Cannot hide input',GetPassWarning)
    assert module.configure(tmp_path,prompt=not_hidden,probe=Mock())==1
    assert not (tmp_path/'.env').exists() and 'interactive terminal' in capsys.readouterr().out


def test_restart_only_stops_the_matching_project_and_saved_pid(tmp_path):
    pid_file=tmp_path/'server-pid.txt';pid_file.write_text('876543')
    health={'mode':'synthetic-paper','process_id':876543,'project_id':project_id(tmp_path)}
    terminate=Mock();launch.stop_owned_server(health,pid_file,tmp_path,terminate)
    assert terminate.call_args.args[0]==876543
    for changes in [{'process_id':876544},{'project_id':project_id(tmp_path/'other')},{'mode':'other'},{'process_id':None}]:
        terminate.reset_mock()
        with pytest.raises(ValueError):launch.stop_owned_server({**health,**changes},pid_file,tmp_path,terminate)
        terminate.assert_not_called()


def test_missing_pid_never_kills_anything(tmp_path):
    terminate=Mock()
    with pytest.raises(ValueError):launch.stop_owned_server({},tmp_path/'missing',tmp_path,terminate)
    terminate.assert_not_called()
