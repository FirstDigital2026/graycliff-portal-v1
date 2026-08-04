# First Digital Graycliff Portal v1

This is a new, independent Graycliff project—not a modification of the GAC production portal.

## Included now
- New Smartsheet workspace builder with one master Small Projects sheet and supporting billing/payment sheets.
- Manager and technician portal foundation.
- All-open-jobs view, self-assignment, manager assignment, status/work updates.
- Separate field files and billing files. Technicians cannot download billing files.
- Per-job billing ZIP generator.
- Graycliff Large Projects parked page.
- Payment-review foundation for: exact quantity + total, date tie-breaker, then manager review.
- Zoho item setup script using names like `AS24 (Graycliff)` and short descriptions.

## First deployment
1. Deploy this folder as a new Render web service. Do not replace the GAC service.
2. Add a persistent disk at `/var/data`.
3. Set `FLASK_SECRET_KEY` and `ADMIN_PASSWORD`.
4. Run `python scripts/setup_smartsheet_workspace.py` locally or in Render Shell after setting `SMARTSHEET_ACCESS_TOKEN`.
5. Copy the generated sheet IDs into Render environment variables.
6. Run `python scripts/setup_zoho_graycliff_items.py` after setting the Zoho credentials.

## Initial login
- Email: `admin@firstdigitalsc.com`
- Password: whatever is set in `ADMIN_PASSWORD`

## Important
The portal is a working v1 foundation and local database workflow. The next integration pass connects every job create/edit/upload action directly to the new Smartsheet sheet IDs and adds Graycliff payment-attachment parsing. Existing Graycliff sheets remain untouched until migration.
