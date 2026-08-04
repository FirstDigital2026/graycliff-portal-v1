from __future__ import annotations

import hmac
import os
import secrets
import sqlite3
import threading
from datetime import datetime
from functools import wraps
from io import BytesIO
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from smartsheet_api import SmartsheetClient, SmartsheetError, rows_as_records

APP_NAME = "Graycliff Project Portal"

WORKSPACE_ID = 3074739741714308
FIELD_SHEET_ID = 1440710464065412
DIRECTORY_SHEET_ID = 7015354675974020
BILLING_SHEET_ID = 7158170928500612
PAYMENTS_SHEET_ID = 6526836505792388
PAYMENT_MATCHES_SHEET_ID = 435877095362436

ASSIGNED_TECH_COLUMN_ID = 7656679053496196

FLORENCE_MOBILE_SHEET = "Florence Technician Jobs"
COLUMBIA_MOBILE_SHEET = "Columbia Technician Jobs"

MOBILE_COLUMNS = [
    ("Project ID", "TEXT_NUMBER", True),
    ("Priority", "PICKLIST", False),
    ("Task Name", "TEXT_NUMBER", False),
    ("Address", "TEXT_NUMBER", False),
    ("City", "TEXT_NUMBER", False),
    ("Job Type", "PICKLIST", False),
    ("CRQ Number", "TEXT_NUMBER", False),
    ("Due Date", "DATE", False),
    ("Assigned Technician", "CONTACT_LIST", False),
    ("Status", "PICKLIST", False),
    ("Date Started", "DATE", False),
    ("Date Field Completed", "DATE", False),
    ("Work Performed", "TEXT_NUMBER", False),
    ("Field File Complete", "CHECKBOX", False),
    ("Required Photos Complete", "CHECKBOX", False),
    ("Manager Notes", "TEXT_NUMBER", False),
    ("Customer Notes", "TEXT_NUMBER", False),
    ("Master Row ID", "TEXT_NUMBER", False),
]

MANAGER_TO_MOBILE_FIELDS = [
    "Priority", "Task Name", "Address", "City", "Job Type", "CRQ Number",
    "Due Date", "Assigned Technician", "Manager Notes", "Customer Notes",
]

TECH_TO_MASTER_FIELDS = [
    "Status", "Date Started", "Date Field Completed", "Work Performed",
    "Field File Complete", "Required Photos Complete",
]

DATA_PATH = Path(os.getenv("DATA_PATH", "/tmp/graycliff.db"))
DATA_PATH.parent.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

store = SmartsheetClient(ttl=int(os.getenv("SMARTSHEET_CACHE_SECONDS", "60")))
sync_lock = threading.Lock()
scheduler: BackgroundScheduler | None = None


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DATA_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin','office','graycliff')),
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS portal_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT NOT NULL,
                action TEXT NOT NULL,
                object_type TEXT,
                object_id TEXT,
                details TEXT,
                created_at TEXT NOT NULL
            );
            """
        )

        # Migrate an older persistent users table from the previous portal build.
        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        migrations = {
            "display_name": "TEXT NOT NULL DEFAULT ''",
            "password_hash": "TEXT NOT NULL DEFAULT ''",
            "role": "TEXT NOT NULL DEFAULT 'graycliff'",
            "active": "INTEGER NOT NULL DEFAULT 1",
            "created_at": "TEXT NOT NULL DEFAULT ''",
        }
        for column_name, definition in migrations.items():
            if column_name not in existing_columns:
                connection.execute(
                    f"ALTER TABLE users ADD COLUMN {column_name} {definition}"
                )

        admin_email = os.getenv("ADMIN_EMAIL", "thomas@firstdigitalsc.com").strip().lower()
        admin_password = os.getenv("ADMIN_PASSWORD", "")
        existing = connection.execute(
            "SELECT id FROM users WHERE email=?", (admin_email,)
        ).fetchone()
        if not existing and admin_password:
            connection.execute(
                """
                INSERT INTO users(email,display_name,password_hash,role,active,created_at)
                VALUES(?,?,?,?,1,?)
                """,
                (
                    admin_email,
                    "Thomas Bramlette II",
                    generate_password_hash(admin_password),
                    "admin",
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
        elif existing:
            # Always promote the configured ADMIN_EMAIL to administrator.
            # Preserve the existing password unless ADMIN_PASSWORD is supplied.
            if admin_password:
                connection.execute(
                    """
                    UPDATE users
                    SET display_name=?, password_hash=?, role='admin', active=1,
                        created_at=CASE WHEN created_at='' THEN ? ELSE created_at END
                    WHERE email=?
                    """,
                    (
                        "Thomas Bramlette II",
                        generate_password_hash(admin_password),
                        datetime.now().isoformat(timespec="seconds"),
                        admin_email,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE users
                    SET display_name=?, role='admin', active=1,
                        created_at=CASE WHEN created_at='' THEN ? ELSE created_at END
                    WHERE email=?
                    """,
                    (
                        "Thomas Bramlette II",
                        datetime.now().isoformat(timespec="seconds"),
                        admin_email,
                    ),
                )


def log_action(action: str, object_type: str = "", object_id: str = "", details: str = "") -> None:
    with db() as connection:
        connection.execute(
            """
            INSERT INTO audit_log(user_email,action,object_type,object_id,details,created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (
                session.get("user", "system"),
                action,
                object_type,
                object_id,
                details,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def refresh_session_user() -> bool:
    """Reload the current user's role from the database on every protected request."""
    email = session.get("user")
    if not email:
        return False

    with db() as connection:
        user = connection.execute(
            "SELECT email,display_name,role,active FROM users WHERE email=?",
            (email,),
        ).fetchone()

    if not user or not user["active"]:
        session.clear()
        return False

    session["display_name"] = user["display_name"]
    session["role"] = user["role"]
    return True


def require_login(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not refresh_session_user():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def roles(*allowed: str):
    def decorator(view):
        @wraps(view)
        @require_login
        def wrapped(*args, **kwargs):
            if session.get("role") not in allowed:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


@app.context_processor
def inject_globals() -> dict[str, Any]:
    return {
        "app_name": APP_NAME,
        "role": session.get("role"),
        "current_user": session.get("user"),
    }


def record_map(sheet_id: int, *, force: bool = False) -> list[dict[str, Any]]:
    return rows_as_records(store.get_sheet(sheet_id, force=force))


def by_project(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("Project ID", "")).strip(): row
        for row in records
        if str(row.get("Project ID", "")).strip()
    }


def active_technicians() -> list[dict[str, str]]:
    records = record_map(DIRECTORY_SHEET_ID)
    technicians = []
    seen = set()
    for row in records:
        active = row.get("Active")
        enabled = active is True or str(active).strip().lower() in {"true", "1", "yes", "checked"}
        email = str(row.get("Technician Email", "")).strip().lower()
        if not enabled or not email or "@" not in email or email in seen:
            continue
        seen.add(email)
        technicians.append(
            {
                "email": email,
                "name": str(row.get("Technician Name", "")).strip() or email,
                "market": str(row.get("Market", "")).strip(),
            }
        )
    return sorted(technicians, key=lambda t: (t["name"].lower(), t["email"]))


def sync_technician_contacts() -> dict[str, Any]:
    if not sync_lock.acquire(blocking=False):
        return {"ok": True, "message": "A technician sync is already running."}
    try:
        technicians = active_technicians()
        if not technicians:
            return {"ok": False, "message": "No active technicians with valid emails were found."}

        current = store.get_column(FIELD_SHEET_ID, ASSIGNED_TECH_COLUMN_ID)
        body = {
            "title": "Assigned Technician",
            "index": current.get("index", 9),
            "type": "CONTACT_LIST",
            "contactOptions": [
                {"name": tech["name"], "email": tech["email"]}
                for tech in technicians
            ],
            "validation": True,
        }
        store.update_column(FIELD_SHEET_ID, ASSIGNED_TECH_COLUMN_ID, body)
        return {
            "ok": True,
            "message": f"Synced {len(technicians)} active technician(s).",
            "count": len(technicians),
        }
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
    finally:
        sync_lock.release()




def find_sheet_by_name(name: str) -> dict[str, Any] | None:
    for sheet in store.list_sheets():
        if sheet.get("name") == name:
            return sheet
    return None


def mobile_sheet_definition(name: str) -> dict[str, Any]:
    source = store.get_sheet(FIELD_SHEET_ID, force=True)
    source_columns = {c["title"]: c for c in source.get("columns", [])}

    columns = []
    for title, col_type, primary in MOBILE_COLUMNS:
        source_col = source_columns.get(title, {})
        item = {
            "title": title,
            "type": col_type,
            "primary": primary,
        }
        if col_type == "PICKLIST":
            item["options"] = source_col.get("options", [])
        elif col_type == "CONTACT_LIST":
            item["contactOptions"] = source_col.get("contactOptions", [])
        columns.append(item)

    return {"name": name, "columns": columns}


def ensure_mobile_field_sheets() -> dict[str, Any]:
    created = []
    existing = []
    sheet_ids: dict[str, int] = {}

    for name in (FLORENCE_MOBILE_SHEET, COLUMBIA_MOBILE_SHEET):
        sheet = find_sheet_by_name(name)
        if sheet and sheet.get("id"):
            sheet_id = int(sheet["id"])
            existing.append({"name": name, "id": sheet_id})
            sheet_ids[name] = sheet_id
            continue

        result = store.create_sheet_in_workspace(
            WORKSPACE_ID,
            mobile_sheet_definition(name),
        )
        sheet_id = int(result.get("id"))
        created.append({"name": name, "id": sheet_id})
        sheet_ids[name] = sheet_id

    return {
        "ok": True,
        "created": created,
        "existing": existing,
        "sheet_ids": sheet_ids,
        "message": f"Created {len(created)} mobile sheet(s); {len(existing)} already existed.",
    }


def _attachment_names(sheet_id: int, row_id: int) -> set[str]:
    return {
        str(item.get("name", "")).strip()
        for item in store.list_row_attachments(sheet_id, row_id)
        if str(item.get("name", "")).strip()
    }


def _copy_new_attachments(
    source_sheet_id: int,
    source_row_id: int,
    target_sheet_id: int,
    target_row_id: int,
) -> int:
    copied = 0
    target_names = _attachment_names(target_sheet_id, target_row_id)
    for attachment in store.list_row_attachments(source_sheet_id, source_row_id):
        name = str(attachment.get("name", "")).strip()
        attachment_id = attachment.get("id")
        if not name or not attachment_id or name in target_names:
            continue
        data, filename, mime = store.download_attachment(int(attachment_id))
        store.attach_file_to_row(
            target_sheet_id,
            target_row_id,
            filename=filename,
            mime_type=mime,
            data=data,
        )
        target_names.add(name)
        copied += 1
    return copied


def sync_mobile_field_sheets() -> dict[str, Any]:
    setup = ensure_mobile_field_sheets()
    if not setup.get("ok"):
        return setup

    mobile_ids = {
        name: int(sheet_id)
        for name, sheet_id in setup.get("sheet_ids", {}).items()
    }
    for name in (FLORENCE_MOBILE_SHEET, COLUMBIA_MOBILE_SHEET):
        if name not in mobile_ids:
            return {"ok": False, "message": f"Unable to locate {name}."}

    master_rows = record_map(FIELD_SHEET_ID, force=True)
    master_by_project = by_project(master_rows)

    stats = {
        "created_rows": 0,
        "updated_mobile_rows": 0,
        "updated_master_rows": 0,
        "copied_attachments": 0,
        "archived_rows": 0,
    }

    # Load both mobile sheets once.
    mobile_rows_by_sheet = {}
    mobile_by_project_by_sheet = {}
    for sheet_name, sheet_id in mobile_ids.items():
        rows = record_map(sheet_id, force=True)
        mobile_rows_by_sheet[sheet_name] = rows
        mobile_by_project_by_sheet[sheet_name] = by_project(rows)

    for project_id, master in master_by_project.items():
        market = str(master.get("Market", "")).strip()
        target_name = (
            FLORENCE_MOBILE_SHEET if market == "Florence"
            else COLUMBIA_MOBILE_SHEET if market == "Columbia"
            else ""
        )
        if not target_name:
            continue

        target_id = mobile_ids[target_name]
        target_rows = mobile_by_project_by_sheet[target_name]
        mobile = target_rows.get(project_id)

        # Archived or closed jobs do not stay in the technician sheets.
        should_hide = bool(master.get("Archived")) or str(master.get("Status", "")).strip() == "Closed"
        if should_hide:
            if mobile:
                store.delete_row(target_id, int(mobile["_row_id"]))
                stats["archived_rows"] += 1
            continue

        if not mobile:
            values = {"Project ID": project_id, "Master Row ID": str(master["_row_id"])}
            for field in MANAGER_TO_MOBILE_FIELDS + TECH_TO_MASTER_FIELDS:
                values[field] = master.get(field, "")
            new_row = store.add_row(target_id, values)
            mobile_row_id = int(new_row.get("id"))
            stats["created_rows"] += 1
            stats["copied_attachments"] += _copy_new_attachments(
                FIELD_SHEET_ID,
                int(master["_row_id"]),
                target_id,
                mobile_row_id,
            )
            continue

        # Manager-controlled values always flow from master -> mobile.
        mobile_updates = {"Master Row ID": str(master["_row_id"])}
        for field in MANAGER_TO_MOBILE_FIELDS:
            if mobile.get(field, "") != master.get(field, ""):
                mobile_updates[field] = master.get(field, "")
        if len(mobile_updates) > 1 or str(mobile.get("Master Row ID", "")) != str(master["_row_id"]):
            store.update_row(target_id, int(mobile["_row_id"]), mobile_updates)
            stats["updated_mobile_rows"] += 1

        # Technician-controlled values flow mobile -> master.
        master_updates = {}
        for field in TECH_TO_MASTER_FIELDS:
            if master.get(field, "") != mobile.get(field, ""):
                master_updates[field] = mobile.get(field, "")
        if master_updates:
            store.update_row(FIELD_SHEET_ID, int(master["_row_id"]), master_updates)
            stats["updated_master_rows"] += 1

        # Attachments flow both directions and are de-duplicated by filename.
        stats["copied_attachments"] += _copy_new_attachments(
            target_id,
            int(mobile["_row_id"]),
            FIELD_SHEET_ID,
            int(master["_row_id"]),
        )
        stats["copied_attachments"] += _copy_new_attachments(
            FIELD_SHEET_ID,
            int(master["_row_id"]),
            target_id,
            int(mobile["_row_id"]),
        )

    # Remove jobs from wrong market sheets or jobs no longer present in master.
    for sheet_name, rows in mobile_rows_by_sheet.items():
        expected_market = "Florence" if sheet_name == FLORENCE_MOBILE_SHEET else "Columbia"
        sheet_id = mobile_ids[sheet_name]
        for mobile in rows:
            project_id = str(mobile.get("Project ID", "")).strip()
            master = master_by_project.get(project_id)
            if not master or str(master.get("Market", "")).strip() != expected_market:
                store.delete_row(sheet_id, int(mobile["_row_id"]))
                stats["archived_rows"] += 1

    return {
        "ok": True,
        "message": (
            f"Mobile sync complete: {stats['created_rows']} row(s) created, "
            f"{stats['updated_master_rows']} master row(s) updated, "
            f"{stats['updated_mobile_rows']} mobile row(s) refreshed, "
            f"{stats['copied_attachments']} attachment(s) copied."
        ),
        **stats,
    }


def build_field_reports() -> dict[str, Any]:
    """Create the Florence and Columbia technician reports in the Graycliff workspace."""
    sheet = store.get_sheet(FIELD_SHEET_ID, force=True)
    column_by_title = {c["title"]: c for c in sheet.get("columns", [])}

    visible_titles = [
        "Project ID",
        "Priority",
        "Task Name",
        "Address",
        "City",
        "Job Type",
        "CRQ Number",
        "Due Date",
        "Assigned Technician",
        "Status",
        "Date Started",
        "Date Field Completed",
        "Work Performed",
        "Field File Complete",
        "Required Photos Complete",
        "Manager Notes",
        "Customer Notes",
    ]

    missing = [title for title in visible_titles if title not in column_by_title]
    if missing:
        return {"ok": False, "message": "Missing report columns: " + ", ".join(missing)}

    market_column = column_by_title["Market"]
    status_column = column_by_title["Status"]

    columns = []
    for index, title in enumerate(visible_titles):
        source = column_by_title[title]
        item = {
            "index": index,
            "title": title,
            "type": source["type"],
            "width": 150,
        }
        if title == "Project ID":
            item["primary"] = True
        columns.append(item)

    existing = {r.get("name"): r for r in store.list_reports()}
    created = []
    skipped = []

    for market in ("Florence", "Columbia"):
        name = f"{market} Field Work"
        if name in existing:
            skipped.append(name)
            continue

        body = {
            "name": name,
            "destination": {
                "destinationType": "workspace",
                "destinationId": WORKSPACE_ID,
            },
            "scope": [
                {
                    "assetType": "sheet",
                    "assetId": FIELD_SHEET_ID,
                }
            ],
            "columns": columns,
            "isSummaryReport": False,
            "reportDefinition": {
                "filters": {
                    "operator": "AND",
                    "criteria": [
                        {
                            "column": {
                                "title": market_column["title"],
                                "type": market_column["type"],
                            },
                            "operator": "EQUAL",
                            "values": [market],
                        },
                        {
                            "column": {
                                "title": status_column["title"],
                                "type": status_column["type"],
                            },
                            "operator": "NOT_EQUAL",
                            "values": ["Closed"],
                        },
                    ],
                },
                "sortingCriteria": [
                    {
                        "column": {"title": "Priority", "type": column_by_title["Priority"]["type"]},
                        "sortingDirection": "DESCENDING",
                    },
                    {
                        "column": {"title": "Due Date", "type": column_by_title["Due Date"]["type"]},
                        "sortingDirection": "ASCENDING",
                    },
                ],
            },
        }
        result = store.create_report(body)
        created.append({"name": name, "id": result.get("id"), "permalink": result.get("permalink")})

    return {
        "ok": True,
        "message": f"Created {len(created)} report(s); {len(skipped)} already existed.",
        "created": created,
        "skipped": skipped,
    }


def sync_billing_queue() -> dict[str, Any]:
    field_rows = record_map(FIELD_SHEET_ID, force=True)
    billing_rows = record_map(BILLING_SHEET_ID, force=True)
    existing = by_project(billing_rows)
    created = 0

    for job in field_rows:
        project_id = str(job.get("Project ID", "")).strip()
        if not project_id or project_id in existing:
            continue
        if str(job.get("Status", "")).strip() not in {"Field Complete", "Office Approved", "Missing Documents"}:
            continue

        store.add_row(
            BILLING_SHEET_ID,
            {
                "Project ID": project_id,
                "Market": job.get("Market", ""),
                "Task Name": job.get("Task Name", ""),
                "Job Type": job.get("Job Type", ""),
                "CRQ Number": job.get("CRQ Number", ""),
                "Work Performed": job.get("Work Performed", ""),
                "Billing Status": "Review",
                "Payment Status": "Unpaid",
                "Created At": datetime.now().isoformat(timespec="seconds"),
                "Updated At": datetime.now().isoformat(timespec="seconds"),
            },
        )
        created += 1
    return {"ok": True, "created": created}


def start_scheduler() -> None:
    global scheduler
    if os.getenv("ENABLE_BACKGROUND_SYNC", "true").lower() not in {"1", "true", "yes"}:
        return
    if scheduler:
        return

    minutes = max(int(os.getenv("TECH_SYNC_MINUTES", "15")), 5)
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        sync_technician_contacts,
        "interval",
        minutes=minutes,
        id="technician_contact_sync",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        sync_billing_queue,
        "interval",
        minutes=minutes,
        id="billing_queue_sync",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        sync_mobile_field_sheets,
        "interval",
        minutes=minutes,
        id="mobile_field_sheet_sync",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()


init_db()
start_scheduler()


@app.route("/healthz")
def healthz():
    return {"ok": True, "app": APP_NAME}, 200


@app.route("/")
def index():
    if session.get("user"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        with db() as connection:
            user = connection.execute(
                "SELECT * FROM users WHERE email=? AND active=1", (email,)
            ).fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Incorrect email or password.", "error")
            return render_template("login.html"), 401
        session.clear()
        session.update(
            {
                "user": user["email"],
                "display_name": user["display_name"],
                "role": user["role"],
            }
        )
        log_action("Login")
        return redirect(request.args.get("next") or url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@require_login
def dashboard():
    if session.get("role") == "graycliff":
        return redirect(url_for("customer_jobs"))

    field_rows = record_map(FIELD_SHEET_ID)
    billing_rows = record_map(BILLING_SHEET_ID)
    metrics = {
        "open": sum(
            str(r.get("Status", "")) not in {"Office Approved", "Closed"}
            and not bool(r.get("Archived"))
            for r in field_rows
        ),
        "field_complete": sum(str(r.get("Status", "")) == "Field Complete" for r in field_rows),
        "missing": sum(
            str(r.get("Status", "")) == "Missing Documents"
            or str(r.get("Office Review Status", "")) == "Missing Documents"
            for r in field_rows
        ),
        "ready_to_bill": sum(str(r.get("Billing Status", "")) == "Ready to Bill" for r in billing_rows),
        "invoiced": sum(str(r.get("Billing Status", "")) in {"Invoiced", "Sent"} for r in billing_rows),
    }
    return render_template("dashboard.html", metrics=metrics)


@app.route("/office/work-orders")
@roles("admin", "office")
def office_work_orders():
    rows = record_map(FIELD_SHEET_ID)
    status = request.args.get("status", "").strip()
    if status:
        rows = [row for row in rows if str(row.get("Status", "")) == status]
    rows.sort(key=lambda r: (str(r.get("Due Date", "9999-99-99")), str(r.get("Project ID", ""))))
    return render_template("office_work_orders.html", jobs=rows, selected_status=status)


@app.route("/office/work-orders/<project_id>")
@roles("admin", "office")
def office_work_order_detail(project_id: str):
    jobs = by_project(record_map(FIELD_SHEET_ID))
    job = jobs.get(project_id)
    if not job:
        abort(404)
    attachments = store.list_row_attachments(FIELD_SHEET_ID, job["_row_id"])
    billing = by_project(record_map(BILLING_SHEET_ID)).get(project_id)
    return render_template(
        "office_work_order_detail.html",
        job=job,
        attachments=attachments,
        billing=billing,
    )


@app.route("/office/work-orders/<project_id>/review", methods=["POST"])
@roles("admin", "office")
def review_work_order(project_id: str):
    jobs = by_project(record_map(FIELD_SHEET_ID, force=True))
    job = jobs.get(project_id)
    if not job:
        abort(404)

    action = request.form.get("action")
    if action == "approve":
        values = {"Status": "Office Approved", "Office Review Status": "Approved"}
    elif action == "missing":
        values = {"Status": "Missing Documents", "Office Review Status": "Missing Documents"}
    else:
        abort(400)

    store.update_row(FIELD_SHEET_ID, job["_row_id"], values)
    sync_billing_queue()
    log_action("Review Work Order", "project", project_id, action)
    flash("Work order updated.", "success")
    return redirect(url_for("office_work_order_detail", project_id=project_id))


@app.route("/office/billing")
@roles("admin", "office")
def billing_queue():
    sync_billing_queue()
    rows = record_map(BILLING_SHEET_ID, force=True)
    rows.sort(key=lambda r: (str(r.get("Billing Status", "")), str(r.get("Project ID", ""))))
    return render_template("billing_queue.html", billing_rows=rows)


@app.route("/office/billing/<project_id>", methods=["GET", "POST"])
@roles("admin", "office")
def billing_detail(project_id: str):
    billing_rows = by_project(record_map(BILLING_SHEET_ID, force=True))
    row = billing_rows.get(project_id)
    if not row:
        abort(404)

    if request.method == "POST":
        values = {
            "Billing Status": request.form.get("billing_status", "Review"),
            "Office Notes": request.form.get("office_notes", "").strip(),
            "Invoice Number": request.form.get("invoice_number", "").strip(),
            "Invoice Date": request.form.get("invoice_date", "").strip(),
            "Invoice Amount": request.form.get("invoice_amount", "").strip(),
            "Payment Status": request.form.get("payment_status", "Unpaid"),
            "Updated At": datetime.now().isoformat(timespec="seconds"),
        }
        store.update_row(BILLING_SHEET_ID, row["_row_id"], values)
        log_action("Update Billing", "project", project_id, str(values))
        flash("Billing record saved.", "success")
        return redirect(url_for("billing_detail", project_id=project_id))

    job = by_project(record_map(FIELD_SHEET_ID)).get(project_id)
    attachments = (
        store.list_row_attachments(FIELD_SHEET_ID, job["_row_id"]) if job else []
    )
    return render_template("billing_detail.html", billing=row, job=job, attachments=attachments)


@app.route("/attachments/<int:attachment_id>")
@require_login
def attachment_download(attachment_id: int):
    data, filename, mime = store.download_attachment(attachment_id)
    return send_file(BytesIO(data), mimetype=mime, as_attachment=True, download_name=filename)


@app.route("/customer/jobs")
@roles("graycliff", "admin")
def customer_jobs():
    jobs = [
        row
        for row in record_map(FIELD_SHEET_ID)
        if not bool(row.get("Archived"))
    ]
    billing = by_project(record_map(BILLING_SHEET_ID))
    for job in jobs:
        job["_billing"] = billing.get(str(job.get("Project ID", "")).strip())
    jobs.sort(key=lambda r: (str(r.get("Due Date", "9999-99-99")), str(r.get("Project ID", ""))))
    return render_template("customer_jobs.html", jobs=jobs)


@app.route("/customer/jobs/<project_id>")
@roles("graycliff", "admin")
def customer_job_detail(project_id: str):
    job = by_project(record_map(FIELD_SHEET_ID)).get(project_id)
    if not job:
        abort(404)
    billing = by_project(record_map(BILLING_SHEET_ID)).get(project_id)
    attachments = []
    if str(job.get("Status", "")) in {"Office Approved", "Closed"}:
        attachments = store.list_row_attachments(FIELD_SHEET_ID, job["_row_id"])
    return render_template(
        "customer_job_detail.html",
        job=job,
        billing=billing,
        attachments=attachments,
    )



@app.route("/repair-admin")
@require_login
def repair_admin():
    current_email = session.get("user", "").strip().lower()
    if not current_email:
        abort(403)

    with db() as connection:
        connection.execute(
            "UPDATE users SET role='admin', active=1 WHERE email=?",
            (current_email,),
        )

    session["role"] = "admin"
    flash("Administrator access repaired.", "success")
    return redirect(url_for("dashboard"))


@app.route("/admin/users", methods=["GET", "POST"])
@roles("admin")
def users():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        display_name = request.form.get("display_name", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "graycliff")
        if not email or not display_name or not password or role not in {"admin", "office", "graycliff"}:
            flash("Complete every user field.", "error")
        else:
            try:
                with db() as connection:
                    connection.execute(
                        """
                        INSERT INTO users(email,display_name,password_hash,role,active,created_at)
                        VALUES(?,?,?,?,1,?)
                        """,
                        (
                            email,
                            display_name,
                            generate_password_hash(password),
                            role,
                            datetime.now().isoformat(timespec="seconds"),
                        ),
                    )
                flash("User created.", "success")
            except sqlite3.IntegrityError:
                flash("That email already exists.", "error")
        return redirect(url_for("users"))

    with db() as connection:
        all_users = connection.execute(
            "SELECT id,email,display_name,role,active,created_at FROM users ORDER BY role,display_name"
        ).fetchall()
    return render_template("users.html", users=all_users)




@app.route("/admin/build-mobile-field-sheets", methods=["POST"])
@require_login
def admin_build_mobile_field_sheets():
    result = sync_mobile_field_sheets()
    if result.get("ok"):
        flash(result.get("message", "Mobile field sheets are ready."), "success")
        log_action("Build Mobile Field Sheets", "workspace", str(WORKSPACE_ID), str(result))
    else:
        flash(result.get("message", "Unable to build mobile field sheets."), "error")
    return redirect(url_for("dashboard"))


@app.route("/admin/build-field-views", methods=["POST"])
@require_login
def admin_build_field_views():
    result = build_field_reports()
    if result.get("ok"):
        flash(result.get("message", "Field reports created."), "success")
        log_action("Build Field Reports", "workspace", str(WORKSPACE_ID), str(result))
    else:
        flash(result.get("message", "Unable to create field reports."), "error")
    return redirect(url_for("dashboard"))


@app.route("/admin/sync", methods=["POST"])
@roles("admin")
def admin_sync():
    tech_result = sync_technician_contacts()
    billing_result = sync_billing_queue()
    if tech_result.get("ok"):
        flash(
            f"{tech_result.get('message')} Billing queue added {billing_result.get('created', 0)} record(s).",
            "success",
        )
    else:
        flash(tech_result.get("message", "Sync failed."), "error")
    return redirect(request.referrer or url_for("dashboard"))


@app.errorhandler(SmartsheetError)
def smartsheet_error(exc: SmartsheetError):
    return render_template("error.html", title="Smartsheet connection error", message=str(exc)), 503


@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", title="Access denied", message="You do not have access to this page."), 403


@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", title="Not found", message="That record could not be found."), 404


if __name__ == "__main__":
    app.run(debug=True)
