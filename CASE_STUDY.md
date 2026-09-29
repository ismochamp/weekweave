# WeekWeave
## Calendar feeds, aligned timezones and visible scheduling conflicts

**Category:** API & System Integration  
**Author:** Ismail Habib  
**Project type:** New independent working project  
**Stack:** Python, ICS/iCalendar, HTTP, IANA timezones, SQLite, HTML, CSS, JavaScript

### The problem

Separate calendars make a schedule difficult to assess. An event imported from another timezone can appear to collide with the wrong meeting; the same invitation can occur in more than one feed; and a consolidated view is only useful if it preserves the source information behind each event.

### The delivered solution

WeekWeave accepts ICS files, pasted calendar text and configured HTTP feeds. It validates each source before saving, normalizes timed entries to UTC, then presents events in a chosen timezone. Exact duplicates from multiple sources merge while retaining their source names. An overlap review identifies busy-event pairs and the shared time interval.

Users can filter a date window, inspect an event, remove or explicitly replace a local source, and download a consolidated ICS calendar or CSV conflict report. The application never writes back to the source feed.

### Integration decisions

Calendar formats have significant edge cases. This implementation makes its supported subset explicit. Recurring events, embedded timezone definitions and alarm components are rejected with an explanation; they are not discarded silently. IANA timezones and UTC are supported, and ambiguous or nonexistent local times at daylight-saving transitions require UTC input.

The application treats adjacent events as non-conflicting, excludes cancelled/free events from busy-time checks, preserves exclusive all-day end dates, and protects earlier source data when replacement input is invalid. Exports include stable generated UIDs and original UID/source annotations.

### What was verified

Thirteen tests passed locally, including actual HTTP imports from two independent fixture feeds. Ten source events become nine consolidated events, with one exact duplicate merge and two overlapping pairs. An America/New_York event is correctly displayed in Europe/Berlin. Tests also cover exported ICS re-import, recurrence rejection without partial replacement, explicit source replacement, DST gaps and ambiguity, folded text, all-day dates, overlap-window clipping, source removal, unsafe CSV prefixes, Unicode folding, Host/Origin checks and remote-fetch opt-in.

### Why the implementation matters

This project demonstrates integration of a different external data format from JSON APIs, including timezone normalization, duplicate semantics, validation, persistence and usable exports. It turns incoming calendar data into an inspectable workflow rather than assuming that a successful download means the integration is correct.

### Honest scope

This is an independently built local application, not a claim of a past client engagement or public SaaS deployment. Verification events are fictional and explicitly labeled. The app handles a documented subset of iCalendar; OAuth calendars, recurrence expansion, source writeback and automatic scheduled refresh would require additional implementation.

### Screenshot captions

1. **Separate feeds, one view:** Two local HTTP verification feeds are imported and normalized into a week view with source labels and visible overlaps.
2. **Inspect and export:** Overlapping busy events show their shared intervals; event details preserve source context, and the selected window can be exported as ICS or CSV.
