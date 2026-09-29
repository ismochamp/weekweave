import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import app


def calendar(events):
    return 'BEGIN:VCALENDAR\nVERSION:2.0\n' + events + '\nEND:VCALENDAR\n'


def event(uid='one', start='20260928T080000Z', end='20260928T090000Z', extra='', summary='A meeting'):
    return f'BEGIN:VEVENT\nUID:{uid}\nDTSTART:{start}\nDTEND:{end}\nSUMMARY:{summary}\n{extra}\nEND:VEVENT'


class CalendarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        app.PORT = cls.server.server_address[1]
        cls.base = f'http://127.0.0.1:{app.PORT}'
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DB = Path(self.tmp.name) / 'test.sqlite3'
        app.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, path, body=None, headers=None):
        req = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, headers={'Content-Type': 'application/json', **(headers or {})})
        with urlopen(req, timeout=10) as response:
            return response.read()

    def state(self, start='2026-09-28', end='2026-10-05', zone='Europe/Berlin'):
        return app.get_state(zone, start, end)

    def test_real_http_feed_merge_timezone_conflicts_and_exports(self):
        for name, filename in [('Studio', 'studio.ics'), ('Operations', 'operations.ics')]:
            result = json.loads(self.request('/api/import', {'name': name, 'url': self.base + '/fixtures/' + filename, 'timezone': 'Europe/Berlin'}))
            self.assertEqual(result['imported'], 5)
        state = self.state()
        self.assertEqual((len(state['events']), len(state['conflicts']), state['duplicates_merged']), (9, 2, 1))
        partner = next(e for e in state['events'] if e['summary'] == 'Partner onboarding')
        self.assertEqual(partner['display_start'], '2026-09-28T10:00:00+02:00')
        exported = self.request('/api/export.ics?start=2026-09-28&end=2026-10-05').decode()
        self.assertEqual(len(app.parse_ics(exported)), 9)
        self.assertIn(b'Partner onboarding', self.request('/api/conflicts.csv?start=2026-09-28&end=2026-10-05'))

    def test_unsupported_recurrence_rejects_entire_replace(self):
        app.import_source('Work', calendar(event()), 'UTC')
        recurring = calendar(event(uid='new') + '\n' + event(uid='repeat', extra='RRULE:FREQ=DAILY'))
        with self.assertRaisesRegex(ValueError, 'RRULE is not supported'):
            app.import_source('Work', recurring, 'UTC', replace=True)
        self.assertEqual([e['uid'] for e in self.state()['events']], ['one'])

    def test_replacement_requires_explicit_flag(self):
        app.import_source('Work', calendar(event()), 'UTC')
        with self.assertRaisesRegex(ValueError, 'Explicitly enable'):
            app.import_source('work', calendar(event(uid='two')), 'UTC')
        app.import_source('Work', calendar(event(uid='two')), 'UTC', replace=True)
        self.assertEqual([e['uid'] for e in self.state()['events']], ['two'])

    def test_dst_gap_and_ambiguity_rejected(self):
        for value in ['20260329T023000', '20261025T023000']:
            with self.assertRaisesRegex(ValueError, 'daylight-saving'):
                app.parse_ics(calendar(event(start=value, end=value)), 'Europe/Berlin')

    def test_dst_repeated_hour_orders_absolute_instants(self):
        text = calendar(event(uid='earlier', start='20261025T004500Z', end='20261025T005500Z') + '\n' + event(uid='later', start='20261025T013000Z', end='20261025T014000Z'))
        app.import_source('Autumn transition', text, 'UTC')
        state = self.state('2026-10-25', '2026-10-26')
        self.assertEqual([e['uid'] for e in state['events']], ['earlier', 'later'])
        self.assertEqual([e['display_start'] for e in state['events']], ['2026-10-25T02:45:00+02:00', '2026-10-25T02:30:00+01:00'])
        self.assertEqual(state['conflicts'], [])

    def test_unsupported_altrep_rejects_atomic_source_replacement(self):
        app.import_source('Work', calendar(event()), 'UTC')
        text = calendar(event(uid='valid-first') + '\n' + event(uid='external', extra='DESCRIPTION;ALTREP="https://example.invalid/brief":Correct description'))
        with self.assertRaisesRegex(ValueError, 'Unsupported DESCRIPTION parameter'):
            app.import_source('Work', text, 'UTC', replace=True)
        self.assertEqual([e['uid'] for e in self.state()['events']], ['one'])

    def test_all_day_exclusive_end_and_folded_text_roundtrip(self):
        text = calendar('BEGIN:VEVENT\nUID:all-day\nDTSTART;VALUE=DATE:20260928\nDTEND;VALUE=DATE:20260930\nSUMMARY:Long folded\n  title\\, with comma\nEND:VEVENT')
        app.import_source('Dates', text, 'Europe/Berlin')
        state = self.state()
        self.assertEqual(state['events'][0]['summary'], 'Long folded title, with comma')
        exported = app.export_ics(state)
        parsed = app.parse_ics(exported)
        self.assertTrue(parsed[0]['all_day'])
        self.assertEqual(parsed[0]['end'], '2026-09-30')
        self.assertEqual(len(self.state('2026-09-30', '2026-10-01')['events']), 0)

    def test_adjacent_transparent_cancelled_and_zero_length_do_not_conflict(self):
        text = calendar(event() + '\n' + event(uid='adjacent', start='20260928T090000Z', end='20260928T100000Z') + '\n' + event(uid='free', extra='TRANSP:TRANSPARENT') + '\n' + event(uid='cancelled', extra='STATUS:CANCELLED') + '\n' + event(uid='instant', end='20260928T080000Z'))
        app.import_source('Work', text, 'UTC')
        self.assertEqual(len(self.state()['conflicts']), 0)

    def test_overlap_duration_and_window_clipping(self):
        text = calendar(event(start='20260927T220000Z', end='20260928T030000Z') + '\n' + event(uid='two', start='20260927T230000Z', end='20260928T020000Z'))
        app.import_source('Work', text, 'UTC')
        self.assertEqual(self.state('2026-09-28', '2026-09-29', 'UTC')['conflicts'][0]['minutes'], 120)

    def test_duplicate_uid_and_invalid_calendar_rejected(self):
        for text in [calendar(event() + '\n' + event()), calendar(event(extra='DURATION:PT1H')), calendar('BEGIN:VTIMEZONE\nEND:VTIMEZONE'), calendar(event()).replace('VERSION:2.0', '')]:
            with self.assertRaises(ValueError):
                app.import_source('Invalid', text, 'UTC')
        self.assertEqual(self.state()['sources'], [])

    def test_http_host_origin_and_remote_opt_in(self):
        with self.assertRaises(HTTPError) as error:
            self.request('/api/import', {}, {'Origin': 'https://untrusted.example'})
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        with self.assertRaises(HTTPError) as error:
            self.request('/api/state', headers={'Host': 'untrusted.example'})
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        with patch.dict(os.environ, {'ALLOW_REMOTE_FETCH': '0'}):
            with self.assertRaisesRegex(ValueError, 'disabled'):
                app.fetch_ics('https://example.com/calendar.ics')

    def test_unicode_export_line_folding_and_csv_formula_safety(self):
        title = '=SUM(1) ' + 'Ünicode ' * 20
        app.import_source('Text', calendar(event(summary=title) + '\n' + event(uid='two')), 'UTC')
        exported = app.export_ics(self.state())
        self.assertTrue(all(len(line.encode()) <= 75 for line in exported.split('\r\n')))
        self.assertEqual(app.parse_ics(exported)[0]['summary'], title)
        self.assertIn(b"'=SUM(1)", self.request('/api/conflicts.csv?start=2026-09-28&end=2026-10-05'))

    def test_remove_source_via_http(self):
        saved = app.import_source('Work', calendar(event()), 'UTC')
        self.request('/api/remove', {'source_id': saved['source_id']})
        self.assertEqual(self.state()['events'], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
