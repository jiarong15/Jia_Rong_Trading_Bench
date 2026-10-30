"""Read-only public @marketfeed preview with a shared ten-second cache.

Upstream HTML is reduced to plain text. No account, bot or outgoing messages.
"""
from copy import deepcopy
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
from threading import RLock
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CHANNEL = 'marketfeed'
PREVIEW_URL = 'https://t.me/s/marketfeed'
CHANNEL_URL = 'https://t.me/marketfeed'
POLL_SECONDS = 10
MAX_ITEMS = 1000
MAX_CACHE_BYTES = 100_000_000
HISTORY_INTERVAL = 2
MAX_DOWNLOAD = 2_000_000
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}


class NewsError(Exception):
    def __init__(self, message, code='invalid_preview'):
        super().__init__(message)
        self.code = code


def fetch_preview(before=None):
    if before is not None and (type(before) is not int or not 0 < before < 1_000_000_000_000):
        raise NewsError('Invalid Telegram history cursor.')
    # Use this machine's trusted roots and normal system/environment proxies.
    # An optional deployment-specific CA adds trust without disabling TLS checks.
    context = ssl.create_default_context()
    ca_file = os.environ.get('TELEGRAM_NEWS_CA_FILE', '').strip()
    if ca_file:
        try: context.load_verify_locations(cafile=ca_file)
        except (OSError, ValueError) as exc:
            raise NewsError('The configured Telegram CA certificate could not be loaded. Check TELEGRAM_NEWS_CA_FILE on this computer.', 'configuration_error') from exc
    url = PREVIEW_URL if before is None else PREVIEW_URL + '?before=' + str(before)
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; MeridianLocalDashboard/1.0)',
                      'Accept': 'text/html', 'Cache-Control': 'no-cache', 'Pragma': 'no-cache'})
    with urlopen(request, timeout=8, context=context) as response:
        if 'text/html' not in response.headers.get('Content-Type', ''): raise NewsError('Telegram did not return a channel preview.')
        body = response.read(MAX_DOWNLOAD + 1)
    if len(body) > MAX_DOWNLOAD: raise NewsError('Telegram preview exceeded the download limit.')
    return body.decode('utf-8-sig')


def connection_failure(exc):
    """Actionable diagnostics without exposing URLs, proxy credentials or bodies."""
    if isinstance(exc, NewsError): return exc.code, str(exc)
    if isinstance(exc, HTTPError):
        if exc.code == 429: return 'rate_limited', 'Telegram requested a pause. The feed will retry automatically.'
        if exc.code in (403, 451):
            return 'access_denied', f'The Telegram request was denied (HTTP {exc.code}). Check whether Telegram or a network access filter is restricting this server.'
        return 'http_error', f'The Telegram request returned HTTP {exc.code}. The feed will retry automatically.'
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, ssl.SSLCertVerificationError):
        return 'certificate_error', "This computer could not verify Telegram's HTTPS certificate. Check its trusted certificates or configure TELEGRAM_NEWS_CA_FILE with an approved CA certificate."
    if isinstance(reason, ssl.SSLError):
        return 'tls_error', "The HTTPS handshake with Telegram failed. Check this computer's network/proxy rules and TLS configuration. The feed will retry automatically."
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return 'timeout', 'The Telegram connection timed out. Check server connectivity; the feed will retry automatically.'
    if isinstance(reason, socket.gaierror):
        return 'dns_error', "The server could not resolve Telegram or its proxy. Check this computer's DNS and proxy settings."
    if isinstance(reason, ConnectionRefusedError):
        return 'connection_refused', "The connection was refused. Check Telegram access and this server's configured proxy. The feed will retry automatically."
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return 'invalid_preview', 'Telegram did not return a valid public channel preview. Its layout may have changed or a network sign-in/filter page may have been returned.'
    return 'network_error', 'Telegram is temporarily unreachable from this dashboard server. The next check will retry automatically.'


def check_connection(transport=None):
    """Uncached, read-only deployment probe using the production fetch/parser."""
    report = {'channel': CHANNEL, 'preview_url': PREVIEW_URL, 'checked_at': datetime.now(timezone.utc).isoformat(),
              'poll_interval_seconds': POLL_SECONDS, 'status': 'unavailable', 'post_count': 0,
              'latest_post_id': None, 'latest_published_at': None, 'error_code': None, 'error': None}
    try:
        items = parse_preview((transport or fetch_preview)())
        report.update({'status': 'ready', 'post_count': len(items), 'latest_post_id': items[0]['id'], 'latest_published_at': items[0]['published_at']})
    except (OSError, HTTPException, ValueError, TypeError, KeyError, NewsError) as exc:
        report['error_code'], report['error'] = connection_failure(exc)
    return report


def valid_timestamp(value):
    if not isinstance(value, str): raise NewsError('Telegram post has no publication time.')
    try: at = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc: raise NewsError('Telegram post has an invalid publication time.') from exc
    if at.tzinfo is None or at.year < 2010 or at.timestamp() > time.time() + 300: raise NewsError('Telegram post has an invalid publication time.')
    return at.astimezone(timezone.utc).isoformat()


class PreviewParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []; self.current = None; self.text_depth = None; self.date_depth = None; self.skip_depth = None
        self.items = []; self.post_count = 0; self.channel_meta = False; self.channel_layout = False

    def handle_starttag(self, tag, attrs):
        data = dict(attrs); classes = set(data.get('class', '').split()); depth = len(self.stack) + 1
        if tag == 'meta' and data.get('property') == 'og:url' and data.get('content', '').rstrip('/') == CHANNEL_URL: self.channel_meta = True
        if 'tgme_channel_info' in classes: self.channel_layout = True
        if tag not in VOID: self.stack.append(tag)
        if 'tgme_widget_message' in classes and 'data-post' in data:
            if self.current is not None: raise NewsError('Telegram changed its post layout.')
            match = re.fullmatch(r'marketfeed/([1-9]\d{0,11})', data['data-post'])
            if not match: raise NewsError('Telegram returned a different channel or invalid post identifier.')
            self.current = {'id': int(match[1]), 'published_at': None, 'parts': [], 'media': None, 'depth': depth}
            self.post_count += 1
            if self.post_count > 100: raise NewsError('Telegram returned too many preview posts.')
        if self.current is None: return
        if tag in ('script', 'style') and self.skip_depth is None: self.skip_depth = depth
        if 'tgme_widget_message_text' in classes:
            if self.text_depth is not None: raise NewsError('Telegram changed its message text layout.')
            self.text_depth = depth
        if tag == 'br' and self.text_depth is not None and self.skip_depth is None: self.current['parts'].append('\n')
        if tag == 'a' and 'tgme_widget_message_date' in classes:
            if data.get('href', '').rstrip('/') == CHANNEL_URL + '/' + str(self.current['id']): self.date_depth = depth
        if tag == 'time' and self.date_depth is not None: self.current['published_at'] = valid_timestamp(data.get('datetime'))
        if 'tgme_widget_message_photo_wrap' in classes: self.current['media'] = 'Photo post'
        if any('video' in cls for cls in classes): self.current['media'] = 'Video post'
        if any('document_wrap' in cls for cls in classes): self.current['media'] = 'Document post'

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID: self.handle_endtag(tag)

    def handle_data(self, text):
        if self.current is not None and self.text_depth is not None and self.skip_depth is None: self.current['parts'].append(text)

    def handle_endtag(self, tag):
        if tag not in self.stack: return
        depth = len(self.stack) - self.stack[::-1].index(tag)
        if self.current is not None:
            if self.text_depth is not None and depth <= self.text_depth: self.text_depth = None
            if self.date_depth is not None and depth <= self.date_depth: self.date_depth = None
            if self.skip_depth is not None and depth <= self.skip_depth: self.skip_depth = None
            if depth <= self.current['depth']:
                text = ''.join(self.current['parts'])
                text = '\n'.join(re.sub(r'[^\S\n]+', ' ', line).strip() for line in text.splitlines()).strip()
                if not self.current['published_at']: raise NewsError('Telegram changed the post timestamp layout.')
                if len(text) > 20_000: raise NewsError('Telegram returned an oversized post.')
                if not text: text = self.current['media'] or 'Media post — open in Telegram'
                self.items.append({'id': self.current['id'], 'text': text, 'published_at': self.current['published_at'], 'url': CHANNEL_URL + '/' + str(self.current['id'])})
                self.current = None; self.text_depth = self.date_depth = self.skip_depth = None
        del self.stack[depth - 1:]


def parse_preview(body, allow_empty=False):
    if not isinstance(body, str) or len(body) > MAX_DOWNLOAD: raise NewsError('Invalid Telegram preview response.')
    parser = PreviewParser(); parser.feed(body); parser.close()
    if parser.current is not None: raise NewsError('Telegram preview contains an incomplete post.')
    if not parser.items and not (allow_empty and parser.channel_meta and parser.channel_layout and not parser.stack):
        raise NewsError('Telegram preview contains no readable posts or its layout changed.')
    unique = {}
    for item in parser.items:
        if item['id'] in unique and unique[item['id']] != item: raise NewsError('Telegram returned conflicting duplicate posts.')
        unique[item['id']] = item
    return sorted(unique.values(), key=lambda item: item['id'], reverse=True)


def checked_item(item):
    if not isinstance(item, dict) or type(item.get('id')) is not int or not 0 < item['id'] < 1_000_000_000_000: raise NewsError('Invalid saved Telegram post identifier.')
    if not isinstance(item.get('text'), str) or not 0 < len(item['text']) <= 20_000: raise NewsError('Invalid saved Telegram text.')
    expected = CHANNEL_URL + '/' + str(item['id'])
    if item.get('url') != expected: raise NewsError('Saved Telegram link left the channel.')
    return {'id': item['id'], 'text': item['text'], 'published_at': valid_timestamp(item.get('published_at')), 'url': expected}


class TelegramNewsService:
    def __init__(self, cache_path, enabled=True, transport=None, monotonic=None, history_transport=None):
        self.cache_path = Path(cache_path); self.enabled = enabled; self.transport = transport or fetch_preview
        self.monotonic = monotonic or time.monotonic; self.lock = RLock(); self.next_check = 0
        self.history_transport = history_transport or (lambda before: fetch_preview(before))
        self.next_history_check = 0; self.history_exhausted = False; self.history_cursor = None; self.history_error = self.history_error_code = None
        self.items = []; self.last_checked_at = self.last_attempt_at = None; self.error = self.error_code = None; self.cache_warning = None
        try:
            if self.cache_path.stat().st_size > MAX_CACHE_BYTES: raise NewsError('Saved news cache is oversized.')
            data = json.loads(self.cache_path.read_text(encoding='utf-8'))
            if data.get('version') != 1 or data.get('channel') != CHANNEL or not isinstance(data.get('items'), list): raise NewsError('Invalid news cache.')
            if len(data['items']) > MAX_ITEMS: raise NewsError('Oversized news history.')
            self.items = sorted({item['id']: checked_item(item) for item in data['items']}.values(), key=lambda item: item['id'], reverse=True)
            self.last_checked_at = valid_timestamp(data['last_checked_at'])
            self.history_exhausted = data.get('history_exhausted') is True
            cursor = data.get('history_cursor')
            self.history_cursor = cursor if type(cursor) is int and 0 < cursor < 1_000_000_000_000 else self.items[-1]['id'] if self.items else None
        except (OSError, ValueError, TypeError, KeyError, NewsError): self.items = []; self.last_checked_at = None

    def _save(self):
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True); temporary = self.cache_path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'version': 1, 'channel': CHANNEL, 'last_checked_at': self.last_checked_at,
                                            'items': self.items, 'history_exhausted': self.history_exhausted, 'history_cursor': self.history_cursor}, ensure_ascii=False), encoding='utf-8')
            temporary.replace(self.cache_path)
        except OSError: self.cache_warning = 'The news is available, but recent posts could not be saved locally.'

    def _rate_limit(self, exc):
        if not isinstance(exc, HTTPError) or exc.code != 429: return
        try: delay = min(300, max(POLL_SECONDS, int(exc.headers.get('Retry-After', POLL_SECONDS))))
        except (ValueError, TypeError): delay = 60
        deadline = self.monotonic() + delay
        self.next_check = max(self.next_check, deadline); self.next_history_check = max(self.next_history_check, deadline)

    def _result(self):
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(self.last_checked_at)).total_seconds() if self.last_checked_at else None
        status = 'disabled' if not self.enabled else 'unavailable' if not self.items else 'stale' if self.error or age is None or age > 30 else 'ready'
        return {'channel': {'username': CHANNEL, 'title': 'Market News Feed', 'url': CHANNEL_URL, 'preview_url': PREVIEW_URL},
                'status': status, 'items': deepcopy(self.items), 'poll_interval_seconds': POLL_SECONDS,
                'last_checked_at': self.last_checked_at, 'last_attempt_at': self.last_attempt_at,
                'retry_after_seconds': max(0, math.ceil(self.next_check - self.monotonic())),
                'error': self.error if self.enabled else 'The Telegram news connection is disabled on this server.',
                'error_code': self.error_code if self.enabled else 'disabled', 'cache_warning': self.cache_warning,
                'history_limit': MAX_ITEMS, 'has_more_history': self.enabled and bool(self.items) and not self.history_exhausted and (len(self.items) < MAX_ITEMS or self.history_cursor is not None and self.history_cursor > self.items[-1]['id']),
                'history_before': self.history_cursor,
                'oldest_id': self.items[-1]['id'] if self.items else None, 'history_exhausted': self.history_exhausted,
                'history_error': self.history_error, 'history_error_code': self.history_error_code,
                'history_retry_after_seconds': max(0, math.ceil(self.next_history_check - self.monotonic()))}

    def load_history(self, before):
        if type(before) is not int or not 0 < before < 1_000_000_000_000: raise NewsError('Invalid Telegram history cursor.')
        with self.lock:
            # Only the tracked history cursor is allowed. Concurrent users
            # receive an already-expanded archive instead of downloading twice.
            if (not self.enabled or not self.items or self.history_exhausted or
                len(self.items) >= MAX_ITEMS and before <= self.items[-1]['id'] or
                before != self.history_cursor or self.monotonic() < self.next_history_check): return self._result()
            if before == 1:
                self.history_exhausted = True; self.history_error = self.history_error_code = None; self._save(); return self._result()
            self.next_history_check = self.monotonic() + HISTORY_INTERVAL
            try:
                page = parse_preview(self.history_transport(before), allow_empty=True)
                older = [item for item in page if item['id'] < before]
                if page and not older: raise NewsError('Telegram did not return an older page. Saved news is retained; retry loading older posts.')
                if not page: self.history_exhausted = True
                if older: self.history_cursor = min(item['id'] for item in older)
                merged = {item['id']: item for item in self.items}; merged.update({item['id']: item for item in older})
                self.items = sorted(merged.values(), key=lambda item: item['id'], reverse=True)[:MAX_ITEMS]
                self.history_error = self.history_error_code = None; self.cache_warning = None; self._save()
            except (OSError, HTTPException, ValueError, TypeError, KeyError, NewsError) as exc:
                self.history_error_code, self.history_error = connection_failure(exc)
                self.next_history_check = max(self.next_history_check, self.monotonic() + POLL_SECONDS); self._rate_limit(exc)
            # Loading old messages never advances the latest-feed check time or
            # clears an outage in the independent ten-second live update path.
            return self._result()

    def view(self):
        with self.lock:
            attempt_started = self.monotonic()
            if self.enabled and attempt_started >= self.next_check:
                self.last_attempt_at = datetime.now(timezone.utc).isoformat(); self.cache_warning = None
                try:
                    newest = parse_preview(self.transport())
                    if self.items and max(item['id'] for item in newest) < self.items[0]['id']: raise NewsError('Telegram returned an older preview; saved news has been retained.')
                    if not self.items or (not {item['id'] for item in newest}.intersection(item['id'] for item in self.items) and min(item['id'] for item in newest) > self.items[0]['id'] + 1):
                        self.history_cursor = min(item['id'] for item in newest); self.history_exhausted = False
                    merged = {item['id']: item for item in self.items}; merged.update({item['id']: item for item in newest})
                    self.items = sorted(merged.values(), key=lambda item: item['id'], reverse=True)[:MAX_ITEMS]
                    self.last_checked_at = datetime.now(timezone.utc).isoformat(); self.error = self.error_code = None
                    self._save()
                except (OSError, HTTPException, ValueError, TypeError, KeyError, NewsError) as exc:
                    self.error_code, self.error = connection_failure(exc)
                    self._rate_limit(exc)
                self.next_check = max(self.next_check, attempt_started + POLL_SECONDS)
            return self._result()
