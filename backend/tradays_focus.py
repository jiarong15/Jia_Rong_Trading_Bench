"""Resolve existing widget anchors; economic values stay inside the live iframe."""
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
import json
import re
from threading import RLock
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SGT = timezone(timedelta(hours=8))
WIDGET = 'https://www.tradays.com/en/economic-calendar/widget'


def fetch_widget(today):
    params = {'mode': '2', 'date_mode': '0', 'importance': '8',
              'from': str(today-timedelta(days=1))+'T00:00:00',
              'to': str(today+timedelta(days=1))+'T23:59:59',
              'dateFormat': 'DMY', 'utm_source': 'jr-trading-workbench'}
    request = Request(WIDGET+'?'+urlencode(params), headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'text/html'})
    with urlopen(request, timeout=8) as response:
        body = response.read(2_000_001)
    if len(body) > 2_000_000: raise ValueError('Oversized widget response.')
    return body.decode('utf-8')


def parse_targets(body, today):
    before, after = today-timedelta(days=1), today+timedelta(days=1)
    mode = re.search(r'Calendar.checkMode\("selectInterval",\s*0,\s*(\d+),\s*(\d+)\)', body)
    if not mode: raise ValueError('Widget date range missing.')
    dates = [datetime.fromtimestamp(int(value)/1000, timezone.utc).date() for value in mode.groups()]
    if dates != [before, after]: raise ValueError('Widget returned a different date range.')
    match = re.search(r'Calendar.Data\s*=\s*(\[[\s\S]*?\]);', body)
    if not match: raise ValueError('Widget event data missing.')
    rows = json.loads(match.group(1))
    if not isinstance(rows, list) or len(rows) > 5000: raise ValueError('Invalid widget data.')
    targets = {str(today+timedelta(days=offset)): None for offset in (-1, 0, 1)}
    first = {}
    for row in rows:
        if row['Importance'] != 'high' or type(row['Id']) is not int or not 0 < row['Id'] < 10**12:
            raise ValueError('Invalid widget event.')
        if type(row['ReleaseDate']) is not int: raise ValueError('Invalid event time.')
        day = datetime.fromtimestamp(row['ReleaseDate']/1000, SGT).date().isoformat()
        if day in targets and (day not in first or row['ReleaseDate'] < first[day]):
            first[day] = row['ReleaseDate']
            targets[day] = 'prevValue'+str(row['Id'])
    return targets


class TradaysFocusService:
    def __init__(self, enabled=True, transport=None, clock=None):
        self.enabled = enabled
        self.transport = transport or fetch_widget
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = RLock()
        self.cached = None
        self.attempt = None

    def view(self):
        with self.lock:
            now = self.clock()
            today = now.astimezone(SGT).date()
            if not self.enabled:
                return {'status': 'disabled', 'today': str(today), 'targets': {}, 'error': 'Calendar date navigation is disabled on this server.'}
            if self.cached and self.cached['today'] == str(today) and self.attempt is not None:
                ttl = 300 if self.cached['status'] == 'ready' else 30
                if (now-self.attempt).total_seconds() < ttl: return {**self.cached, 'targets': dict(self.cached['targets'])}
            self.attempt = now
            try:
                targets = parse_targets(self.transport(today), today)
                self.cached = {'status': 'ready', 'today': str(today), 'targets': targets, 'error': None}
            except (OSError, HTTPException, ValueError, KeyError, TypeError, OverflowError):
                self.cached = {'status': 'unavailable', 'today': str(today), 'targets': {},
                               'error': 'Could not locate that date in Tradays. The calendar remains available; try the date button again.'}
            return {**self.cached, 'targets': dict(self.cached['targets'])}
