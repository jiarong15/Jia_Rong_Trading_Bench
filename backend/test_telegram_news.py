from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from urllib.error import HTTPError
from urllib.error import URLError
import importlib.util
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from .telegram_news import TelegramNewsService, NewsError, parse_preview
from . import telegram_news as news


# Representative Telegram preview markup, with synthetic test headlines only.
def post(identifier=100, text='TEST HEADLINE', stamp='2026-01-01T12:00:00+00:00', extra=''):
    return f'<div class="tgme_widget_message_wrap"><div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="marketfeed/{identifier}"><div class="tgme_widget_message_bubble">{extra}<div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div><div class="tgme_widget_message_footer"><a class="tgme_widget_message_date" href="https://t.me/marketfeed/{identifier}"><time datetime="{stamp}">12:00</time></a></div></div></div></div>'


def test_plain_text_entities_lines_nested_markup_and_untrusted_html_are_not_executed():
    body = post(text='TEST <b>headline</b> &amp; detail<br/>Second line <a href="javascript:alert(1)">link text</a><script>bad()</script><style>.bad{}</style> &lt;img src=x onerror=bad()&gt;')
    row = parse_preview(body)[0]
    assert row['text'] == 'TEST headline & detail\nSecond line link text <img src=x onerror=bad()>'
    assert row['url'] == 'https://t.me/marketfeed/100' and row['published_at'] == '2026-01-01T12:00:00+00:00'


def test_post_timestamp_uses_own_date_not_forwarded_or_embedded_time():
    extra = '<div><a class="tgme_widget_message_date" href="https://t.me/other/1"><time datetime="2020-01-01T00:00:00Z">older</time></a></div>'
    assert parse_preview(post(extra=extra))[0]['published_at'] == '2026-01-01T12:00:00+00:00'


def test_photo_only_posts_have_timestamp_and_canonical_link():
    row = parse_preview(post(text='', extra='<a class="tgme_widget_message_photo_wrap" href="https://t.me/marketfeed/100"></a>'))[0]
    assert row['text'] == 'Photo post' and row['id'] == 100


def test_identical_duplicates_are_removed_and_posts_sort_newest_first():
    rows = parse_preview(post(101) + post(100) + post(101))
    assert [row['id'] for row in rows] == [101, 100]
    with pytest.raises(NewsError): parse_preview(post(100, 'First') + post(100, 'Conflicting'))


@pytest.mark.parametrize('damage', ['foreign_channel', 'missing_time', 'naive_time', 'invalid_time', 'future_time', 'truncated', 'no_posts', 'oversize'])
def test_invalid_channel_markup_timestamps_and_responses_fail_explicitly(damage):
    body = post()
    if damage == 'foreign_channel': body = body.replace('data-post="marketfeed/', 'data-post="other/')
    elif damage == 'missing_time': body = body.replace('tgme_widget_message_date', 'not_a_date')
    elif damage == 'naive_time': body = body.replace('2026-01-01T12:00:00+00:00', '2026-01-01T12:00:00')
    elif damage == 'invalid_time': body = body.replace('2026-01-01T12:00:00+00:00', 'not-a-date')
    elif damage == 'future_time': body = post(stamp=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
    elif damage == 'truncated': body = body[:-18]
    elif damage == 'no_posts': body = '<html><h1>Sign in or try again later</h1></html>'
    else: body = 'x' * 2_000_001
    with pytest.raises(NewsError): parse_preview(body)


def test_shared_cache_checks_every_ten_seconds_and_does_not_delay_by_request_duration(tmp_path):
    clock = [0.0]; calls = []
    def download():
        calls.append(clock[0]); clock[0] += .3; return post()
    service = TelegramNewsService(tmp_path / 'news.json', transport=download, monotonic=lambda: clock[0])
    assert service.view()['status'] == 'ready'
    clock[0] = 9.99; service.view(); assert len(calls) == 1
    clock[0] = 10; service.view(); assert len(calls) == 2
    assert service.view()['poll_interval_seconds'] == 10


def test_new_posts_and_edits_update_by_id_preserving_recent_history(tmp_path):
    clock = [0]; body = [post(100, 'Original')]
    service = TelegramNewsService(tmp_path / 'news.json', transport=lambda: body[0], monotonic=lambda: clock[0])
    service.view(); clock[0] = 10; body[0] = post(101, 'New') + post(100, 'Edited')
    rows = service.view()['items']; assert [(row['id'], row['text']) for row in rows] == [(101, 'New'), (100, 'Edited')]
    clock[0] = 20; body[0] = post(102, 'Latest'); assert [row['id'] for row in service.view()['items']] == [102, 101, 100]
    assert service.cache_path.exists() and not service.cache_path.with_suffix('.tmp').exists()


def test_history_is_bounded_to_200_posts(tmp_path):
    clock = [0]; batch = [0]
    service = TelegramNewsService(tmp_path / 'news.json', transport=lambda: ''.join(post(i) for i in range(batch[0] + 1, batch[0] + 101)), monotonic=lambda: clock[0])
    for value in (0, 100, 200):
        batch[0] = value; service.view(); clock[0] += 10
    rows = service.view()['items']; assert len(rows) == 200 and rows[0]['id'] == 300 and rows[-1]['id'] == 101


def test_outage_preserves_original_check_time_and_saved_posts_across_restart(tmp_path):
    service = TelegramNewsService(tmp_path / 'news.json', transport=lambda: post()); original = service.view()
    def offline(): raise OSError('TLS or network unavailable')
    reloaded = TelegramNewsService(service.cache_path, transport=offline)
    result = reloaded.view()
    assert result['status'] == 'stale' and result['items'] == original['items'] and result['last_checked_at'] == original['last_checked_at']
    assert result['error'] and result['last_attempt_at']


def test_empty_source_failure_never_invents_headlines_or_successful_check(tmp_path):
    def offline(): raise OSError('Offline')
    result = TelegramNewsService(tmp_path / 'news.json', transport=offline).view()
    assert result['status'] == 'unavailable' and result['items'] == [] and result['last_checked_at'] is None
    assert result['error'] and result['retry_after_seconds'] > 0


def test_regressed_preview_retains_newest_known_posts_and_reports_stale(tmp_path):
    clock = [0]; body = [post(100)]
    service = TelegramNewsService(tmp_path / 'news.json', transport=lambda: body[0], monotonic=lambda: clock[0]); first = service.view()
    clock[0] = 10; body[0] = post(99); result = service.view()
    assert result['status'] == 'stale' and result['items'] == first['items'] and result['last_checked_at'] == first['last_checked_at']


def test_rate_limit_is_respected_between_ten_second_client_polls(tmp_path):
    clock = [0]; calls = []
    def limited():
        calls.append(1); raise HTTPError('https://t.me/s/marketfeed', 429, 'Too many requests', {'Retry-After': '30'}, None)
    service = TelegramNewsService(tmp_path / 'news.json', transport=limited, monotonic=lambda: clock[0])
    assert service.view()['retry_after_seconds'] == 30
    clock[0] = 10; service.view(); clock[0] = 20; service.view(); assert len(calls) == 1
    clock[0] = 30; service.view(); assert len(calls) == 2


def test_multiple_clients_share_one_source_check(tmp_path):
    calls = []
    def download(): calls.append(1); return post()
    service = TelegramNewsService(tmp_path / 'news.json', transport=download, monotonic=lambda: 0)
    with ThreadPoolExecutor(max_workers=8) as pool: results = list(pool.map(lambda _: service.view(), range(16)))
    assert len(calls) == 1 and all(result['status'] == 'ready' for result in results)


def test_disabled_connection_does_not_download_and_corrupt_cache_is_not_shown(tmp_path):
    def unwanted(): raise AssertionError('No network expected')
    path = tmp_path / 'news.json'
    path.write_text(json.dumps({'version': 1, 'channel': 'other', 'items': []}), encoding='utf-8')
    result = TelegramNewsService(path, enabled=False, transport=unwanted).view()
    assert result['status'] == 'disabled' and result['items'] == [] and result['last_attempt_at'] is None


def test_endpoint_is_read_only_fixed_channel_and_no_store(tmp_path):
    app = create_app(tmp_path / 'state.json'); service = app.state.telegram_news
    service.enabled = True; service.transport = lambda: post(321, 'Synthetic endpoint check')
    client = TestClient(app); response = client.get('/api/news/telegram')
    assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
    result = response.json(); assert result['channel']['username'] == 'marketfeed' and result['items'][0]['id'] == 321
    assert result['poll_interval_seconds'] == 10 and result['status'] == 'ready'
    assert client.post('/api/news/telegram').status_code == 405


@pytest.mark.parametrize('exception,code', [
    (URLError(ssl.SSLError('handshake failure')), 'tls_error'),
    (URLError(ssl.SSLCertVerificationError('verification failed')), 'certificate_error'),
    (URLError(socket.gaierror('no DNS')), 'dns_error'),
    (URLError(ConnectionRefusedError('proxy refused')), 'connection_refused'),
    (URLError(TimeoutError('timed out')), 'timeout'),
    (HTTPError('https://user:SECRET@example.com/private?token=SECRET', 403, 'SECRET', {}, None), 'access_denied'),
    (HTTPError('https://t.me/s/marketfeed', 429, 'SECRET', {}, None), 'rate_limited'),
    (HTTPError('https://t.me/s/marketfeed', 502, 'SECRET', {}, None), 'http_error'),
    (ValueError('SECRET'), 'invalid_preview'),
    (OSError('SECRET'), 'network_error'),
])
def test_connection_diagnostics_identify_failure_without_leaking_internal_details(exception, code):
    result_code, message = news.connection_failure(exception)
    assert result_code == code and 'SECRET' not in message and message


def test_blocked_connection_recovers_automatically_without_restart_or_cache_reset(tmp_path):
    clock = [0]; connection = ['blocked']
    def download():
        if connection[0] == 'blocked': raise URLError(ssl.SSLError('handshake failed'))
        return post(200, 'SYNTHETIC RECOVERY TEST')
    service = TelegramNewsService(tmp_path / 'relocated-project' / 'data' / 'news.json', transport=download, monotonic=lambda: clock[0])
    failed = service.view(); assert failed['error_code'] == 'tls_error' and failed['last_checked_at'] is None
    connection[0] = 'working'; clock[0] = 10; recovered = service.view()
    assert recovered['status'] == 'ready' and recovered['error_code'] is None and recovered['error'] is None
    assert recovered['items'][0]['id'] == 200 and service.cache_path.exists()
    connection[0] = 'blocked'; clock[0] = 20; stale = service.view()
    assert stale['status'] == 'stale' and stale['error_code'] == 'tls_error' and stale['items'] == recovered['items']
    assert stale['last_checked_at'] == recovered['last_checked_at']


class PreviewResponse:
    def __init__(self, body=None, content_type='text/html; charset=utf-8'):
        self.headers = {'Content-Type': content_type}; self.body = body if body is not None else post().encode('utf-8')
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def read(self, limit): return self.body[:limit]


def test_production_download_parser_service_and_api_use_verified_https(monkeypatch, tmp_path):
    seen = []
    def request(req, *, timeout, context):
        assert req.full_url == 'https://t.me/s/marketfeed' and timeout == 8
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        seen.append(req.full_url); return PreviewResponse(post(876, 'SYNTHETIC HTTPS TRANSPORT TEST').encode('utf-8'))
    monkeypatch.delenv('TELEGRAM_NEWS_CA_FILE', raising=False); monkeypatch.setattr(news, 'urlopen', request)
    app = create_app(tmp_path / 'app' / 'state.json'); app.state.telegram_news.enabled = True
    result = TestClient(app).get('/api/news/telegram').json()
    assert seen == ['https://t.me/s/marketfeed'] and result['status'] == 'ready' and result['error_code'] is None
    assert result['items'][0]['text'] == 'SYNTHETIC HTTPS TRANSPORT TEST'


def test_optional_approved_ca_is_loaded_without_disabling_certificate_checks(monkeypatch, tmp_path):
    context = ssl.create_default_context(); roots = context.get_ca_certs(binary_form=True)
    if not roots: pytest.skip('This Python installation has no extractable trusted roots.')
    certificate = tmp_path / 'approved-ca.pem'; certificate.write_text(ssl.DER_cert_to_PEM_cert(roots[0]), encoding='ascii')
    monkeypatch.setenv('TELEGRAM_NEWS_CA_FILE', str(certificate))
    def request(req, *, timeout, context):
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        return PreviewResponse()
    monkeypatch.setattr(news, 'urlopen', request)
    assert news.check_connection()['status'] == 'ready'
    monkeypatch.setenv('TELEGRAM_NEWS_CA_FILE', str(tmp_path / 'missing.pem'))
    result = news.check_connection()
    assert result['status'] == 'unavailable' and result['error_code'] == 'configuration_error' and result['post_count'] == 0


@pytest.mark.parametrize('response', [PreviewResponse(content_type='application/json'), PreviewResponse(body=b'x' * 2_000_001), PreviewResponse(body=b'\xff\xfe')])
def test_production_download_rejects_wrong_content_oversize_and_invalid_encoding(monkeypatch, response):
    monkeypatch.delenv('TELEGRAM_NEWS_CA_FILE', raising=False); monkeypatch.setattr(news, 'urlopen', lambda *args, **kwargs: response)
    report = news.check_connection()
    assert report['status'] == 'unavailable' and report['error_code'] == 'invalid_preview' and not report['post_count']


def load_checker():
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'check-telegram-news.py'
    spec = importlib.util.spec_from_file_location('telegram_news_checker', path); module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module); return module


def test_deployment_probe_has_no_saved_headlines_and_returns_correct_exit_codes(monkeypatch, tmp_path, capsys):
    checker = load_checker()
    monkeypatch.setattr(news, 'fetch_preview', lambda: post(999, 'SYNTHETIC PRIVATE HEADLINE'))
    monkeypatch.chdir(tmp_path)
    assert checker.main(['--json']) == 0
    output = capsys.readouterr().out; report = json.loads(output)
    assert report['post_count'] == 1 and report['latest_post_id'] == 999 and 'SYNTHETIC PRIVATE HEADLINE' not in output
    assert list(tmp_path.iterdir()) == []
    def blocked(): raise URLError(ConnectionRefusedError('SECRET'))
    monkeypatch.setattr(news, 'fetch_preview', blocked)
    assert checker.main(['--json']) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['error_code'] == 'connection_refused' and report['status'] == 'unavailable' and report['post_count'] == 0


def test_checker_verifies_dashboard_freshness_and_retains_its_connection_diagnostic(monkeypatch):
    checker = load_checker(); feed = {'status': 'ready', 'channel': {'username': 'marketfeed'}, 'items': [{'id': 1}],
                                     'last_checked_at': datetime.now(timezone.utc).isoformat(), 'poll_interval_seconds': 10}
    def response(url, timeout):
        result = PreviewResponse(json.dumps(feed).encode()); result.headers = {'Cache-Control': 'no-store'}; return result
    monkeypatch.setattr(checker, 'urlopen', response)
    assert checker.dashboard_check('http://127.0.0.1:8000')['status'] == 'ready'
    feed['last_checked_at'] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert checker.dashboard_check('http://127.0.0.1:8000')['status'] == 'unavailable'
    feed.update({'status': 'unavailable', 'last_checked_at': None, 'error_code': 'tls_error', 'error': 'HTTPS handshake failed.'})
    assert checker.dashboard_check('http://127.0.0.1:8000')['error_code'] == 'tls_error'


def test_checker_starts_from_a_relocated_folder_using_only_standard_library(tmp_path):
    project = tmp_path / 'another computer' / 'copied-dashboard'; (project / 'backend').mkdir(parents=True); (project / 'scripts').mkdir()
    (project / 'backend' / '__init__.py').write_text('', encoding='utf-8')
    shutil.copyfile(Path(news.__file__), project / 'backend' / 'telegram_news.py')
    source = Path(__file__).resolve().parents[1] / 'scripts' / 'check-telegram-news.py'
    shutil.copyfile(source, project / 'scripts' / source.name)
    result = subprocess.run([sys.executable, '-I', str(project / 'scripts' / source.name), '--help'], cwd=tmp_path,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0 and '--dashboard' in result.stdout and '--json' in result.stdout
