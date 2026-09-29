# Portfolio title
WeekWeave — Calendar Feed Integration & Timezone-Aware Conflict Review

# Short description
A working ICS integration app that combines calendar feeds, aligns timezones, merges exact duplicates and exports consolidated schedules with an overlap report.

# Full description
When schedules are spread across calendars, a shared view only helps if event times, duplicates and conflicts are handled correctly.

I built WeekWeave to connect those pieces. Users can upload ICS files, paste calendar data or import a configured HTTP feed. The application validates the source, stores its events locally, and displays the combined schedule in the selected timezone. It identifies overlapping busy events, preserves source context and exports the current window as an ICS calendar or CSV conflict report.

The integration includes explicit replacement controls, transactional imports, daylight-saving validation and clear errors for unsupported recurrence. Invalid data leaves the existing source intact.

This is a new independent working project. Thirteen automated tests verified real local HTTP feed imports, timezone conversion, exact duplicate merging, conflict detection and export behavior. The screenshots use fictional, labeled verification calendars; they do not imply a past client engagement or public deployment.

Relevant skills: API & system integration · Python · ICS/iCalendar · Timezone normalization · SQLite · Data validation · Web interfaces.

# Suggested image order
1. Consolidated calendar showing source attribution.
2. Conflict review and inspected event details.
