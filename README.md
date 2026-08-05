# Graycliff Cloud Portal v1

This is a clean rebuild designed around the final architecture:

- **Smartsheet:** field operations for technicians
- **First Digital portal:** office review and billing
- **Graycliff portal:** read-only customer project/invoice view
- **Cloud sync:** active technician directory -> Assigned Technician contact options
- **No technician traffic through the website**
- **No PC-based process**

## Smartsheet IDs already configured

- Workspace: `3074739741714308`
- Graycliff Field Work Orders: `1440710464065412`
- Graycliff Technician Directory: `7015354675974020`
- Graycliff Billing: `7158170928500612`
- Graycliff Payments: `6526836505792388`
- Graycliff Payment Matches: `435877095362436`

## Deploy to Render

1. Create a new empty GitHub repository.
2. Upload the **contents of this folder** to the repository root.
3. In Render, choose **New > Blueprint** and connect the repository.
4. Set these secret environment variables:
   - `ADMIN_PASSWORD`
   - `SMARTSHEET_ACCESS_TOKEN`
5. Deploy.
6. Log in using `ADMIN_EMAIL` and `ADMIN_PASSWORD`.
7. Open **Users** and create:
   - First Digital office users
   - Graycliff customer users
8. Press **Sync Now** once.

## Automatic sync

The Render service runs two cloud jobs every 15 minutes:

1. Synchronize active Technician Directory contacts into the Assigned Technician contact column.
2. Create missing billing-queue rows for field-complete/approved work orders.

Set `TECH_SYNC_MINUTES` to a different value if needed. Five minutes is the minimum recommended value.

## What works in v1

- Cloud-only technician contact sync
- Cached Smartsheet reads
- First Digital office dashboard
- Work-order review
- Missing-documents / office-approval workflow
- Automatic billing-queue creation
- Billing record editing
- Smartsheet row attachment downloads
- Graycliff read-only job view
- Graycliff invoice/payment summary
- Portal user management
- Persistent SQLite database on Render disk
- First Digital branding

## Deliberately not included yet

These require the exact production rules and credentials before they should be automated:

- Zoho invoice creation
- Rate-card line-item calculations
- Work Completed Spreadsheet generation
- Billing-package ZIP generation
- Remittance PDF/email parsing
- Daily Number payment matching
- Automatic attachment classification into Field File vs Required Photos

The structure is ready for those integrations without changing the technician workflow.


## Smartsheet technician views

After deployment, log in as the administrator and press:

`Build Florence & Columbia Views`

This creates two reports in the Graycliff Portal workspace:

- Florence Field Work
- Columbia Field Work

They are sourced from the single Graycliff Field Work Orders master sheet. Technicians can use the reports from Smartsheet mobile after the reports and source sheet are shared with the appropriate people.


## Mobile technician sheets

Use the dashboard button:

`Build & Sync Mobile Field Sheets`

This creates:

- Florence Technician Jobs
- Columbia Technician Jobs

Each is a real Smartsheet sheet, so technicians can use the standard Smartsheet Mobile View.

Cloud sync behavior:

- Manager-controlled job details flow from the master sheet to the market sheet.
- Technician status, dates, work performed, and completion checkboxes flow back to the master.
- Row attachments are copied both directions and de-duplicated by filename.
- Closed or archived jobs are removed from technician sheets.
- Jobs moved between markets are moved to the correct market sheet.
- Sync runs on the same cloud interval as the technician-directory sync.


## Mobile card layout

The sync now adds and maintains three combined display fields:

- Job Summary: Job Type, Task Name, and CRQ when present
- Location: Address and City
- Due / Priority: Due Date and Priority

Recommended Mobile View fields:

1. Job Summary
2. Location
3. Due / Priority
4. Assigned Technician
5. Status
6. Work Performed
7. Field File Complete
8. Required Photos Complete
9. Date Field Completed

Project ID remains the card title. Comments and attachments remain available from the card icons.


## Graycliff dedicated mailbox import

The portal supports app-only Microsoft Graph access to:

`graycliffjobs@firstdigitalsc.com`

Required Render secrets:

- `MS_TENANT_ID`
- `MS_CLIENT_ID`
- `MS_CLIENT_SECRET`
- `GRAYCLIFF_JOBS_MAILBOX=graycliffjobs@firstdigitalsc.com`

Import rules:

- Only subjects matching `PO Number for NTP <work order> PRISM <number> Created/Revised` are auto-created.
- Work Order Number becomes Project ID.
- PO number and all dollar amounts are ignored.
- Revisions update the existing Work Order Number.
- Unrecognized email is recorded as ignored and remains available for manual entry.
- Original attachments and an EML copy are attached to the Smartsheet row.
- If market cannot be inferred, the job is created but will not enter a technician sheet until office staff selects Florence or Columbia.

## Automatic field dates

- Status `In Progress` stamps Date Started once.
- Status `Field Complete` stamps Date Field Completed once.
- Field Complete also stamps Date Started when it was missing.
- Technicians do not manually enter either date.

## Security cleanup

- The emergency `/repair-admin` route has been removed.
- Mobile-sheet build actions are admin-only.
- Sync Now now includes technician contacts, mobile sheets, billing, and mailbox import.


## Correct work-order package parsing

The importer now opens attached ZIP packages and reads the formal WO PDF for the
work-order number, PRISM ID, job address, city, and estimated completion date.
It does not import PO numbers, PO amounts, rates, quantities, or any dollar values.
Forwarded subject text is no longer used as Task Name. Revisions preserve status
and assignment.
