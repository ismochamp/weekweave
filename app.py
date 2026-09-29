#!/usr/bin/env python3
"""WeekWeave: bounded RFC 5545 subset import, merge, overlap analysis and export."""
import csv
from contextlib import contextmanager
import hashlib
import io
import ipaddress
import json
import os
import re
import sqlite3
from datetime import datetime, date, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from urllib.request import Request, build_opener, HTTPRedirectHandler
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent
PORT = int(os.getenv('PORT', '8112'))
DB = Path(os.getenv('WEEKWEAVE_DB', str(ROOT / 'data' / 'weekweave.sqlite3')))
DEFAULT_TZ = 'Europe/Berlin'
UNSUPPORTED = {'RRULE', 'RDATE', 'EXDATE', 'EXRULE', 'RECURRENCE-ID', 'DURATION'}
TEXT_FIELDS = {'UID', 'SUMMARY', 'DESCRIPTION', 'LOCATION'}
ACCEPTED = TEXT_FIELDS | {'DTSTART', 'DTEND', 'DTSTAMP', 'CREATED', 'LAST-MODIFIED', 'SEQUENCE', 'STATUS', 'TRANSP', 'CLASS', 'URL', 'CATEGORIES', 'PRIORITY', 'ORGANIZER', 'ATTENDEE'}


@contextmanager
def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA busy_timeout=5000')
    try:
        with con:
            yield con
    finally:
        con.close()


def initialize():
    with connect() as con:
        con.executescript('''CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, name TEXT NOT NULL, imported TEXT NOT NULL, origin TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(source_id TEXT NOT NULL, uid TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(source_id,uid));''')


def get_zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('Unknown IANA timezone. Use a name such as Europe/Berlin or UTC.')


def unescape(value):
    if re.search(r'\\(?![nN,;\\])', value):
        raise ValueError('Invalid iCalendar text escape.')
    return re.sub(r'\\([nN,;\\])', lambda m: '\n' if m[1] in ('n', 'N') else m[1], value)


def localize(naive, zone):
    candidates = [naive.replace(tzinfo=zone, fold=f) for f in (0, 1)]
    valid = [x for x in candidates if x.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == naive]
    if not valid:
        raise ValueError('A local event time does not exist during a daylight-saving transition. Use UTC.')
    if len({x.utcoffset() for x in valid}) > 1:
        raise ValueError('An ambiguous local event time occurs during a daylight-saving transition. Use UTC.')
    return valid[0]


def parse_time(prop, default_zone):
    params, value = prop
    if set(params) - {'TZID', 'VALUE'}:
        raise ValueError('Unsupported date/time parameter.')
    kind = params.get('VALUE', 'DATE-TIME')
    if kind == 'DATE':
        if 'TZID' in params or not re.fullmatch(r'\d{8}', value):
            raise ValueError('DATE values must be YYYYMMDD without TZID.')
        return datetime.strptime(value, '%Y%m%d').date().isoformat(), True
    if kind != 'DATE-TIME' or not re.fullmatch(r'\d{8}T\d{6}Z?', value):
        raise ValueError('Use RFC 5545 DATE or DATE-TIME values with seconds.')
    if value.endswith('Z'):
        if 'TZID' in params:
            raise ValueError('UTC timestamps must not include TZID.')
        parsed = datetime.strptime(value, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
    else:
        zone = get_zone(params.get('TZID', default_zone))
        parsed = localize(datetime.strptime(value, '%Y%m%dT%H%M%S'), zone)
    return parsed.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'), False


def parse_ics(text, default_zone=DEFAULT_TZ):
    get_zone(default_zone)
    if not isinstance(text, str) or len(text.encode('utf-8')) > 2_000_000:
        raise ValueError('ICS input must be UTF-8 text within 2 MB.')
    if '\x00' in text:
        raise ValueError('NUL characters are not supported.')
    lines = text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n').split('\n')
    unfolded = []
    for line in lines:
        if line.startswith((' ', '\t')):
            if not unfolded:
                raise ValueError('Invalid first-line folding.')
            unfolded[-1] += line[1:]
        elif line:
            unfolded.append(line)
    if not unfolded or unfolded[0] != 'BEGIN:VCALENDAR' or unfolded[-1] != 'END:VCALENDAR':
        raise ValueError('Input must contain exactly one complete VCALENDAR.')
    events, current, version = [], None, False
    for line in unfolded[1:-1]:
        if len(line) > 100000:
            raise ValueError('An unfolded line exceeds 100,000 characters.')
        if line == 'BEGIN:VEVENT':
            if current is not None:
                raise ValueError('Nested events are not supported.')
            current = {}
            continue
        if line == 'END:VEVENT':
            if current is None:
                raise ValueError('Unexpected END:VEVENT.')
            if 'UID' not in current or 'DTSTART' not in current:
                raise ValueError('Every event requires a UID and DTSTART.')
            start, all_day = parse_time(current['DTSTART'], default_zone)
            if 'DTEND' in current:
                end, end_all_day = parse_time(current['DTEND'], default_zone)
                if end_all_day != all_day:
                    raise ValueError('DTSTART and DTEND must have matching value types.')
            else:
                end = (date.fromisoformat(start) + timedelta(days=1)).isoformat() if all_day else start
            if end < start or (all_day and end == start):
                raise ValueError('DTEND must follow DTSTART; timed events may have zero duration.')
            def txt(key, fallback=''):
                return unescape(current.get(key, ({}, fallback))[1])
            uid = txt('UID')
            if not uid.strip() or len(uid) > 500:
                raise ValueError('UID must contain 1–500 characters.')
            status = current.get('STATUS', ({}, 'CONFIRMED'))[1]
            transparency = current.get('TRANSP', ({}, 'OPAQUE'))[1]
            if status not in ('TENTATIVE', 'CONFIRMED', 'CANCELLED') or transparency not in ('OPAQUE', 'TRANSPARENT'):
                raise ValueError('Unsupported STATUS or TRANSP value.')
            event = {'uid': uid, 'summary': txt('SUMMARY', '(Untitled event)'), 'description': txt('DESCRIPTION'), 'location': txt('LOCATION'), 'start': start, 'end': end, 'all_day': all_day, 'status': status, 'transparent': transparency == 'TRANSPARENT'}
            if any(e['uid'] == uid for e in events):
                raise ValueError(f'Duplicate UID within the source: {uid}. No source changes were saved.')
            events.append(event)
            if len(events) > 5000:
                raise ValueError('An import is limited to 5,000 events.')
            current = None
            continue
        if line.startswith(('BEGIN:', 'END:')):
            raise ValueError('Only VCALENDAR and VEVENT components are supported. Expand recurrences and remove VTIMEZONE/VALARM components before importing.')
        if ':' not in line:
            raise ValueError('Malformed iCalendar property.')
        head, value = line.split(':', 1)
        parts = head.split(';')
        key = parts[0].upper()
        if current is None:
            if key == 'VERSION':
                if value != '2.0' or version:
                    raise ValueError('Exactly one VERSION:2.0 is required.')
                version = True
            elif key not in {'PRODID', 'CALSCALE', 'METHOD', 'NAME', 'DESCRIPTION', 'X-WR-CALNAME', 'X-WR-TIMEZONE'} and not key.startswith('X-'):
                raise ValueError(f'Unsupported calendar property: {key}.')
            elif key == 'METHOD' and value != 'PUBLISH':
                raise ValueError('Scheduling messages are not supported; use a METHOD:PUBLISH or method-free calendar.')
            elif key == 'CALSCALE' and value != 'GREGORIAN':
                raise ValueError('Only the Gregorian calendar is supported.')
            continue
        if key in UNSUPPORTED:
            raise ValueError(f'{key} is not supported. Expand recurring events to individual VEVENTs and use explicit DTEND values before importing. No partial import was saved.')
        if key not in ACCEPTED and not key.startswith('X-'):
            raise ValueError(f'Unsupported event property: {key}.')
        params = {}
        for part in parts[1:]:
            if '=' not in part:
                raise ValueError('Malformed property parameter.')
            pname, pvalue = part.split('=', 1)
            if pname.upper() in params:
                raise ValueError('Duplicate property parameter.')
            params[pname.upper()] = pvalue.strip('"')
        if key in TEXT_FIELDS and params:
            if set(params) - {'LANGUAGE'}:
                raise ValueError(f'Unsupported {key} parameter.')
        if key in {'UID', 'SUMMARY', 'DESCRIPTION', 'LOCATION', 'DTSTART', 'DTEND', 'STATUS', 'TRANSP'}:
            if key in current:
                raise ValueError(f'Duplicate event property: {key}.')
            current[key] = (params, value)
    if current is not None or not version:
        raise ValueError('Calendar is incomplete or VERSION:2.0 is missing.')
    return events


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Redirects are disabled. Enter the final feed URL.')


def fetch_ics(url):
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError('Use an HTTP(S) feed URL without embedded credentials or fragments.')
    try:
        local = ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        local = parts.hostname == 'localhost'
    if not local and os.getenv('ALLOW_REMOTE_FETCH') != '1':
        raise ValueError('Remote feed fetching is disabled. Set ALLOW_REMOTE_FETCH=1 to enable it explicitly.')
    if not local and parts.scheme != 'https' and os.getenv('ALLOW_INSECURE_HTTP') != '1':
        raise ValueError('Remote feeds require HTTPS unless ALLOW_INSECURE_HTTP=1 is set.')
    with build_opener(NoRedirect()).open(Request(url, headers={'Accept': 'text/calendar', 'User-Agent': 'WeekWeave/1.0'}), timeout=8) as response:
        body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise ValueError('Feed exceeds 2 MB.')
        return body.decode('utf-8-sig')


def import_source(name, text, zone, origin='Uploaded / pasted ICS', replace=False):
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValueError('Use a source name containing 1–100 characters.')
    name = name.strip()
    events = parse_ics(text, zone)
    ident = hashlib.sha256(name.casefold().encode()).hexdigest()[:16]
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        if con.execute('SELECT 1 FROM sources WHERE id=?', (ident,)).fetchone() and replace is not True:
            raise ValueError('A source with this name exists. Explicitly enable replacement or choose a new name.')
        remaining = con.execute('SELECT COUNT(*) FROM events WHERE source_id != ?', (ident,)).fetchone()[0]
        if remaining + len(events) > 10000:
            raise ValueError('This local workspace supports at most 10,000 stored events.')
        con.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?)', (ident, name, datetime.now(timezone.utc).isoformat(timespec='seconds'), origin))
        con.execute('DELETE FROM events WHERE source_id=?', (ident,))
        con.executemany('INSERT INTO events VALUES(?,?,?)', [(ident, e['uid'], json.dumps(e)) for e in events])
    return {'source_id': ident, 'imported': len(events)}


def event_times(event, zone):
    if event['all_day']:
        return tuple(localize(datetime.combine(date.fromisoformat(event[k]), datetime.min.time()), zone) for k in ('start', 'end'))
    return tuple(datetime.fromisoformat(event[k].replace('Z', '+00:00')).astimezone(zone) for k in ('start', 'end'))


def get_state(zone_name=DEFAULT_TZ, start=None, end=None):
    zone = get_zone(zone_name)
    today = datetime.now(zone).date()
    first = date.fromisoformat(start) if start else today - timedelta(days=today.weekday())
    last = date.fromisoformat(end) if end else first + timedelta(days=7)
    if last <= first or (last - first).days > 93:
        raise ValueError('Choose an end date after the start date, within 93 days. End is exclusive.')
    window_start, window_end = localize(datetime.combine(first, datetime.min.time()), zone), localize(datetime.combine(last, datetime.min.time()), zone)
    with connect() as con:
        sources = [dict(r) for r in con.execute('SELECT s.*, COUNT(e.uid) AS count FROM sources s LEFT JOIN events e ON s.id=e.source_id GROUP BY s.id ORDER BY s.name')]
        raw = [dict(r) for r in con.execute('SELECT e.*,s.name FROM events e JOIN sources s ON s.id=e.source_id')]
    merged = {}
    for row in raw:
        event = json.loads(row['payload'])
        # Only exactly equivalent events (including UID and content) merge across sources.
        signature = json.dumps(event, sort_keys=True)
        if signature in merged:
            merged[signature]['sources'].append(row['name'])
            continue
        start_dt, end_dt = event_times(event, zone)
        if start_dt >= window_end or (end_dt <= window_start and start_dt < window_start):
            continue
        event.update({'display_start': start_dt.isoformat(), 'display_end': end_dt.isoformat(), 'sources': [row['name']], 'id': hashlib.sha256(signature.encode()).hexdigest()[:24]})
        merged[signature] = event
    # A repeated daylight-saving hour can have the same wall time at two UTC offsets.
    # Sort absolute instants, not ISO strings, so the visible agenda stays chronological.
    events = sorted(merged.values(), key=lambda e: (datetime.fromisoformat(e['display_start']).timestamp(), e['summary']))
    if len(events) > 1000:
        raise ValueError('More than 1,000 events fall in this window. Narrow the date range before analysis/export.')
    conflicts = []
    active = []
    for event in sorted(events, key=lambda e: datetime.fromisoformat(e['display_start']).timestamp()):
        if event['transparent'] or event['status'] == 'CANCELLED':
            continue
        a, b = event_times(event, zone)
        au, bu = a.astimezone(timezone.utc), b.astimezone(timezone.utc)
        if bu <= au:
            continue
        active = [(other, begin, finish) for other, begin, finish in active if finish > au]
        for other, begin, finish in active:
            overlap_start = max(begin, au, window_start.astimezone(timezone.utc))
            overlap_end = min(finish, bu, window_end.astimezone(timezone.utc))
            minutes = max(0, int((overlap_end - overlap_start).total_seconds() / 60))
            if overlap_end > overlap_start:
                conflicts.append({'a': other['id'], 'b': event['id'], 'first': other['summary'], 'second': event['summary'], 'start': overlap_start.astimezone(zone).isoformat(), 'end': overlap_end.astimezone(zone).isoformat(), 'minutes': minutes, 'sources': ' / '.join(sorted(set(other['sources'] + event['sources'])))})
                if len(conflicts) > 5000:
                    raise ValueError('More than 5,000 overlapping pairs fall in this window. Narrow the date range.')
        active.append((event, au, bu))
    return {'sources': sources, 'events': events, 'conflicts': conflicts, 'timezone': zone_name, 'start': first.isoformat(), 'end': last.isoformat(), 'remote_enabled': os.getenv('ALLOW_REMOTE_FETCH') == '1', 'duplicates_merged': sum(len(e['sources']) - 1 for e in events)}


def escape(value):
    return value.replace('\\', '\\\\').replace('\n', '\\n').replace(';', '\\;').replace(',', '\\,').replace('\r', '')


def fold(line):
    # Fold at 75 UTF-8 octets without splitting code points (continuation space included).
    parts, current = [], ''
    for char in line:
        if len((current + char).encode('utf-8')) > 75:
            parts.append(current)
            current = ' '
        current += char
    parts.append(current)
    return '\r\n'.join(parts)


def export_ics(state):
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//WeekWeave//Calendar Integration//EN', 'CALSCALE:GREGORIAN', 'X-WR-CALNAME:WeekWeave consolidated calendar']
    now = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    for event in state['events']:
        lines += ['BEGIN:VEVENT', f"UID:{event['id']}@weekweave.local", f'DTSTAMP:{now}']
        if event['all_day']:
            lines += ['DTSTART;VALUE=DATE:' + event['start'].replace('-', ''), 'DTEND;VALUE=DATE:' + event['end'].replace('-', '')]
        else:
            lines += [key + ':' + datetime.fromisoformat(event[field].replace('Z', '+00:00')).astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ') for key, field in [('DTSTART', 'start'), ('DTEND', 'end')]]
        lines += ['SUMMARY:' + escape(event['summary']), 'DESCRIPTION:' + escape(event['description']), 'LOCATION:' + escape(event['location']), 'STATUS:' + event['status'], 'TRANSP:' + ('TRANSPARENT' if event['transparent'] else 'OPAQUE'), 'X-WEEKWEAVE-SOURCES:' + escape(' / '.join(event['sources'])), 'X-ORIGINAL-UID:' + escape(event['uid']), 'END:VEVENT']
    lines.append('END:VCALENDAR')
    return '\r\n'.join(fold(line) for line in lines) + '\r\n'


class Handler(BaseHTTPRequestHandler):
    def valid_request(self):
        allowed = {f'localhost:{PORT}', f'127.0.0.1:{PORT}'}
        if self.headers.get('Host') not in allowed or (self.headers.get('Origin') and self.headers.get('Origin') not in {f'http://{v}' for v in allowed}):
            self.reply(403, {'error': 'Host or Origin rejected.'})
            return False
        return True

    def reply(self, status, payload, kind='application/json', filename=None):
        raw = (json.dumps(payload) if kind == 'application/json' else payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', kind + '; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self.valid_request():
            return
        path = urlsplit(self.path).path
        query = parse_qs(urlsplit(self.path).query)
        try:
            if path == '/':
                self.reply(200, (ROOT / 'index.html').read_text(), 'text/html')
            elif path.startswith('/fixtures/') and path in ('/fixtures/studio.ics', '/fixtures/operations.ics'):
                self.reply(200, (ROOT / path.lstrip('/')).read_text(), 'text/calendar')
            elif path in ('/api/state', '/api/export.ics', '/api/conflicts.csv'):
                state = get_state(query.get('timezone', [DEFAULT_TZ])[0], query.get('start', [None])[0], query.get('end', [None])[0])
                if path == '/api/state':
                    self.reply(200, state)
                elif path == '/api/export.ics':
                    self.reply(200, export_ics(state), 'text/calendar', 'weekweave-calendar.ics')
                else:
                    out = io.StringIO()
                    fields = ['first', 'second', 'start', 'end', 'minutes', 'sources']
                    writer = csv.DictWriter(out, fieldnames=fields)
                    writer.writeheader()
                    for item in state['conflicts']:
                        writer.writerow({k: "'" + str(item[k]) if str(item[k]).startswith(('=', '+', '-', '@', '\t', '\r')) else item[k] for k in fields})
                    self.reply(200, out.getvalue(), 'text/csv', 'weekweave-conflicts.csv')
            else:
                self.reply(404, {'error': 'Not found.'})
        except (ValueError, OSError) as error:
            self.reply(400, {'error': str(error)})

    def do_POST(self):
        if not self.valid_request():
            return
        try:
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError('JSON requests are required.')
            length = int(self.headers.get('Content-Length', 0))
            if length < 1 or length > 3_000_000:
                raise ValueError('Request is empty or exceeds 3 MB.')
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError('Request body must be an object.')
            if self.path == '/api/import':
                url = body.get('url', '').strip()
                text = fetch_ics(url) if url else body.get('text', '')
                result = import_source(body.get('name', ''), text, body.get('timezone', DEFAULT_TZ), url or 'Uploaded / pasted ICS', body.get('replace', False))
            elif self.path == '/api/remove':
                with connect() as con:
                    con.execute('DELETE FROM events WHERE source_id=?', (body.get('source_id'),))
                    changed = con.execute('DELETE FROM sources WHERE id=?', (body.get('source_id'),)).rowcount
                result = {'removed': bool(changed)}
            else:
                self.reply(404, {'error': 'Not found.'})
                return
            self.reply(200, result)
        except (ValueError, OSError, TypeError, KeyError) as error:
            self.reply(400, {'error': str(error)})

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)


if __name__ == '__main__':
    initialize()
    print(f'WeekWeave · http://127.0.0.1:{PORT} · local calendar workspace', flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
