# Billing update ready for release

- Explicit “fiber workbook not required” excludes a stale generated workbook from selections, preview and outgoing email. The work completed spreadsheet and completed asbuilt remain required.
- Forwarded First Digital payroll/billing recap PDFs to graycliffjobs@firstdigitalsc.com are intercepted before job creation. Original email/attachments are retained. Printed totals must reconcile exactly; partial parses are held rather than guessed.
- Tracking begins October 27, 2026 Eastern. Pre-start billing does not enter missing-recap checks. Old recap weeks are retained without alerts. Personal inbox monitoring is not enabled.
- A separate Graycliff Billing Recap Reconciliation Smartsheet records our billed dollars, recap dollars, difference and code/quantity changes. Our sent work, revenue and payment status are preserved.
- Your existing owner-only financial dashboard gains Billing recaps. Other admins retain no access. No billing or customer emails are sent by this release.
- Duplicate file hashes are counted once; conflicting versions and ambiguous matches are shown for review.

Validation: 55 targeted Python tests pass, including real attachment preview and MIME construction with mocked email transport; current financial analysis and complete portal JavaScript checks pass. Existing older test_billing_mail tests use a retired spreadsheet fixture and fail before attachment collection finishes; the new test_no_workbook uses the current compact template and verifies both opt-out and normal required-workbook behavior.

Release: push the prepared backend changes to production main (Render automatic deploy), then save the assembled WordPress snippet 2887. Reopen billing review afterward; existing prepared emails should be reviewed again. No real email is sent during verification.
