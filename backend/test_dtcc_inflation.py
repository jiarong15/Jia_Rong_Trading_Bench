from copy import deepcopy
from datetime import datetime, timedelta, timezone
import csv
import io
import json
from threading import Event
import zipfile

import pytest

from .dtcc_inflation import (BASE, BASIS, FIELDS, DTCCInflationSource, aggregate, candidate,
                             file_identity, parse_archive, parse_index, public_bytes)
from .us_rates import USRatesService
from .test_us_rates import Source as FredSource

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
DAILY = 'CFTC_CUMULATIVE_RATES_2026_10_30.zip'
SLICE = 'CFTC_SLICE_RATES_2026_10_31_1.zip'


def trade(identifier='100', day='2026-10-30', rate='0.025', **updates):
    row = dict.fromkeys(FIELDS, '')
    row.update({'Dissemination Identifier': identifier, 'Action type': 'NEWT', 'Event type': 'TRAD',
                'Event timestamp': day + 'T16:00:00Z', 'Execution Timestamp': day + 'T15:59:00Z',
                'Effective Date': '2026-11-03', 'Expiration Date': '2031-11-03',
                'Notional currency-Leg 1': 'USD', 'Notional currency-Leg 2': 'USD',
                'UPI Underlier Name': 'USA-CPI-U', 'UPI FISN': 'NA/Swap Infl Idx USD',
                'Fixed rate-Leg 1': rate, 'Package indicator': 'FALSE',
                'Fixed rate payment frequency period-Leg 1': 'YEAR',
                'Fixed rate payment frequency period multiplier-Leg 1': '5',
                'Floating rate payment frequency period-Leg 2': 'YEAR',
                'Floating rate payment frequency period multiplier-Leg 2': '5'})
    row.update(updates)
    return row


def archive(rows, fields=None):
    text = io.StringIO(); writer = csv.DictWriter(text, fieldnames=sorted(fields or FIELDS))
    writer.writeheader(); writer.writerows(rows)
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr('rates.csv', text.getvalue())
    return data.getvalue()


def metadata(name, count, at='2026-10-31T01:00:00Z'):
    return {'fileName': name, 'rowCount': count, 'dissemDTM': at, 'fullFilePath': 'https://untrusted.invalid/never-follow'}


def test_public_download_is_keyless_bounded_and_releases_timed_out_response(monkeypatch):
    class Response:
        closed = False
        def __enter__(self): return self
        def __exit__(self, *args): self.closed = True
        def read1(self, size):
            assert size == 65536
            return next(chunks)
    response = Response()
    class Opener:
        def open(self, request, timeout):
            assert timeout == 12 and request.full_url == BASE+'test'
            assert request.get_header('Authorization') is None
            return response
    monkeypatch.setattr('backend.dtcc_inflation.build_opener', lambda *args: Opener())
    monkeypatch.setattr('backend.dtcc_inflation.tls_context', lambda: None)
    ticks = iter([0, 1, 2, 3]); chunks = iter([b'one', b'two', b''])
    monkeypatch.setattr('backend.dtcc_inflation.monotonic', lambda: next(ticks))
    assert public_bytes(BASE+'test') == b'onetwo' and response.closed
    response.closed = False; ticks = iter([0, 1, 31]); chunks = iter([b'partial'])
    with pytest.raises(TimeoutError): public_bytes(BASE+'test')
    assert response.closed


class Tape:
    def __init__(self):
        self.calls = []; self.fail = set()
        self.rows = {DAILY: [trade()], SLICE: [trade('200', '2026-10-31', '0.03', **{
            'Event timestamp': '2026-10-31T10:00:00Z', 'Execution Timestamp': '2026-10-31T09:59:00Z'})]}
        self.daily = [metadata(DAILY, 1)]; self.slices = [metadata(SLICE, 1, '2026-10-31T10:01:00Z')]

    def __call__(self, url):
        self.calls.append(url)
        if any(key in url for key in self.fail):
            raise ValueError('PRIVATE_FAILURE_' + url)
        if url.endswith('bucketname'):
            return b'kgc0418-tdw-data-0'
        if url.endswith('Cumulative.json'):
            return json.dumps({'CFTC_IR': self.daily}).encode()
        if url.endswith('Slice.json'):
            return json.dumps({'CFTC_IR': self.slices}).encode()
        return archive(self.rows[url.rsplit('/', 1)[-1]])


def test_exact_instrument_units_and_single_payment_screen():
    assert candidate(trade(), NOW)['value'] == 2.5
    expiry = trade(**{'Fixed rate payment frequency period-Leg 1': 'EXPI', 'Floating rate payment frequency period-Leg 2': 'EXPI'})
    assert candidate(expiry, NOW)['value'] == 2.5
    negative = trade(rate='-0.001')
    assert candidate(negative, NOW)['value'] == -.1
    zero = trade(rate='0')
    assert candidate(zero, NOW)['value'] == 0
    leg2 = trade(**{'Fixed rate-Leg 1': '', 'Fixed rate-Leg 2': '.028',
        'Fixed rate payment frequency period-Leg 2': 'MNTH', 'Fixed rate payment frequency period multiplier-Leg 2': '60',
        'Floating rate payment frequency period-Leg 1': 'EXPI'})
    assert candidate(leg2, NOW)['value'] == pytest.approx(2.8)


@pytest.mark.parametrize('updates', [
    {'UPI Underlier Name': 'EUR-EXT-CPI'}, {'UPI FISN': 'NA/Swap Fixed Float USD'},
    {'Notional currency-Leg 2': 'EUR'}, {'Event type': 'COMP'},
    {'Package indicator': 'TRUE'}, {'Package indicator': ''}, {'Other payment amount': '10.25'},
    {'Expiration Date': '2036-11-03'}, {'Effective Date': '2025-11-03', 'Expiration Date': '2030-11-03'},
    {'Fixed rate payment frequency period multiplier-Leg 1': '1'},
    {'Floating rate payment frequency period multiplier-Leg 2': '1'}, {'Fixed rate-Leg 2': '.01'},
])
def test_excludes_wrong_index_tenor_annual_coupons_seasoned_packages_and_upfronts(updates):
    assert candidate(trade(**updates), NOW) is None


def test_corrections_errors_duplicate_slices_and_lifecycle_events():
    first = trade('100'); second = trade('101', rate='.027')
    correction = trade('102', rate='.031', **{'Action type': 'CORR', 'Original Dissemination Identifier': '100', 'Event timestamp': '2026-10-30T17:00:00Z'})
    eror = trade('103', **{'Action type': 'EROR', 'Original Dissemination Identifier': '101', 'Event timestamp': '2026-10-30T18:00:00Z'})
    term = trade('104', rate='.5', **{'Action type': 'TERM', 'Original Dissemination Identifier': '102'})
    modi = trade('105', rate='.4', **{'Action type': 'MODI', 'Original Dissemination Identifier': '102'})
    events = parse_archive(archive([first, second, correction, eror, term, modi]), NOW, 6)
    values, details = aggregate({'one': {'events': events}, 'duplicate_slice': {'events': events}})
    assert values == {'2026-10-30': 3.1}
    assert details['2026-10-30']['trade_count'] == 1
    assert details['2026-10-30']['last_trade_at'] == '2026-10-30T15:59:00+00:00'
    # A correction making a formerly eligible trade a package removes it.
    correction['Package indicator'] = 'TRUE'
    changed, _ = aggregate({'one': {'events': parse_archive(archive([first, second, correction, eror]), NOW)}})
    assert changed == {}


def test_rejects_truncated_files_changed_schema_duplicate_identity_future_and_nonfinite_values():
    with pytest.raises(ValueError):
        parse_archive(archive([trade()]), NOW, 2)
    with pytest.raises(ValueError):
        parse_archive(archive([] , FIELDS - {'UPI Underlier Name'}), NOW)
    with pytest.raises(ValueError):
        parse_archive(archive([trade(), trade()]), NOW)
    with pytest.raises(ValueError):
        parse_archive(archive([trade(rate='NaN')]), NOW)
    with pytest.raises(ValueError):
        parse_archive(archive([trade(**{'Execution Timestamp': '2027-01-01T00:00:00Z'})]), NOW)
    malicious = io.BytesIO()
    with zipfile.ZipFile(malicious, 'w') as zipped:
        zipped.writestr('one.csv', 'x'); zipped.writestr('two.csv', 'x')
    with pytest.raises(ValueError):
        parse_archive(malicious.getvalue(), NOW)


def test_index_uses_validated_names_counts_and_publication_times():
    result = parse_index(json.dumps({'CFTC_IR': [metadata(DAILY, 1)]}), 'CUMULATIVE', NOW)
    assert result[DAILY]['day'] == '2026-10-30' and 'fullFilePath' not in result[DAILY]
    for name in ('../secrets.zip', 'CFTC_CUMULATIVE_RATES_2026_02_30.zip', 'CFTC_SLICE_RATES_2026_10_31.zip'):
        with pytest.raises(ValueError):
            file_identity(name)
    with pytest.raises(ValueError):
        parse_index(json.dumps({'CFTC_IR': [metadata(DAILY, 2, '2027-01-01T00:00:00Z')]}), 'CUMULATIVE', NOW)


def test_automatic_poll_new_trade_correction_outage_recovery_and_saved_restart(tmp_path):
    tape = Tape(); now = [NOW]; path = tmp_path/'dtcc.json'
    source = DTCCInflationSource(path, transport=tape, clock=lambda: now[0])
    source.refresh(); before = source.snapshot()
    assert before['values'] == {'2026-10-30': 2.5, '2026-10-31': 3}
    assert before['status'] == 'ready' and before['last_trade_at'] == '2026-10-31T09:59:00+00:00'
    calls = len(tape.calls); now[0] += timedelta(seconds=59); source.refresh(); assert len(tape.calls) == calls
    new_name = 'CFTC_SLICE_RATES_2026_10_31_2.zip'
    tape.rows[new_name] = [trade('201', '2026-10-31', '.04', **{'Event timestamp': '2026-10-31T11:00:00Z', 'Execution Timestamp': '2026-10-31T10:59:00Z'})]
    tape.slices.insert(0, metadata(new_name, 1, '2026-10-31T11:01:00Z'))
    now[0] += timedelta(seconds=1); source.refresh(); updated = source.snapshot()
    assert updated['values']['2026-10-31'] == 3.5 and updated['observation_details']['2026-10-31']['trade_count'] == 2
    assert updated['last_trade_at'] == '2026-10-31T10:59:00+00:00'
    # A fresh check without new trades advances check time, never trade time.
    now[0] += timedelta(seconds=60); source.refresh(); unchanged = source.snapshot()
    assert unchanged['checked_at'] != updated['checked_at'] and unchanged['last_trade_at'] == updated['last_trade_at']
    tape.fail.add('Slice.json'); now[0] += timedelta(seconds=60); source.refresh(); failed = source.snapshot()
    assert failed['status'] == 'stale' and failed['values'] == updated['values'] and failed['checked_at'] == unchanged['checked_at']
    assert 'PRIVATE_FAILURE' not in json.dumps(failed)
    saved = DTCCInflationSource(path, transport=lambda url: pytest.fail('Saved snapshot must not fetch'), clock=lambda: now[0])
    assert saved.snapshot()['values'] == updated['values']
    assert all('events' not in key for key in saved.snapshot())
    tape.fail.clear(); now[0] += timedelta(seconds=60); source.refresh(); assert source.snapshot()['status'] == 'ready'


def test_new_daily_file_supersedes_slices_and_corrects_prior_day(tmp_path):
    tape = Tape(); source = DTCCInflationSource(tmp_path/'dtcc.json', transport=tape, clock=lambda: NOW)
    source.refresh()
    now = NOW + timedelta(days=1)
    name = 'CFTC_CUMULATIVE_RATES_2026_10_31.zip'
    corrected = trade('201', '2026-10-31', '.035', **{'Action type': 'CORR', 'Original Dissemination Identifier': '200',
        'Event timestamp': '2026-10-31T11:00:00Z', 'Execution Timestamp': '2026-10-31T09:59:00Z'})
    tape.rows[name] = tape.rows[SLICE] + [corrected]
    tape.daily.insert(0, metadata(name, 2, '2026-11-01T00:15:00Z'))
    source.refresh(now, force=True)
    assert source.snapshot()['values']['2026-10-31'] == 3.5
    assert SLICE not in source.files and source.snapshot()['observation_details']['2026-10-31']['trade_count'] == 1


def test_pending_live_files_are_explicit_and_failed_files_are_retried(tmp_path, monkeypatch):
    monkeypatch.setattr('backend.dtcc_inflation.SLICE_BATCH', 1)
    tape = Tape(); name = 'CFTC_SLICE_RATES_2026_10_31_2.zip'
    tape.rows[name] = [trade('201', '2026-10-31', '.04', **{'Event timestamp': '2026-10-31T11:00:00Z', 'Execution Timestamp': '2026-10-31T10:59:00Z'})]
    tape.slices.insert(0, metadata(name, 1, '2026-10-31T11:01:00Z'))
    source = DTCCInflationSource(tmp_path/'dtcc.json', transport=tape, clock=lambda: NOW)
    source.refresh(); assert source.snapshot()['status'] == 'catching_up' and source.snapshot()['pending_live_files'] == 1
    tape.fail.add(SLICE); source.refresh(NOW+timedelta(seconds=60)); assert source.snapshot()['status'] == 'stale'
    tape.fail.clear(); source.refresh(NOW+timedelta(seconds=120))
    assert source.snapshot()['status'] == 'ready' and source.snapshot()['values']['2026-10-31'] == 3.5


def test_background_updates_without_browser_requests_and_stops_cleanly(tmp_path):
    tape = Tape(); now = [NOW]; waiting = Event(); release = Event(); updated = Event(); waits = []
    source = DTCCInflationSource(tmp_path/'dtcc.json', transport=tape, clock=lambda: now[0])
    def wait(seconds):
        assert 0 < seconds <= 60
        waits.append(1)
        if len(waits) == 1:
            waiting.set(); assert release.wait(3); return False
        updated.set(); return True
    source.stop.wait = wait
    try:
        source.start(); source.start(); assert waiting.wait(3)
        tape.rows[DAILY][0]['Fixed rate-Leg 1'] = '.028'
        # A new correction is delivered in the next published slice.
        name = 'CFTC_SLICE_RATES_2026_10_31_2.zip'
        tape.rows[name] = [trade('300', rate='.028', **{'Action type': 'CORR', 'Original Dissemination Identifier': '100', 'Event timestamp': '2026-10-31T11:00:00Z'})]
        tape.slices.append(metadata(name, 1, '2026-10-31T11:01:00Z'))
        now[0] += timedelta(seconds=60); release.set(); assert updated.wait(3)
        assert source.snapshot()['values']['2026-10-30'] == 2.8
    finally:
        release.set(); source.close()
    assert not source.thread.is_alive()


def test_rates_panel_merges_live_swap_without_persisting_or_exporting_trade_records(tmp_path, monkeypatch):
    monkeypatch.delenv('RATES_INFLATION_SWAP_URL', raising=False)
    tape = Tape(); source = DTCCInflationSource(tmp_path/'dtcc.json', transport=tape, clock=lambda: NOW)
    rates = USRatesService(tmp_path/'rates.json', transport=FredSource(), clock=lambda: NOW, api_key='', swap_source=source)
    document = rates.view(); swap = document['charts'][4]['series'][0]
    assert swap['status'] == 'ready' and swap['last_value'] == 3 and document['status'] == 'ready'
    assert swap['last_observation_at'] == '2026-10-31T09:59:00+00:00' and swap['trade_count'] == 1
    assert swap['delivery'] == 'reported_transactions' and 'trade median' in swap['label']
    assert 'events' not in json.dumps(document) and 'Dissemination Identifier' not in json.dumps(document)
    assert 'USD_ZCIS_5Y' not in json.loads((tmp_path/'rates.json').read_text())['values']
    before = len(tape.calls); rates.view(saved_only=True); assert len(tape.calls) == before


def test_disabled_source_never_fetches_or_starts(tmp_path):
    source = DTCCInflationSource(tmp_path/'dtcc.json', enabled=False, transport=lambda url: pytest.fail('Disabled source fetched'))
    source.start(); source.refresh()
    assert source.thread is None and source.snapshot()['status'] == 'disabled' and not source.snapshot()['values']
