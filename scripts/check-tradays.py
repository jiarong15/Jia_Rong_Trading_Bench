"""Fresh read-only probe of the exact official widget used by the frontend."""
from datetime import datetime, timedelta, timezone
import json
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def get(url):
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Cache-Control': 'no-cache'})
    with urlopen(request, timeout=20) as response:
        body = response.read(2_000_001)
        if len(body) > 2_000_000: raise ValueError('Provider response too large.')
        return body.decode('utf-8')


def main():
    today = datetime.now(timezone(timedelta(hours=8))).date()
    before, after = today-timedelta(days=1), today+timedelta(days=1)
    params = {'mode': '2', 'date_mode': '0', 'importance': '8', 'from': str(before)+'T00:00:00',
              'to': str(after)+'T23:59:59', 'dateFormat': 'DMY', 'utm_source': 'jr-trading-workbench'}
    base = 'https://www.tradays.com'
    body = get(base+'/en/economic-calendar/widget?'+urlencode(params))
    match = re.search(r'Calendar.Data\s*=\s*(\[[\s\S]*?\]);', body)
    if not match: raise ValueError('Widget did not supply event data.')
    rows = json.loads(match.group(1))
    if not isinstance(rows, list) or any(row['Importance'] != 'high' for row in rows):
        raise ValueError('The source did not enforce High impact.')
    mode = re.search(r'Calendar.checkMode\("selectInterval",\s*0,\s*(\d+),\s*(\d+)\)', body)
    if not mode: raise ValueError('Widget did not accept the custom date range.')
    selected = [datetime.fromtimestamp(int(stamp)/1000, timezone.utc).date() for stamp in mode.groups()]
    if selected != [before, after]: raise ValueError('Widget returned a different date range.')
    asset = re.search(r'<script[^>]+src="(/c/js/widgets/calendar/calendar\.[a-f0-9]+\.js)"', body)
    if not asset or 'new CalendarUpdater(valueUrl, Calendar.Data)' not in body:
        raise ValueError('Widget release updater is missing.')
    runtime = get(base+asset.group(1))
    if not all(marker in runtime for marker in ('function CalendarUpdater(', 'function RefreshTask(', 'ActualValue', 'window.Ajax.get(t,{ids:n}')):
        raise ValueError('Vendor updater changed; inspect the current release-update mechanism.')
    print('PASS: live official widget response, high-impact-only data, exact three-day range, and vendor actual-result updater.')
    print('Singapore range:', before, 'through', after)
    print('High-impact records supplied:', len(rows), '| Records with actual values:', sum(bool(row.get('ActualValue')) for row in rows))
    print('This verifies provider responses and update code; it does not measure release-to-display latency or browser connectivity.')


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, TypeError, KeyError):
        raise SystemExit('FAIL: Tradays could not be reached or its widget contract changed. Check this machine\'s network and the provider response.') from None
