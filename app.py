from __future__ import annotations

import hmac
import html
import mimetypes
import zipfile
from email import policy
from email.parser import BytesParser
import os
import secrets
import sqlite3
import threading
from datetime import date, datetime
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
from werkzeug.utils import secure_filename

from smartsheet_api import SmartsheetClient, SmartsheetError, rows_as_records
from graph_import import (
    GraphImportError,
    access_token as graph_access_token,
    attachment_bytes as graph_attachment_bytes,
    configured as graph_configured,
    get_message_mime,
    list_attachments as graph_list_attachments,
    list_recent_messages as graph_list_recent_messages,
    ensure_mail_folder as graph_ensure_mail_folder,
    move_message as graph_move_message,
    mark_message_read as graph_mark_message_read,
    mailbox as graph_mailbox,
    parse_recognized_ntp,
    mailbox_diagnostics as graph_mailbox_diagnostics,
    enrich_ntp_from_attachments,
)

APP_NAME = "Graycliff Project Portal"

WORKSPACE_ID = 3074739741714308
FIELD_SHEET_ID = 1440710464065412
DIRECTORY_SHEET_ID = 7015354675974020
BILLING_SHEET_ID = 7158170928500612
PAYMENTS_SHEET_ID = 6526836505792388
PAYMENT_MATCHES_SHEET_ID = 435877095362436

FIELD_DOCUMENTS_SHEET_NAME = "Graycliff Field Documents"
BILLING_DOCUMENTS_SHEET_NAME = "Graycliff Billing Documents"

_DOCUMENT_SHEET_LOCK = threading.RLock()

ASSIGNED_TECH_COLUMN_ID = 7656679053496196

FLORENCE_MOBILE_SHEET = "Florence Technician Jobs"
COLUMBIA_MOBILE_SHEET = "Columbia Technician Jobs"

MOBILE_COLUMNS = [
    ("Project ID", "TEXT_NUMBER", True),
    ("Job Summary", "TEXT_NUMBER", False),
    ("Location", "TEXT_NUMBER", False),
    ("Due / Priority", "TEXT_NUMBER", False),
    ("Assigned Technician", "CONTACT_LIST", False),
    ("Status", "PICKLIST", False),
    ("Work Performed", "TEXT_NUMBER", False),
    ("Field File Complete", "CHECKBOX", False),
    ("Date Field Completed", "DATE", False),
    ("Date Started", "DATE", False),
    ("Master Row ID", "TEXT_NUMBER", False),
]

MANAGER_TO_MOBILE_FIELDS = [
    "Priority", "Task Name", "Address", "City", "Job Type", "CRQ Number",
    "Due Date", "Assigned Technician", "Manager Notes", "Customer Notes",
]

TECH_TO_MASTER_FIELDS = [
    "Status", "Work Performed", "Field File Complete",
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

            CREATE TABLE IF NOT EXISTS imported_mail (
                message_id TEXT PRIMARY KEY,
                internet_message_id TEXT,
                subject TEXT NOT NULL,
                project_id TEXT,
                result TEXT NOT NULL,
                imported_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS field_document_selection (
                project_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                selected_at TEXT NOT NULL,
                selected_by TEXT,
                PRIMARY KEY(project_id, filename)
            );

            CREATE TABLE IF NOT EXISTS billing_document_selection (
                project_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                selected_at TEXT NOT NULL,
                selected_by TEXT,
                source TEXT,
                PRIMARY KEY(project_id, filename)
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




def find_sheets_by_name(name: str) -> list[dict[str, Any]]:
    return [
        sheet
        for sheet in store.list_sheets()
        if str(sheet.get("name", "")).strip() == name
    ]


def find_sheet_by_name(name: str) -> dict[str, Any] | None:
    matches = find_sheets_by_name(name)
    if not matches:
        return None

    # Prefer the oldest sheet ID. The original technician sheet predates the
    # accidental duplicates and contains the established mobile configuration.
    return min(matches, key=lambda sheet: int(sheet.get("id", 0)))



def build_job_summary(row: dict[str, Any]) -> str:
    job_type = str(row.get("Job Type", "")).strip()
    task_name = str(row.get("Task Name", "")).strip()
    crq = str(row.get("CRQ Number", "")).strip()

    first_line = " - ".join(part for part in (job_type, task_name) if part)
    lines = [first_line] if first_line else []
    if crq:
        lines.append(f"CRQ: {crq}")
    return "\n".join(lines)


def build_location(row: dict[str, Any]) -> str:
    address = str(row.get("Address", "")).strip()
    city = str(row.get("City", "")).strip()
    return "\n".join(part for part in (address, city) if part)


def build_due_priority(row: dict[str, Any]) -> str:
    due = str(row.get("Due Date", "")).strip()
    priority = str(row.get("Priority", "")).strip()
    lines = []
    if due:
        lines.append(f"Due: {due}")
    if priority:
        lines.append(f"Priority: {priority}")
    return "\n".join(lines)


def ensure_mobile_summary_columns(sheet_id: int) -> None:
    sheet = store.get_sheet(sheet_id, force=True)
    existing = {c["title"] for c in sheet.get("columns", [])}

    # Smartsheet requires a consistent insertion index when adding multiple
    # columns in one request. Add these individually so each can be placed
    # directly after Project ID in the intended order.
    for index, title in reversed(
        list(enumerate(("Job Summary", "Location", "Due / Priority"), start=1))
    ):
        if title in existing:
            continue
        store.add_columns(
            sheet_id,
            [
                {
                    "title": title,
                    "type": "TEXT_NUMBER",
                    "index": index,
                }
            ],
        )



def document_sheet_definition(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "columns": [
            {"title": "Project ID", "type": "TEXT_NUMBER", "primary": True},
            {"title": "Document Type", "type": "TEXT_NUMBER"},
            {"title": "Master Row ID", "type": "TEXT_NUMBER"},
        ],
    }


def _get_portal_setting(key: str) -> str:
    with db() as connection:
        row = connection.execute(
            "SELECT value FROM portal_settings WHERE key=?",
            (key,),
        ).fetchone()
    return str(row["value"]) if row else ""


def _set_portal_setting(key: str, value: str) -> None:
    with db() as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO portal_settings(key, value)
            VALUES(?,?)
            """,
            (key, value),
        )


def ensure_document_sheet(name: str) -> int:
    setting_key = "document_sheet_id:" + name.lower().replace(" ", "_")

    # A persistent registry avoids relying on Smartsheet's eventually consistent
    # sheet listing every time a job page or scheduled sync runs.
    registered = _get_portal_setting(setting_key)
    if registered.isdigit():
        return int(registered)

    with _DOCUMENT_SHEET_LOCK:
        # Another request in this process may have created/registered it while
        # this request was waiting for the lock.
        registered = _get_portal_setting(setting_key)
        if registered.isdigit():
            return int(registered)

        matches = find_sheets_by_name(name)
        if matches:
            # Reuse the oldest existing sheet and ignore accidental duplicates.
            sheet_id = int(
                min(matches, key=lambda sheet: int(sheet.get("id", 0)))["id"]
            )
        else:
            created = store.create_sheet_in_workspace(
                WORKSPACE_ID,
                document_sheet_definition(name),
            )
            sheet_id = int(created["id"])

        _set_portal_setting(setting_key, str(sheet_id))
        return sheet_id


def ensure_document_row(
    sheet_id: int,
    project_id: str,
    master_row_id: int,
    document_type: str,
) -> int:
    existing = by_project(record_map(sheet_id, force=True)).get(project_id)
    if existing:
        return int(existing["_row_id"])
    created = store.add_row(
        sheet_id,
        {
            "Project ID": project_id,
            "Document Type": document_type,
            "Master Row ID": str(master_row_id),
        },
    )
    return int(created["id"])


def document_sheet_context(project_id: str, master_row_id: int) -> dict[str, int]:
    field_sheet_id = ensure_document_sheet(FIELD_DOCUMENTS_SHEET_NAME)
    billing_sheet_id = ensure_document_sheet(BILLING_DOCUMENTS_SHEET_NAME)
    return {
        "field_sheet_id": field_sheet_id,
        "field_row_id": ensure_document_row(
            field_sheet_id, project_id, master_row_id, "Field"
        ),
        "billing_sheet_id": billing_sheet_id,
        "billing_row_id": ensure_document_row(
            billing_sheet_id, project_id, master_row_id, "Billing"
        ),
    }


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
            ensure_mobile_summary_columns(sheet_id)
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

    duplicate_counts = {
        name: max(0, len(find_sheets_by_name(name)) - 1)
        for name in (FLORENCE_MOBILE_SHEET, COLUMBIA_MOBILE_SHEET)
    }
    duplicate_total = sum(duplicate_counts.values())

    return {
        "ok": True,
        "created": created,
        "existing": existing,
        "sheet_ids": sheet_ids,
        "duplicate_counts": duplicate_counts,
        "message": (
            f"Created {len(created)} mobile sheet(s); {len(existing)} already existed. "
            f"Detected {duplicate_total} duplicate mobile sheet(s); no new duplicates will be created."
        ),
    }


def selected_field_document_names(project_id: str) -> set[str]:
    with db() as connection:
        rows = connection.execute(
            "SELECT filename FROM field_document_selection WHERE project_id=?",
            (project_id,),
        ).fetchall()
    return {str(row["filename"]) for row in rows}


def select_field_document(project_id: str, filename: str) -> None:
    with db() as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO field_document_selection(
                project_id, filename, selected_at, selected_by
            ) VALUES(?,?,?,?)
            """,
            (
                project_id,
                filename,
                datetime.now().isoformat(timespec="seconds"),
                session.get("user_email", ""),
            ),
        )


def unselect_field_document(project_id: str, filename: str) -> None:
    with db() as connection:
        connection.execute(
            "DELETE FROM field_document_selection WHERE project_id=? AND filename=?",
            (project_id, filename),
        )


def selected_billing_document_names(project_id: str) -> set[str]:
    with db() as connection:
        rows = connection.execute(
            "SELECT filename FROM billing_document_selection WHERE project_id=?",
            (project_id,),
        ).fetchall()
    return {str(row["filename"]) for row in rows}


def select_billing_document(
    project_id: str,
    filename: str,
    *,
    source: str = "office",
) -> None:
    with db() as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO billing_document_selection(
                project_id, filename, selected_at, selected_by, source
            ) VALUES(?,?,?,?,?)
            """,
            (
                project_id,
                filename,
                datetime.now().isoformat(timespec="seconds"),
                session.get("user_email", "system"),
                source,
            ),
        )


def unselect_billing_document(project_id: str, filename: str) -> None:
    with db() as connection:
        connection.execute(
            "DELETE FROM billing_document_selection WHERE project_id=? AND filename=?",
            (project_id, filename),
        )


def _copy_mobile_attachments_to_billing(
    project_id: str,
    mobile_sheet_id: int,
    mobile_row_id: int,
    master_row_id: int,
) -> int:
    copied = 0
    context = document_sheet_context(project_id, master_row_id)
    target_sheet_id = context["billing_sheet_id"]
    target_row_id = context["billing_row_id"]
    source_items = store.list_row_attachments(mobile_sheet_id, mobile_row_id)
    target_names = _attachment_names(target_sheet_id, target_row_id)

    for item in source_items:
        name = str(item.get("name", "")).strip()
        attachment_id = item.get("id")
        if not name or not attachment_id or name in target_names:
            continue
        data, filename, mime = store.download_attachment(
            mobile_sheet_id,
            int(attachment_id),
        )
        store.attach_file_to_row(
            target_sheet_id,
            target_row_id,
            filename=filename,
            mime_type=mime,
            data=data,
        )
        target_names.add(filename)
        copied += 1

    return copied


def _attachment_names(sheet_id: int, row_id: int) -> set[str]:
    return {
        str(item.get("name", "")).strip()
        for item in store.list_row_attachments(sheet_id, row_id)
        if str(item.get("name", "")).strip()
    }


def _sync_all_row_attachments(
    source_sheet_id: int,
    source_row_id: int,
    target_sheet_id: int,
    target_row_id: int,
) -> int:
    copied = 0
    source_items = store.list_row_attachments(source_sheet_id, source_row_id)
    target_items = store.list_row_attachments(target_sheet_id, target_row_id)
    source_by_name = {
        str(item.get("name", "")).strip(): item
        for item in source_items
        if str(item.get("name", "")).strip()
    }
    target_by_name = {
        str(item.get("name", "")).strip(): item
        for item in target_items
        if str(item.get("name", "")).strip()
    }

    for name, item in target_by_name.items():
        if name not in source_by_name and item.get("id"):
            store.delete_attachment(target_sheet_id, int(item["id"]))

    for name, item in source_by_name.items():
        if name in target_by_name or not item.get("id"):
            continue
        data, filename, mime = store.download_attachment(
            source_sheet_id,
            int(item["id"]),
        )
        store.attach_file_to_row(
            target_sheet_id,
            target_row_id,
            filename=filename,
            mime_type=mime,
            data=data,
        )
        copied += 1

    return copied


def _today() -> str:
    return date.today().isoformat()


def _stamp_status_dates(
    master: dict[str, Any],
    mobile: dict[str, Any],
    master_updates: dict[str, Any],
    mobile_updates: dict[str, Any],
) -> None:
    new_status = str(master_updates.get("Status", mobile.get("Status", master.get("Status", "")))).strip()

    started = str(master.get("Date Started", "")).strip()
    completed = str(master.get("Date Field Completed", "")).strip()

    if new_status in {"In Progress", "Field Complete"} and not started:
        started = _today()
        master_updates["Date Started"] = started
        mobile_updates["Date Started"] = started

    if new_status == "Field Complete" and not completed:
        completed = _today()
        master_updates["Date Field Completed"] = completed
        mobile_updates["Date Field Completed"] = completed


def _mail_already_processed(message_id: str) -> bool:
    with db() as connection:
        return connection.execute(
            "SELECT 1 FROM imported_mail WHERE message_id=?",
            (message_id,),
        ).fetchone() is not None


def _record_mail_result(
    message: dict[str, Any],
    *,
    project_id: str = "",
    result: str,
) -> None:
    with db() as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO imported_mail(
                message_id,internet_message_id,subject,project_id,result,imported_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                str(message.get("id", "")),
                str(message.get("internetMessageId", "")),
                str(message.get("subject", "")),
                project_id,
                result,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def _next_manual_project_id() -> str:
    prefix = f"GC-{datetime.now():%Y%m%d}-"
    existing = set(by_project(record_map(FIELD_SHEET_ID, force=True)))
    with db() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS total FROM imported_mail WHERE project_id LIKE ?",
            (prefix + "%",),
        ).fetchone()["total"]
    number = int(count) + 1
    while f"{prefix}{number:04d}" in existing:
        number += 1
    return f"{prefix}{number:04d}"


def create_field_job(values: dict[str, Any]) -> dict[str, Any]:
    project_id = str(values.get("Project ID", "")).strip() or _next_manual_project_id()
    existing = by_project(record_map(FIELD_SHEET_ID, force=True))
    if project_id in existing:
        raise ValueError(f"Job {project_id} already exists.")

    clean = {
        "Project ID": project_id,
        "Market": str(values.get("Market", "")).strip(),
        "Task Name": str(values.get("Task Name", "")).strip(),
        "Address": str(values.get("Address", "")).strip(),
        "City": str(values.get("City", "")).strip(),
        "Job Type": str(values.get("Job Type", "Standard")).strip() or "Standard",
        "CRQ Number": str(values.get("CRQ Number", "")).strip(),
        "Due Date": str(values.get("Due Date", "")).strip(),
        "Assigned Technician": values.get("Assigned Technician", ""),
        "Priority": str(values.get("Priority", "Normal")).strip() or "Normal",
        "Status": str(values.get("Status", "Unassigned")).strip() or "Unassigned",
        "Office Review Status": str(values.get("Office Review Status", "Not Ready")).strip() or "Not Ready",
        "Customer Notes": str(values.get("Customer Notes", "")).strip(),
    }
    row = store.add_row(FIELD_SHEET_ID, clean)
    return {"project_id": project_id, "row": row, "values": clean}


def import_graycliff_mailbox() -> dict[str, Any]:
    if not graph_configured():
        return {"ok": False, "configured": False, "message": "Graycliff mailbox connection is not configured in Render.", "created": 0, "updated": 0, "ignored": 0, "failed": 0}

    token = graph_access_token()
    imported_folder = graph_ensure_mail_folder(token, "Imported")
    failed_folder = graph_ensure_mail_folder(token, "Import Failed")
    messages = graph_list_recent_messages(token, top=40)
    existing = by_project(record_map(FIELD_SHEET_ID, force=True))
    stats = {"created": 0, "updated": 0, "ignored": 0, "failed": 0, "attachments": 0}

    for message in reversed(messages):
        message_id = str(message.get("id", ""))
        if not message_id:
            continue

        # Inbox is the processing queue. Moving a message back to Inbox is the
        # retry switch; read/unread state and prior database records do not block it.
        parsed = parse_recognized_ntp(message)
        if not parsed:
            stats["ignored"] += 1
            _record_mail_result(message, result="failed-unrecognized-format")
            try:
                graph_move_message(token, message_id, failed_folder)
            except Exception:
                try:
                    graph_mark_message_read(token, message_id)
                except Exception:
                    pass
            continue

        message_attachments = (
            graph_list_attachments(token, message_id)
            if message.get("hasAttachments")
            else []
        )
        parsed = enrich_ntp_from_attachments(parsed, message_attachments)

        project_id = parsed.work_order
        try:
            task_name = parsed.address or parsed.customer_name or f"Graycliff Work Order {project_id}"
            source_values = {
                "Market": parsed.market,
                "Task Name": task_name,
                "Address": parsed.address,
                "City": parsed.city,
                "Job Type": "Standard",
                "Due Date": parsed.due_date,
                "Priority": "Normal",
                "Customer Notes": f"PRISM {parsed.prism}" if parsed.prism else "",
            }

            if project_id in existing:
                row = existing[project_id]
                # Revisions update source data but never reset assignment or workflow status.
                update_values = {
                    key: value
                    for key, value in source_values.items()
                    if value and row.get(key, "") != value
                }
                if update_values:
                    store.update_row(FIELD_SHEET_ID, int(row["_row_id"]), update_values)
                target_row_id = int(row["_row_id"])
                stats["updated"] += 1
                result_name = "updated-revision"
            else:
                created = create_field_job(
                    {
                        "Project ID": project_id,
                        **source_values,
                        "Status": "Unassigned",
                        "Office Review Status": "Not Ready",
                    }
                )
                target_row_id = int(created["row"].get("id"))
                stats["created"] += 1
                result_name = "created"
                existing = by_project(record_map(FIELD_SHEET_ID, force=True))

            current_names = _attachment_names(FIELD_SHEET_ID, target_row_id)
            for item in message_attachments:
                decoded = graph_attachment_bytes(item)
                if not decoded:
                    continue
                filename, mime_type, data = decoded
                if filename in current_names:
                    continue
                store.attach_file_to_row(
                    FIELD_SHEET_ID,
                    target_row_id,
                    filename=filename,
                    mime_type=mime_type,
                    data=data,
                )
                current_names.add(filename)
                stats["attachments"] += 1

            eml_name = f"NTP-{project_id}-{message_id[-8:]}.eml"
            if eml_name not in current_names:
                mime_data = get_message_mime(token, message_id)
                if mime_data:
                    store.attach_file_to_row(FIELD_SHEET_ID, target_row_id, filename=eml_name, mime_type="message/rfc822", data=mime_data)
                    stats["attachments"] += 1

            _record_mail_result(message, project_id=project_id, result=result_name)
            graph_move_message(token, message_id, imported_folder)
        except Exception as exc:
            stats["failed"] += 1
            _record_mail_result(message, project_id=project_id, result=f"failed: {str(exc)[:300]}")
            try:
                graph_move_message(token, message_id, failed_folder)
            except Exception:
                try:
                    graph_mark_message_read(token, message_id)
                except Exception:
                    pass

    if stats["created"] or stats["updated"]:
        sync_mobile_field_sheets()

    return {
        "ok": True, "configured": True, **stats,
        "message": (
            f"Mailbox import complete: {stats['created']} job(s) created, {stats['updated']} revision(s) updated, "
            f"{stats['failed']} failed email(s) moved to Import Failed, {stats['ignored']} unrecognized email(s) moved to Manual Review, "
            f"and {stats['attachments']} attachment(s) copied."
        ),
    }


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
        approved_for_field = str(master.get("Office Review Status", "")).strip() == "Approved"

        # Remove the job from every technician sheet when not approved, held,
        # moved to another market, archived, or closed.
        located_mobile = None
        for sheet_name, rows_by_project in mobile_by_project_by_sheet.items():
            candidate = rows_by_project.get(project_id)
            if not candidate:
                continue
            should_remove = (
                not approved_for_field
                or sheet_name != target_name
                or bool(master.get("Archived"))
                or str(master.get("Status", "")).strip() in {"Closed", "Missing Documents", "On Hold"}
            )
            if should_remove:
                store.delete_row(mobile_ids[sheet_name], int(candidate["_row_id"]))
                stats["archived_rows"] += 1
            elif sheet_name == target_name:
                located_mobile = candidate

        if not approved_for_field or not target_name:
            continue

        target_id = mobile_ids[target_name]
        target_rows = mobile_by_project_by_sheet[target_name]
        mobile = located_mobile or target_rows.get(project_id)

        should_hide = (
            bool(master.get("Archived"))
            or str(master.get("Status", "")).strip() in {"Closed", "Missing Documents", "On Hold"}
        )
        if should_hide:
            if mobile:
                store.delete_row(target_id, int(mobile["_row_id"]))
                stats["archived_rows"] += 1
            continue

        if not mobile:
            values = {
                "Project ID": project_id,
                "Job Summary": build_job_summary(master),
                "Location": build_location(master),
                "Due / Priority": build_due_priority(master),
                "Master Row ID": str(master["_row_id"]),
            }
            for field in ("Assigned Technician",) + tuple(TECH_TO_MASTER_FIELDS):
                values[field] = master.get(field, "")
            new_row = store.add_row(target_id, values)
            mobile_row_id = int(new_row.get("id"))
            stats["created_rows"] += 1
            context = document_sheet_context(project_id, int(master["_row_id"]))
            stats["copied_attachments"] += _sync_all_row_attachments(
                context["field_sheet_id"],
                context["field_row_id"],
                target_id,
                mobile_row_id,
            )
            continue

        # Mobile display fields and assignment always flow from master -> mobile.
        desired_mobile = {
            "Master Row ID": str(master["_row_id"]),
            "Job Summary": build_job_summary(master),
            "Location": build_location(master),
            "Due / Priority": build_due_priority(master),
            "Assigned Technician": master.get("Assigned Technician", ""),
        }
        mobile_updates = {
            field: value
            for field, value in desired_mobile.items()
            if mobile.get(field, "") != value
        }
        if mobile_updates:
            store.update_row(target_id, int(mobile["_row_id"]), mobile_updates)
            stats["updated_mobile_rows"] += 1

        # Technician-controlled values flow mobile -> master.
        master_updates = {}
        for field in TECH_TO_MASTER_FIELDS:
            if master.get(field, "") != mobile.get(field, ""):
                master_updates[field] = mobile.get(field, "")

        # Dates are system-stamped from status changes and never manually entered.
        date_mobile_updates = {}
        _stamp_status_dates(master, mobile, master_updates, date_mobile_updates)

        if master_updates:
            store.update_row(FIELD_SHEET_ID, int(master["_row_id"]), master_updates)
            stats["updated_master_rows"] += 1
        if date_mobile_updates:
            store.update_row(target_id, int(mobile["_row_id"]), date_mobile_updates)
            stats["updated_mobile_rows"] += 1

        # Attachments flow both directions and are de-duplicated by filename.
        stats["copied_attachments"] += _copy_mobile_attachments_to_billing(
            project_id,
            target_id,
            int(mobile["_row_id"]),
            int(master["_row_id"]),
        )
        context = document_sheet_context(project_id, int(master["_row_id"]))
        stats["copied_attachments"] += _sync_all_row_attachments(
            context["field_sheet_id"],
            context["field_row_id"],
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
    scheduler.add_job(
        import_graycliff_mailbox,
        "interval",
        minutes=minutes,
        id="graycliff_mailbox_import",
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
    on_hold_jobs = [
        r
        for r in field_rows
        if (
            str(r.get("Status", "")).strip() in {"On Hold", "Missing Documents"}
            or str(r.get("Office Review Status", "")).strip() == "Missing Documents"
        )
        and not bool(r.get("Archived"))
    ]
    metrics = {
        "open": sum(
            str(r.get("Status", "")) not in {"Office Approved", "Closed"}
            and not bool(r.get("Archived"))
            for r in field_rows
        ),
        "field_complete": sum(str(r.get("Status", "")) == "Field Complete" for r in field_rows),
        "on_hold": len(on_hold_jobs),
        "ready_to_bill": sum(str(r.get("Billing Status", "")) == "Ready to Bill" for r in billing_rows),
        "invoiced": sum(str(r.get("Billing Status", "")) in {"Invoiced", "Sent"} for r in billing_rows),
    }
    return render_template(
        "dashboard.html",
        metrics=metrics,
        on_hold_jobs=on_hold_jobs,
        mailbox_configured=graph_configured(),
        mailbox_address=graph_mailbox(),
    )



@app.route("/office/work-orders/new", methods=["GET", "POST"])
@roles("admin", "office")
def create_work_order():
    technicians = active_technicians()
    if request.method == "POST":
        try:
            result = create_field_job(
                {
                    "Project ID": request.form.get("project_id", "").strip(),
                    "Market": request.form.get("market", "").strip(),
                    "Task Name": request.form.get("task_name", "").strip(),
                    "Address": request.form.get("address", "").strip(),
                    "City": request.form.get("city", "").strip(),
                    "Job Type": request.form.get("job_type", "Standard").strip(),
                    "CRQ Number": request.form.get("crq_number", "").strip(),
                    "Due Date": request.form.get("due_date", "").strip(),
                    "Assigned Technician": request.form.get("assigned_technician", "").strip(),
                    "Priority": request.form.get("priority", "Normal").strip(),
                    "Status": "Unassigned",
                    "Customer Notes": request.form.get("customer_notes", "").strip(),
                }
            )
            project_id = result["project_id"]
            log_action("Create Work Order", "project", project_id, "manual")
            sync_mobile_field_sheets()
            flash(f"Job {project_id} created.", "success")
            return redirect(url_for("office_work_order_detail", project_id=project_id))
        except ValueError as exc:
            flash(str(exc), "error")
        except Exception as exc:
            flash(f"Unable to create job: {exc}", "error")

    return render_template("create_work_order.html", technicians=technicians)




@app.route("/admin/register-document-sheets", methods=["POST"])
@roles("admin")
def admin_register_document_sheets():
    registered = []
    for name in (FIELD_DOCUMENTS_SHEET_NAME, BILLING_DOCUMENTS_SHEET_NAME):
        matches = find_sheets_by_name(name)
        if not matches:
            sheet_id = ensure_document_sheet(name)
        else:
            sheet_id = int(
                min(matches, key=lambda sheet: int(sheet.get("id", 0)))["id"]
            )
            setting_key = "document_sheet_id:" + name.lower().replace(" ", "_")
            _set_portal_setting(setting_key, str(sheet_id))
        registered.append(f"{name}: {sheet_id}")

    flash(
        "Document sheets locked to the original sheets. "
        + " | ".join(registered),
        "success",
    )
    return redirect(url_for("dashboard"))


@app.route("/admin/mailbox-diagnostics")
@roles("admin")
def admin_mailbox_diagnostics():
    try:
        token = graph_access_token()
        details = graph_mailbox_diagnostics(token)
        return render_template("mailbox_diagnostics.html", details=details)
    except Exception as exc:
        flash(f"Mailbox diagnostics failed: {exc}", "error")
        return redirect(url_for("dashboard"))


@app.route("/admin/import-graycliff-mail", methods=["POST"])
@roles("admin")
def admin_import_graycliff_mail():
    try:
        result = import_graycliff_mailbox()
        flash(result.get("message", "Mailbox import finished."), "success" if result.get("ok") else "error")
        if result.get("ok"):
            log_action("Import Graycliff Mail", "mailbox", graph_mailbox(), str(result))
    except GraphImportError as exc:
        flash(str(exc), "error")
    return redirect(url_for("dashboard"))


@app.route("/office/work-orders")
@roles("admin", "office")
def office_work_orders():
    rows = record_map(FIELD_SHEET_ID)
    status = request.args.get("status", "").strip()
    if status == "On Hold":
        rows = [
            row for row in rows
            if (
                str(row.get("Status", "")).strip() in {"On Hold", "Missing Documents"}
                or str(row.get("Office Review Status", "")).strip() == "Missing Documents"
            )
        ]
    elif status:
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
    source_documents = store.list_row_attachments(FIELD_SHEET_ID, job["_row_id"])
    context = document_sheet_context(project_id, int(job["_row_id"]))
    field_documents = store.list_row_attachments(
        context["field_sheet_id"],
        context["field_row_id"],
    )
    try:
        discussions = store.list_row_discussions(FIELD_SHEET_ID, job["_row_id"])
    except Exception:
        discussions = []
    billing = by_project(record_map(BILLING_SHEET_ID)).get(project_id)
    return render_template(
        "office_work_order_detail.html",
        job=job,
        source_documents=source_documents,
        field_documents=field_documents,
        discussions=discussions,
        billing=billing,
        technicians=active_technicians(),
        field_sheet_id=FIELD_SHEET_ID,
        field_documents_sheet_id=context["field_sheet_id"],
        field_documents_row_id=context["field_row_id"],
    )


@app.route("/office/work-orders/<project_id>/review", methods=["POST"])
@roles("admin", "office")
def review_work_order(project_id: str):
    job = by_project(record_map(FIELD_SHEET_ID, force=True)).get(project_id)
    if not job:
        abort(404)

    action = request.form.get("action", "save")
    values = {
        "Market": request.form.get("market", "").strip(),
        "Task Name": request.form.get("task_name", "").strip(),
        "Address": request.form.get("address", "").strip(),
        "City": request.form.get("city", "").strip(),
        "Job Type": request.form.get("job_type", "Standard").strip(),
        "CRQ Number": request.form.get("crq_number", "").strip(),
        "Priority": request.form.get("priority", "Normal").strip(),
        "Due Date": request.form.get("due_date", "").strip(),
        "Assigned Technician": request.form.get("assigned_technician", "").strip(),
        "Manager Notes": request.form.get("manager_notes", "").strip(),
        "Customer Notes": request.form.get("customer_notes", "").strip(),
    }

    if action == "approve":
        missing = []
        if not values["Market"]:
            missing.append("market")
        if not values["Task Name"]:
            missing.append("task name")
        if missing:
            flash("Cannot release to field. Missing: " + ", ".join(missing) + ".", "error")
            return redirect(url_for("office_work_order_detail", project_id=project_id))
        values.update({
            "Status": "Assigned" if values["Assigned Technician"] else "Unassigned",
            "Office Review Status": "Approved",
        })
        message = "Job approved, field documents synced, and released to the market job pool."
    elif action == "hold":
        reason = request.form.get("hold_reason", "").strip()
        if reason:
            values["Manager Notes"] = reason
        values.update({"Status": "On Hold", "Office Review Status": "Missing Documents"})
        message = "Job placed on hold and hidden from technicians."
    else:
        values.update({"Office Review Status": "Not Ready"})
        message = "Manager changes saved. Job remains hidden from technicians."

    store.update_row(FIELD_SHEET_ID, int(job["_row_id"]), values)
    sync_mobile_field_sheets()
    log_action("Manager Review", "project", project_id, action)
    flash(message, "success")
    return redirect(url_for("office_work_order_detail", project_id=project_id))


@app.route("/office/work-orders/<project_id>/upload", methods=["POST"])
@roles("admin", "office")
def upload_work_order_document(project_id: str):
    job = by_project(record_map(FIELD_SHEET_ID, force=True)).get(project_id)
    if not job:
        abort(404)

    added = 0
    for uploaded in request.files.getlist("documents"):
        if not uploaded or not uploaded.filename:
            continue
        filename = secure_filename(uploaded.filename)
        data = uploaded.read()
        if not data:
            continue
        mime_type = uploaded.mimetype or "application/octet-stream"
        context = document_sheet_context(project_id, int(job["_row_id"]))
        store.attach_file_to_row(
            context["field_sheet_id"],
            context["field_row_id"],
            filename=filename,
            mime_type=mime_type,
            data=data,
        )
        added += 1

    sync_mobile_field_sheets()
    flash(f"{added} document(s) added to the field documents.", "success")
    return redirect(url_for("office_work_order_detail", project_id=project_id))



@app.route("/office/work-orders/<project_id>/documents/select", methods=["POST"])
@roles("admin", "office")
def select_work_order_field_document(project_id: str):
    job = by_project(record_map(FIELD_SHEET_ID, force=True)).get(project_id)
    if not job:
        abort(404)

    filename = request.form.get("filename", "").strip()
    source_items = store.list_row_attachments(FIELD_SHEET_ID, int(job["_row_id"]))
    source = next(
        (
            item for item in source_items
            if str(item.get("name", "")).strip() == filename
        ),
        None,
    )
    if not source or not source.get("id"):
        flash("That source document is no longer available.", "error")
        return redirect(url_for("office_work_order_detail", project_id=project_id))

    context = document_sheet_context(project_id, int(job["_row_id"]))
    current = _attachment_names(context["field_sheet_id"], context["field_row_id"])
    if filename not in current:
        data, current_name, mime = store.download_attachment(
            FIELD_SHEET_ID,
            int(source["id"]),
        )
        store.attach_file_to_row(
            context["field_sheet_id"],
            context["field_row_id"],
            filename=current_name,
            mime_type=mime,
            data=data,
        )

    sync_mobile_field_sheets()
    flash(f"{filename} added to Field Documents.", "success")
    return redirect(url_for("office_work_order_detail", project_id=project_id))


@app.route("/office/work-orders/<project_id>/documents/unselect", methods=["POST"])
@roles("admin", "office")
def unselect_work_order_field_document(project_id: str):
    job = by_project(record_map(FIELD_SHEET_ID, force=True)).get(project_id)
    if not job:
        abort(404)

    filename = request.form.get("filename", "").strip()
    context = document_sheet_context(project_id, int(job["_row_id"]))
    items = store.list_row_attachments(context["field_sheet_id"], context["field_row_id"])
    match = next(
        (
            item for item in items
            if str(item.get("name", "")).strip() == filename
        ),
        None,
    )
    if match and match.get("id"):
        store.delete_attachment(context["field_sheet_id"], int(match["id"]))

    sync_mobile_field_sheets()
    flash(f"{filename} removed from Field Documents.", "success")
    return redirect(url_for("office_work_order_detail", project_id=project_id))


@app.route("/office/work-orders/<project_id>/comment", methods=["POST"])
@roles("admin", "office")
def add_work_order_comment(project_id: str):
    job = by_project(record_map(FIELD_SHEET_ID, force=True)).get(project_id)
    if not job:
        abort(404)

    comment = request.form.get("comment", "").strip()
    if not comment:
        flash("Enter a comment first.", "error")
        return redirect(url_for("office_work_order_detail", project_id=project_id))

    author = session.get("user_email", "Office")
    store.add_row_comment(FIELD_SHEET_ID, int(job["_row_id"]), f"{author}: {comment}")
    flash("Comment added.", "success")
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
    source_documents = (
        store.list_row_attachments(FIELD_SHEET_ID, job["_row_id"]) if job else []
    )
    context = document_sheet_context(project_id, int(job["_row_id"])) if job else None
    billing_documents = (
        store.list_row_attachments(
            context["billing_sheet_id"],
            context["billing_row_id"],
        )
        if context else []
    )
    billing_names = {
        str(item.get("name", "")).strip() for item in billing_documents
    }
    available_documents = [
        item for item in source_documents
        if str(item.get("name", "")).strip() not in billing_names
    ]
    return render_template(
        "billing_detail.html",
        billing=row,
        job=job,
        billing_documents=billing_documents,
        available_documents=available_documents,
        field_sheet_id=FIELD_SHEET_ID,
        billing_documents_sheet_id=context["billing_sheet_id"] if context else 0,
        billing_documents_row_id=context["billing_row_id"] if context else 0,
    )



@app.route("/office/billing/<project_id>/documents/select", methods=["POST"])
@roles("admin", "office")
def select_billing_document_route(project_id: str):
    job = by_project(record_map(FIELD_SHEET_ID, force=True)).get(project_id)
    if not job:
        abort(404)

    filename = request.form.get("filename", "").strip()
    source_items = store.list_row_attachments(FIELD_SHEET_ID, int(job["_row_id"]))
    source = next(
        (
            item for item in source_items
            if str(item.get("name", "")).strip() == filename
        ),
        None,
    )
    if not source or not source.get("id"):
        flash("That job document is no longer available.", "error")
        return redirect(url_for("billing_detail", project_id=project_id))

    context = document_sheet_context(project_id, int(job["_row_id"]))
    current = _attachment_names(context["billing_sheet_id"], context["billing_row_id"])
    if filename not in current:
        data, current_name, mime = store.download_attachment(
            FIELD_SHEET_ID,
            int(source["id"]),
        )
        store.attach_file_to_row(
            context["billing_sheet_id"],
            context["billing_row_id"],
            filename=current_name,
            mime_type=mime,
            data=data,
        )

    flash(f"{filename} added to Billing Documents.", "success")
    return redirect(url_for("billing_detail", project_id=project_id))


@app.route("/office/billing/<project_id>/documents/unselect", methods=["POST"])
@roles("admin", "office")
def unselect_billing_document_route(project_id: str):
    job = by_project(record_map(FIELD_SHEET_ID, force=True)).get(project_id)
    if not job:
        abort(404)

    filename = request.form.get("filename", "").strip()
    context = document_sheet_context(project_id, int(job["_row_id"]))
    items = store.list_row_attachments(
        context["billing_sheet_id"],
        context["billing_row_id"],
    )
    match = next(
        (
            item for item in items
            if str(item.get("name", "")).strip() == filename
        ),
        None,
    )
    if match and match.get("id"):
        store.delete_attachment(context["billing_sheet_id"], int(match["id"]))

    flash(f"{filename} removed from Billing Documents.", "success")
    return redirect(url_for("billing_detail", project_id=project_id))


@app.route("/office/billing/<project_id>/documents/upload", methods=["POST"])
@roles("admin", "office")
def upload_billing_document(project_id: str):
    job = by_project(record_map(FIELD_SHEET_ID, force=True)).get(project_id)
    if not job:
        abort(404)

    context = document_sheet_context(project_id, int(job["_row_id"]))
    added = 0
    for uploaded in request.files.getlist("documents"):
        if not uploaded or not uploaded.filename:
            continue
        filename = secure_filename(uploaded.filename)
        data = uploaded.read()
        if not data:
            continue
        store.attach_file_to_row(
            context["billing_sheet_id"],
            context["billing_row_id"],
            filename=filename,
            mime_type=uploaded.mimetype or "application/octet-stream",
            data=data,
        )
        added += 1

    flash(f"{added} billing document(s) uploaded.", "success")
    return redirect(url_for("billing_detail", project_id=project_id))



def _resolve_attachment(sheet_id: int, row_id: int, filename: str) -> dict[str, Any] | None:
    attachments = store.list_row_attachments(sheet_id, row_id)
    return next(
        (
            item
            for item in attachments
            if str(item.get("name", "")).strip() == filename.strip()
        ),
        None,
    )


def _plain_email_preview(data: bytes) -> str:
    message = BytesParser(policy=policy.default).parsebytes(data)
    parts = [
        f"From: {message.get('From', '')}",
        f"To: {message.get('To', '')}",
        f"Date: {message.get('Date', '')}",
        f"Subject: {message.get('Subject', '')}",
        "",
    ]
    body = message.get_body(preferencelist=("plain",))
    if body:
        try:
            parts.append(body.get_content())
        except Exception:
            parts.append("")
    return "\n".join(parts)


@app.route("/attachments/preview/<int:sheet_id>/<int:row_id>/<path:filename>")
@require_login
def attachment_preview(sheet_id: int, row_id: int, filename: str):
    match = _resolve_attachment(sheet_id, row_id, filename)
    if not match:
        flash("That file is no longer attached to this job. Refresh the page.", "error")
        return redirect(request.referrer or url_for("office_work_orders"))

    try:
        data, current_name, mime = store.download_attachment(
            sheet_id,
            int(match["id"]),
        )
    except SmartsheetError:
        flash("Smartsheet could not open that file. Refresh the page and try again.", "error")
        return redirect(request.referrer or url_for("office_work_orders"))

    lower = current_name.lower()
    guessed = mime or mimetypes.guess_type(current_name)[0] or "application/octet-stream"

    if guessed.startswith("image/"):
        return send_file(
            BytesIO(data),
            mimetype=guessed,
            as_attachment=False,
            download_name=current_name,
        )

    if guessed == "application/pdf" or lower.endswith(".pdf"):
        return send_file(
            BytesIO(data),
            mimetype="application/pdf",
            as_attachment=False,
            download_name=current_name,
        )

    if lower.endswith(".zip"):
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                entries = [
                    {
                        "name": info.filename,
                        "size": info.file_size,
                    }
                    for info in archive.infolist()
                    if not info.is_dir()
                ]
        except Exception:
            entries = []
        return render_template(
            "attachment_preview.html",
            filename=current_name,
            preview_type="zip",
            entries=entries,
            text_preview="",
            download_url=url_for(
                "attachment_download",
                sheet_id=sheet_id,
                row_id=row_id,
                filename=filename,
            ),
        )

    if lower.endswith(".eml") or guessed == "message/rfc822":
        try:
            text_preview = _plain_email_preview(data)
        except Exception:
            text_preview = "Email preview could not be generated."
        return render_template(
            "attachment_preview.html",
            filename=current_name,
            preview_type="text",
            entries=[],
            text_preview=text_preview,
            download_url=url_for(
                "attachment_download",
                sheet_id=sheet_id,
                row_id=row_id,
                filename=filename,
            ),
        )

    if guessed.startswith("text/") or lower.endswith((".txt", ".csv", ".log")):
        text_preview = data.decode("utf-8", errors="replace")[:100000]
        return render_template(
            "attachment_preview.html",
            filename=current_name,
            preview_type="text",
            entries=[],
            text_preview=text_preview,
            download_url=url_for(
                "attachment_download",
                sheet_id=sheet_id,
                row_id=row_id,
                filename=filename,
            ),
        )

    return render_template(
        "attachment_preview.html",
        filename=current_name,
        preview_type="unsupported",
        entries=[],
        text_preview="This file type cannot be previewed in the browser.",
        download_url=url_for(
            "attachment_download",
            sheet_id=sheet_id,
            row_id=row_id,
            filename=filename,
        ),
    )


@app.route("/attachments/<int:sheet_id>/<int:row_id>/<path:filename>")
@require_login
def attachment_download(sheet_id: int, row_id: int, filename: str):
    # Resolve the attachment fresh from the row every time. Smartsheet
    # attachment IDs can become stale after a row/file is replaced or copied.
    match = _resolve_attachment(sheet_id, row_id, filename)
    if not match:
        flash("That file is no longer attached to this job. Refresh the page.", "error")
        return redirect(request.referrer or url_for("office_work_orders"))

    try:
        data, current_name, mime = store.download_attachment(sheet_id, int(match["id"]))
    except SmartsheetError:
        flash("Smartsheet could not open that file. Refresh the page and try again.", "error")
        return redirect(request.referrer or url_for("office_work_orders"))

    return send_file(
        BytesIO(data),
        mimetype=mime,
        as_attachment=True,
        download_name=current_name,
    )


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
@roles("admin")
def admin_build_mobile_field_sheets():
    result = sync_mobile_field_sheets()
    if result.get("ok"):
        flash(result.get("message", "Mobile field sheets are ready."), "success")
        log_action("Build Mobile Field Sheets", "workspace", str(WORKSPACE_ID), str(result))
    else:
        flash(result.get("message", "Unable to build mobile field sheets."), "error")
    return redirect(url_for("dashboard"))


@app.route("/admin/build-field-views", methods=["POST"])
@roles("admin")
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
    mobile_result = sync_mobile_field_sheets()
    billing_result = sync_billing_queue()
    mail_result = import_graycliff_mailbox() if graph_configured() else {
        "ok": False,
        "message": "Mailbox not configured.",
        "created": 0,
        "updated": 0,
    }
    if tech_result.get("ok") and mobile_result.get("ok"):
        flash(
            f"{tech_result.get('message')} {mobile_result.get('message')} "
            f"Billing queue added {billing_result.get('created', 0)} record(s). "
            f"{mail_result.get('message', '')}",
            "success",
        )
    else:
        flash(
            tech_result.get("message") or mobile_result.get("message") or "Sync failed.",
            "error",
        )
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
