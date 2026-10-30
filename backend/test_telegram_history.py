from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import ssl
from urllib.error import HTTPError, URLError
import pytest
from fastapi.testclient import TestClient
from .app import create_app
from . import telegram_news as news
from .test_telegram_news import post, PreviewResponse


def service(tmp_path, history, latest=None):
    clock = [0.0]
    result = news.TelegramNewsService(tmp_path / 'news.json', transport=latest or (lambda: post(1200)),
                                      history_transport=history, monotonic=lambda: clock[0])
    result.view(); return result, clock


def test_older_pages_fill_archive_to_1000_and_survive_restart(tmp_path):
    calls = []
    def older(before):
        calls.append(before); return ''.join(post(i) for i in range(max(1, before - 100), before))
    feed, clock = service(tmp_path, older)
    for _ in range(10):
        view = feed.load_history(feed.history_cursor); clock[0] += 2
    assert len(view['items']) == 1000 and view['items'][0]['id'] == 1200 and view['items'][-1]['id'] == 201
    assert view['history_limit'] == 1000 and not view['has_more_history'] and len(calls) == 10
    feed.load_history(feed.history_cursor); assert len(calls) == 10
    loaded = news.TelegramNewsService(feed.cache_path, enabled=False)
    assert loaded.items == view['items'] and loaded.history_cursor == feed.history_cursor


def test_history_is_shared_rate_paced_and_duplicates_are_not_downloaded_twice(tmp_path):
    calls = []
    def older(before): calls.append(before); return post(before - 1)
    feed, clock = service(tmp_path, older)
    with ThreadPoolExecutor(max_workers=8) as pool: results = list(pool.map(lambda _: feed.load_history(1200), range(16)))
    assert calls == [1200] and all(len(row['items']) == 2 for row in results)
    assert feed.load_history(1199)['history_retry_after_seconds'] == 2 and calls == [1200]
    clock[0] = 2; assert len(feed.load_history(1199)['items']) == 3 and calls == [1200, 1199]


def test_history_preserves_live_freshness_and_original_outage(tmp_path):
    feed, clock = service(tmp_path, lambda before: post(before - 1)); original = feed.view()
    def offline(): raise URLError(ssl.SSLError('blocked'))
    feed.transport = offline; clock[0] = 10; failed = feed.view()
    older = feed.load_history(1200)
    assert older['last_checked_at'] == original['last_checked_at'] and older['last_attempt_at'] == failed['last_attempt_at']
    assert older['status'] == 'stale' and older['error_code'] == 'tls_error' and older['items'][-1]['id'] == 1199


def test_loading_history_cannot_make_an_old_source_check_live_again(tmp_path):
    feed, _ = service(tmp_path, lambda before: post(before - 1))
    feed.last_checked_at = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    assert feed.load_history(1200)['status'] == 'stale'


def test_failed_history_keeps_live_posts_and_retries_without_restart(tmp_path):
    state = ['failed']
    def older(before):
        if state[0] == 'failed': raise URLError(ssl.SSLError('blocked'))
        return post(before - 1)
    feed, clock = service(tmp_path, older); previous = feed.view(); failed = feed.load_history(1200)
    assert failed['status'] == 'ready' and failed['error'] is None and failed['history_error_code'] == 'tls_error'
    assert failed['items'] == previous['items'] and failed['has_more_history'] and failed['history_retry_after_seconds'] == 10
    state[0] = 'working'; clock[0] = 10; recovered = feed.load_history(1200)
    assert recovered['history_error'] is None and recovered['history_error_code'] is None and len(recovered['items']) == 2


def test_history_rate_limit_pauses_both_history_and_live_requests(tmp_path):
    calls = []
    def older(before): raise HTTPError(news.PREVIEW_URL, 429, 'rate limited', {'Retry-After': '30'}, None)
    def latest(): calls.append(1); return post(1200)
    feed, clock = service(tmp_path, older, latest)
    view = feed.load_history(1200); assert view['history_retry_after_seconds'] == view['retry_after_seconds'] == 30
    clock[0] = 10; feed.view(); feed.load_history(1200); assert len(calls) == 1
    clock[0] = 30; feed.view(); assert len(calls) == 2


def test_live_rate_limit_also_prevents_history_requests(tmp_path):
    older = []
    feed, clock = service(tmp_path, lambda before: older.append(before))
    def limited(): raise HTTPError(news.PREVIEW_URL, 429, 'rate limited', {'Retry-After': '45'}, None)
    feed.transport = limited; clock[0] = 10; feed.view(); feed.load_history(1200)
    assert older == [] and feed.next_history_check == feed.next_check == 55


def test_genuine_empty_channel_page_marks_end_but_sign_in_pages_do_not(tmp_path):
    empty = '<html><head><meta property="og:url" content="https://t.me/marketfeed"></head><body><div class="tgme_channel_info"></div></body></html>'
    feed, _ = service(tmp_path, lambda before: empty)
    view = feed.load_history(1200); assert view['history_exhausted'] and not view['has_more_history'] and view['history_error'] is None
    feed, _ = service(tmp_path / 'blocked', lambda before: '<html>Please sign in</html>')
    view = feed.load_history(1200); assert not view['history_exhausted'] and view['history_error_code'] == 'invalid_preview'


def test_nonprogressing_pages_do_not_loop_or_discard_saved_posts(tmp_path):
    feed, _ = service(tmp_path, lambda before: post(1200))
    view = feed.load_history(1200)
    assert len(view['items']) == 1 and view['history_error'] and view['has_more_history'] and not view['history_exhausted']


def test_pinned_or_overlapping_items_do_not_replace_newer_canonical_entries(tmp_path):
    feed, _ = service(tmp_path, lambda before: post(1200, 'UNEXPECTED PINNED EDIT') + post(1199) + post(1199))
    view = feed.load_history(1200)
    assert [item['id'] for item in view['items']] == [1200, 1199] and view['items'][0]['text'] == 'TEST HEADLINE'


def test_live_updates_still_poll_and_retain_loaded_history(tmp_path):
    latest = [post(1200)]
    feed, clock = service(tmp_path, lambda before: post(1199), latest=lambda: latest[0])
    feed.load_history(1200); clock[0] = 10; latest[0] = post(1201) + post(1200)
    view = feed.view(); assert [row['id'] for row in view['items']] == [1201, 1200, 1199] and view['history_before'] == 1199


def test_nonoverlapping_preview_repairs_gap_before_fetching_ancient_cache(tmp_path):
    latest = [post(1200)]; calls = []
    def older(before): calls.append(before); return post(before - 1)
    feed, clock = service(tmp_path, older, latest=lambda: latest[0]); feed.load_history(1200)
    latest[0] = post(1400) + post(1399); clock[0] = 10; updated = feed.view()
    assert updated['oldest_id'] == 1199 and updated['history_before'] == 1399
    view = feed.load_history(1399); assert calls == [1200, 1399] and any(row['id'] == 1398 for row in view['items'])


def test_full_old_archive_can_be_refilled_with_more_recent_posts_after_an_outage(tmp_path):
    latest = [post(2000)]
    feed, clock = service(tmp_path, lambda before: ''.join(post(i) for i in range(before - 100, before)), latest=lambda: latest[0])
    for _ in range(10): feed.load_history(feed.history_cursor); clock[0] += 2
    assert len(feed.items) == 1000
    latest[0] = post(4000); clock[0] += 10
    assert feed.view()['has_more_history']
    view = feed.load_history(4000)
    assert len(view['items']) == 1000 and view['history_before'] == 3900 and any(item['id'] == 3900 for item in view['items'])


def test_old_200_post_cache_is_migrated_without_losing_saved_items(tmp_path):
    items = [news.parse_preview(post(i))[0] for i in range(1200, 1000, -1)]
    path = tmp_path / 'news.json'; path.write_text(json.dumps({'version': 1, 'channel': 'marketfeed', 'items': items, 'last_checked_at': datetime.now(timezone.utc).isoformat()}))
    feed = news.TelegramNewsService(path, enabled=False)
    assert feed.items == items and feed.history_cursor == 1001 and not feed.history_exhausted


def test_cursor_one_and_disabled_feed_never_download_more_history(tmp_path):
    calls = []
    feed, _ = service(tmp_path, lambda before: calls.append(before), latest=lambda: post(1))
    assert feed.load_history(1)['history_exhausted'] and calls == []
    feed, _ = service(tmp_path / 'disabled', lambda before: calls.append(before)); feed.enabled = False
    assert not feed.load_history(1200)['has_more_history'] and calls == []


@pytest.mark.parametrize('cursor', [0, -1, 1_000_000_000_000, True, '1200', 'https://example.com'])
def test_invalid_cursors_are_rejected(cursor, tmp_path):
    feed, _ = service(tmp_path, lambda before: None)
    with pytest.raises(news.NewsError): feed.load_history(cursor)


def test_history_endpoint_is_read_only_validates_cursor_and_never_accepts_other_urls(tmp_path):
    app = create_app(tmp_path / 'state.json'); feed = app.state.telegram_news; feed.enabled = True
    feed.transport = lambda: post(1200); feed.history_transport = lambda before: post(1199)
    client = TestClient(app); client.get('/api/news/telegram')
    result = client.get('/api/news/telegram/history?before=1200')
    assert result.status_code == 200 and result.headers['Cache-Control'] == 'no-store'
    assert [row['id'] for row in result.json()['items']] == [1200, 1199]
    for value in ('0', '-1', '1000000000000', 'https://example.com', '1.5'):
        assert client.get('/api/news/telegram/history', params={'before': value}).status_code == 422
    assert client.post('/api/news/telegram/history?before=1200').status_code == 405


def test_production_history_uses_only_numeric_before_on_fixed_verified_https(monkeypatch):
    requested = []
    def request(req, *, timeout, context):
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        requested.append(req.full_url); return PreviewResponse(post(1199).encode())
    monkeypatch.setattr(news, 'urlopen', request); monkeypatch.delenv('TELEGRAM_NEWS_CA_FILE', raising=False)
    assert news.parse_preview(news.fetch_preview(1200))[0]['id'] == 1199
    assert requested == ['https://t.me/s/marketfeed?before=1200']
