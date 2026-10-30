"""Check Telegram news on this computer; requires only Python's standard library.

Exit 0 means a fresh public preview was downloaded and parsed successfully.
Exit 1 means source access/preview validation failed. No headlines are saved.
--dashboard additionally checks the running app's news endpoint.
"""
import argparse
from datetime import datetime, timezone
from http.client import HTTPException
import json
from pathlib import Path
import sys
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.telegram_news import check_connection


def dashboard_check(base_url):
    parsed = urlsplit(base_url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return {'status': 'unavailable', 'error': 'Use a dashboard base URL such as http://127.0.0.1:8000, without credentials, queries or fragments.'}
    try:
        with urlopen(base_url.rstrip('/') + '/api/news/telegram', timeout=20) as response:
            if response.headers.get('Cache-Control') != 'no-store': raise ValueError('The server is not serving the expected uncached news endpoint.')
            body = response.read(5_000_001)
            if len(body) > 5_000_000: raise ValueError('The dashboard news response is oversized.')
            feed = json.loads(body)
        if not isinstance(feed, dict): raise ValueError('Invalid news response.')
        if feed.get('status') != 'ready' or feed.get('channel', {}).get('username') != 'marketfeed' or not isinstance(feed.get('items'), list) or not feed['items']:
            return {'status': 'unavailable', 'error_code': feed.get('error_code'), 'error': feed.get('error') or 'The dashboard has no current, successfully checked channel posts.'}
        stamp = datetime.fromisoformat((feed.get('last_checked_at') or '').replace('Z', '+00:00'))
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        if not -300 <= age <= 30:
            return {'status': 'unavailable', 'error_code': feed.get('error_code'), 'error': feed.get('error') or 'The dashboard has no current, successfully checked channel posts.'}
        return {'status': 'ready', 'post_count': len(feed['items']), 'last_checked_at': feed['last_checked_at'], 'poll_interval_seconds': feed['poll_interval_seconds']}
    except (OSError, HTTPException, ValueError, TypeError, KeyError, AttributeError, URLError):
        return {'status': 'unavailable', 'error': 'The dashboard endpoint could not be verified. Start the backend with python launch.py and check the supplied dashboard address.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true', help='Print a machine-readable report without headline text.')
    parser.add_argument('--dashboard', metavar='BASE_URL', help='Also verify the running dashboard, for example http://127.0.0.1:8000.')
    args = parser.parse_args(argv); report = check_connection()
    if args.dashboard: report['dashboard'] = dashboard_check(args.dashboard)
    passed = report['status'] == 'ready' and (not args.dashboard or report['dashboard']['status'] == 'ready')
    if args.json: print(json.dumps(report, indent=2))
    else:
        print(('PASS' if report['status'] == 'ready' else 'FAIL') + ': Telegram public news connection')
        print('Source: ' + report['preview_url'])
        if report['status'] == 'ready':
            print(f"Downloaded and parsed {report['post_count']} posts. Latest post: {report['latest_post_id']}; published {report['latest_published_at']}.")
        else: print(f"{report['error_code']}: {report['error']}")
        if args.dashboard:
            result = report['dashboard']; print(('PASS' if result['status'] == 'ready' else 'FAIL') + ': Dashboard news API')
            if result.get('error'): print(result['error'])
        print('The dashboard polls every 10 seconds while visible. This check does not certify browser interaction or future Telegram availability.')
    return 0 if passed else 1


if __name__ == '__main__': raise SystemExit(main())
