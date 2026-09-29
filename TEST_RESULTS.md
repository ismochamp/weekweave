# Verification results — WeekWeave

- **Run date:** 28 September 2026.
- **Environment:** macOS; Python 3.14; system IANA timezone database.
- **Command:** `python3 -m unittest -v test_app.py`.
- **Result:** **13 tests passed** in the captured final run.
- **Evidence:** `test-run.txt` contains actual output.
- **Isolation:** temporary SQLite files, real loopback HTTP feeds, no credentials and no remote provider account.

| Test | Behavior verified |
|---|---|
| HTTP feed workflow | Two actual feed downloads; 10 source events → 9 consolidated entries, 1 duplicate merge, 2 overlapping pairs; ICS/CSV exports |
| Transactional recurrence rejection | Unsupported RRULE blocks the entire replacement and preserves earlier data |
| Explicit replacement | Same normalized source name cannot overwrite without the replacement flag |
| DST boundaries | Nonexistent spring time and ambiguous autumn time require UTC input |
| Repeated DST hour | Events sort by actual instant when two local clock times occur with different UTC offsets |
| ALTREP parameter | Unsupported alternate-representation parameters reject the whole replacement and preserve earlier source data |
| All-day and folded text | Exclusive end date preserved; line folding and escaped comma decoded; exported calendar re-imports |
| Non-conflicts | Adjacent, cancelled, transparent and zero-duration events do not create busy overlaps |
| Window clipping | Reported overlap duration is clipped to the selected interval |
| Invalid inputs | Duplicate source UIDs, DURATION, embedded VTIMEZONE and missing VERSION rejected |
| Request boundaries | Invalid Host/Origin blocked and remote fetch requires opt-in |
| Export boundaries | Unicode line folding stays within 75 octets; CSV formula prefixes neutralized |
| Source removal | HTTP request removes only local events belonging to that source |

The actual America/New_York verification event resolves to 10:00 Europe/Berlin on 28 September 2026. Counts describe fixture behavior, not client productivity claims. Browser screenshots are captured during final assembly. Remote provider authentication, all RFC 5545 constructs, public hosting, production load and Windows execution are unverified.
