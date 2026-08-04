# First Digital Graycliff Portal

This build uses Smartsheet as the source of truth for Graycliff small projects.

Required Render environment variables:

- `SMARTSHEET_ACCESS_TOKEN`
- `ADMIN_PASSWORD`
- `FLASK_SECRET_KEY`
- `DATA_PATH=/var/data/graycliff.db`
- `FILE_PATH=/var/data/files`
- `SMARTSHEET_CONFIG_PATH=/var/data/graycliff_smartsheet_ids.json`

On the first signed-in dashboard request, the portal creates or reuses the `Graycliff Portal` workspace and these sheets:

- Graycliff Small Projects - Master
- Graycliff Billing Batches
- Graycliff Payments
- Graycliff Payment Matches
- Graycliff Users
- Graycliff Configuration

The operation is idempotent: redeploying does not create duplicate sheets when the workspace already exists.
