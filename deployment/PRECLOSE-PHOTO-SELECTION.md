Completion now separates saved job photographs from email attachments.

- Pre-close uploads each original photo separately to the existing job, with a stable upload receipt and visible progress. A partial failure resumes the same batch without repeating completed uploads. The gallery has no 20-photo limit; the existing 15 MB per-file storage limit remains.
- Email photos start unchecked. Select up to ten saved field-safe pictures or explicitly choose an email without photographs. Review the selected names and thumbnails before the final completion action. Uploading alone does not complete work or send mail.
- The server validates job access, each selected attachment's sheet/job relationship, field release, current filename, and duplicate references. The committed completion activity stores a snapshot of the reviewed selection. Email retries use that exact snapshot and cannot include later uploads.
- Existing activity events without a selection retain legacy handling. The ZIP and verified large-attachment transport remain available for oversized selected packages.

Release: deploy the backend first. Apply the event-field patch to WordPress snippet 2881, then append the PHP addon to snippet 2887 after replacing its PRECLOSE_SCRIPT placeholder with the JS file. The addon replaces the original completion-photo submit handler and registers the selected-photo package endpoint after the existing endpoint.

Checks: Python completion/ZIP/threading tests, PHP selection/download fixtures for a 60-photo job, JavaScript syntax, and assembled PHP syntax. Live verification must open a real job without submitting a completion or sending a test customer email.
