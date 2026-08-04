from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import zipfile
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from typing import Any

import requests
from flask import Flask, flash, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from smartsheet_store import BILLING_STATUSES, MARKETS, PAYMENT_STATUSES, PRIORITIES, STATUSES, SmartsheetError, store

APP_NAME = "First Digital Graycliff Portal"
DB_PATH = Path(os.environ.get("DATA_PATH", "/tmp/graycliff.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
FILE_PATH = Path(os.environ.get("FILE_PATH", "/tmp/graycliff-files"))
FILE_PATH.mkdir(parents=True, exist_ok=True)
# Support the flat GitHub layout currently used by this repository.
# Templates live beside app.py, and only app.css is exposed as a static asset.
app = Flask(__name__, template_folder=str(Path(__file__).resolve().parent), static_folder=None)

@app.route("/static/app.css")
def app_css():
    return send_file(Path(__file__).resolve().parent / "app.css", mimetype="text/css")

app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-change-me")
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "ChangeMeNow!")

FIELD_MAP = {
    "project_id": "Project ID", "market": "Market", "job_type": "Job Type", "task_name": "Task Name", "address": "Address",
    "city": "City", "crq": "CRQ Number", "daily_no": "Daily No", "due_date": "Due Date",
    "status": "Status", "assigned_to": "Assigned Technician", "priority": "Priority",
    "date_received": "Date Received", "date_assigned": "Date Assigned", "date_started": "Date Started",
    "date_field_completed": "Date Field Completed", "work_performed": "Work Performed",
    "manager_notes": "Manager Notes", "customer_notes": "Customer Notes", "billing_status": "Billing Status",
    "zoho_invoice_id": "Zoho Invoice ID", "invoice_number": "Invoice Number", "invoice_date": "Invoice Date",
    "invoice_amount": "Invoice Amount", "payment_status": "Payment Status", "payment_date": "Payment Date",
    "payment_number": "Payment Number", "amount_paid": "Amount Paid", "balance": "Balance",
    "billing_fingerprint": "Billing Fingerprint", "billing_package_path": "Billing Package Path", "archived": "Archived",
}
REVERSE_FIELD_MAP = {v: k for k, v in FIELD_MAP.items()}


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            email TEXT UNIQUE,
            display_name TEXT,
            role TEXT,
            password_hash TEXT,
            markets TEXT,
            is_active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS file_categories(
            attachment_id INTEGER PRIMARY KEY,
            project_id TEXT,
            category TEXT,
            original_filename TEXT,
            uploaded_by TEXT,
            uploaded_at TEXT
        );
        CREATE TABLE IF NOT EXISTS payment_matches(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_number TEXT,
            daily_no TEXT,
            source_date TEXT,
            amount REAL,
            fingerprint TEXT,
            project_id TEXT,
            method TEXT,
            confidence TEXT,
            status TEXT DEFAULT 'Pending',
            notes TEXT
        );
        """
    )
    existing = connection.execute("SELECT * FROM users WHERE lower(email)=lower(?)", ("admin@firstdigitalsc.com",)).fetchone()
    password_hash = generate_password_hash(ADMIN_PASSWORD)
    if not existing:
        connection.execute(
            "INSERT INTO users(email,display_name,role,password_hash,markets) VALUES(?,?,?,?,?)",
            ("admin@firstdigitalsc.com", "Administrator", "Admin", password_hash, "Florence,Columbia"),
        )
    elif ADMIN_PASSWORD != "ChangeMeNow!" and not check_password_hash(existing["password_hash"], ADMIN_PASSWORD):
        # Keep the Render ADMIN_PASSWORD authoritative for the original admin account.
        connection.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, existing["id"]))
    connection.commit()
    return connection


def require_login(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login", next=request.path))
        return function(*args, **kwargs)
    return wrapped


def manager_required(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if session.get("role") not in ["Admin", "Manager", "Billing"]:
            flash("Manager access required.", "error")
            return redirect(url_for("dashboard"))
        return function(*args, **kwargs)
    return wrapped


def admin_required(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if session.get("role") != "Admin":
            flash("Administrator access required.", "error")
            return redirect(url_for("dashboard"))
        return function(*args, **kwargs)
    return wrapped


def master_sheet_id() -> int:
    return int(store.config()["master_sheet_id"])


def normalize_job(record: dict[str, Any]) -> dict[str, Any]:
    job: dict[str, Any] = {"row_id": record.get("row_id"), "id": record.get("row_id")}
    for smartsheet_title, field_name in REVERSE_FIELD_MAP.items():
        value = record.get(smartsheet_title, "")
        if field_name in {"invoice_amount", "amount_paid", "balance"}:
            try:
                value = float(value or 0)
            except (TypeError, ValueError):
                value = 0.0
        job[field_name] = value
    return job


def job_values_from_form(form, *, include_identity: bool = False) -> dict[str, Any]:
    fields = {
        "Market": form.get("market", ""), "Job Type": form.get("job_type", "Standard"), "Task Name": form.get("task_name", ""),
        "Address": form.get("address", ""), "City": form.get("city", ""),
        "CRQ Number": form.get("crq", "") if form.get("job_type", "Standard") == "Night Cut" else "",
        "Due Date": form.get("due_date", ""), "Status": form.get("status", ""),
        "Assigned Technician": form.get("assigned_to", ""), "Priority": form.get("priority", "Normal"),
        "Work Performed": form.get("work_performed", ""), "Manager Notes": form.get("manager_notes", ""),
        "Customer Notes": form.get("customer_notes", ""), "Billing Status": form.get("billing_status", "Not Ready"),
        "Invoice Number": form.get("invoice_number", ""), "Invoice Date": form.get("invoice_date", ""),
        "Invoice Amount": form.get("invoice_amount", ""), "Payment Status": form.get("payment_status", "Unpaid"),
    }
    if not include_identity:
        fields.pop("Market", None)
    return fields


def get_job(project_id: str, *, attachments: bool = False, force: bool = False) -> dict[str, Any] | None:
    record = store.find_record(master_sheet_id(), "Project ID", project_id, include_attachments=attachments, force=force)
    return normalize_job(record) if record else None


def user_can_view_market(job: dict[str, Any]) -> bool:
    role = session.get("role")
    if role in ["Admin", "Manager", "Billing", "Technician", "Graycliff Manager"]:
        return True
    markets = [item.strip() for item in str(session.get("markets", "")).split(",") if item.strip()]
    return not markets or job.get("market") in markets


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        with db() as connection:
            user = connection.execute(
                "SELECT * FROM users WHERE lower(email)=lower(?) AND is_active=1", (request.form["email"],)
            ).fetchone()
        if user and check_password_hash(user["password_hash"], request.form["password"]):
            session.update(
                user=user["email"], display_name=user["display_name"], role=user["role"], markets=user["markets"]
            )
            next_url = request.args.get("next", "")
            if not next_url.startswith("/") or next_url.startswith("//"):
                next_url = url_for("dashboard")
            return redirect(next_url)
        flash("Invalid login.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/healthz")
def healthz():
    return {"status": "ok", "smartsheet_configured": store.enabled}, 200


@app.route("/")
@app.route("/dashboard")
@require_login
def dashboard():
    setup_status = store.status()
    if not setup_status.get("connected"):
        return render_template(
            "dashboard.html",
            counts={},
            recent=[],
            exceptions=0,
            setup_status=setup_status,
        )
    try:
        jobs = [normalize_job(record) for record in store.list_records(master_sheet_id())]
        jobs = [job for job in jobs if not job.get("archived") and user_can_view_market(job)]
        counts: dict[str, int] = {}
        for job in jobs:
            counts[str(job.get("status") or "Unassigned")] = counts.get(str(job.get("status") or "Unassigned"), 0) + 1
        recent = list(reversed(jobs[-8:]))
        with db() as connection:
            exceptions = connection.execute("SELECT COUNT(*) n FROM payment_matches WHERE status='Pending'").fetchone()["n"]
        return render_template("dashboard.html", counts=counts, recent=recent, exceptions=exceptions, setup_status=setup_status)
    except Exception as exc:
        return render_template("setup_error.html", error=str(exc), setup_status=store.status()), 503


@app.route("/admin/smartsheet-status")
@require_login
@admin_required
def smartsheet_status():
    status = store.status()
    return status, (200 if status.get("connected") else 503)


@app.route("/admin/setup-smartsheet", methods=["POST"])
@require_login
@admin_required
def setup_smartsheet():
    try:
        config = store.ensure_workspace()
        with db() as connection:
            users = connection.execute("SELECT * FROM users").fetchall()
        for user in users:
            store.sync_user(user["email"], user["display_name"], user["role"], user["markets"], bool(user["is_active"]))
        flash(f"Smartsheet workspace ready. Workspace ID: {config['workspace_id']}", "success")
    except Exception as exc:
        flash(f"Smartsheet setup failed: {exc}", "error")
    return redirect(url_for("dashboard"))


@app.route("/jobs")
@require_login
def jobs():
    q = request.args.get("q", "").strip().lower()
    market = request.args.get("market", "")
    status = request.args.get("status", "")
    mine = request.args.get("mine", "")
    records = [normalize_job(record) for record in store.list_records(master_sheet_id())]
    rows = []
    for job in records:
        if job.get("archived") or not user_can_view_market(job):
            continue
        searchable = " ".join(str(job.get(key, "")) for key in ["project_id", "task_name", "address", "crq", "city"]).lower()
        if q and q not in searchable:
            continue
        if market and job.get("market") != market:
            continue
        if status and job.get("status") != status:
            continue
        if mine and str(job.get("assigned_to", "")).lower() != str(session.get("user", "")).lower():
            continue
        rows.append(job)
    priority_rank = {"Urgent": 1, "High": 2, "Normal": 3, "Low": 4}
    rows.sort(key=lambda item: (priority_rank.get(item.get("priority"), 3), item.get("due_date") or "9999-12-31", item.get("project_id") or ""))
    return render_template("jobs.html", jobs=rows, markets=MARKETS, statuses=STATUSES)


@app.route("/jobs/new", methods=["GET", "POST"])
@require_login
@manager_required
def new_job():
    if request.method == "POST":
        market = request.form["market"]
        project_id = store.next_project_id(market)
        job_type = request.form.get("job_type", "Standard")
        if job_type == "Night Cut" and not request.form.get("crq", "").strip():
            flash("CRQ Number is required for a Night Cut.", "error")
            return render_template("job_form.html", markets=MARKETS, priorities=PRIORITIES, form=request.form), 400
        values = job_values_from_form(request.form, include_identity=True)
        values.update({
            "Project ID": project_id,
            "Status": "Unassigned",
            "Priority": request.form.get("priority", "Normal"),
            "Date Received": date.today().isoformat(),
            "Billing Status": "Not Ready",
            "Payment Status": "Unpaid",
            "Archived": False,
        })
        store.add_record(master_sheet_id(), values)
        flash(f"Created {project_id}.", "success")
        return redirect(url_for("job_detail", job_id=project_id))
    return render_template("job_form.html", markets=MARKETS, priorities=PRIORITIES)


@app.route("/jobs/<job_id>", methods=["GET", "POST"])
@require_login
def job_detail(job_id: str):
    job = get_job(job_id, force=True)
    if not job or not user_can_view_market(job):
        return "Not found", 404
    if request.method == "POST":
        action = request.form.get("action")
        updates: dict[str, Any] = {}
        if action == "claim":
            if not job.get("assigned_to"):
                updates = {
                    "Assigned Technician": session["user"], "Status": "Assigned",
                    "Date Assigned": date.today().isoformat(),
                }
        elif action == "save":
            if session.get("role") == "Technician":
                updates = {
                    "Status": request.form.get("status", job.get("status", "Assigned")),
                    "Work Performed": request.form.get("work_performed", ""),
                }
            else:
                job_type = request.form.get("job_type", job.get("job_type") or "Standard")
                if job_type == "Night Cut" and not request.form.get("crq", "").strip():
                    flash("CRQ Number is required for a Night Cut.", "error")
                    return redirect(url_for("job_detail", job_id=job_id))
                updates = job_values_from_form(request.form)
        elif action == "complete":
            updates = {
                "Status": "Field Complete", "Date Field Completed": date.today().isoformat(),
                "Billing Status": "Review", "Work Performed": request.form.get("work_performed", job.get("work_performed", "")),
            }
        if updates:
            store.update_record(master_sheet_id(), int(job["row_id"]), updates)
        return redirect(url_for("job_detail", job_id=job_id))

    attachments = store.list_row_attachments(master_sheet_id(), int(job["row_id"]))
    with db() as connection:
        categories = {
            int(row["attachment_id"]): row
            for row in connection.execute("SELECT * FROM file_categories WHERE project_id=?", (job_id,)).fetchall()
        }
        technicians = connection.execute(
            "SELECT email,display_name FROM users WHERE is_active=1 AND role='Technician' ORDER BY display_name"
        ).fetchall()
    files = []
    for attachment in attachments:
        attachment_id = int(attachment["id"])
        category_row = categories.get(attachment_id)
        category = category_row["category"] if category_row else "field"
        files.append({
            "id": attachment_id,
            "attachment_id": attachment_id,
            "filename": attachment.get("name", "Attachment"),
            "category": category,
            "uploaded_at": attachment.get("createdAt", ""),
        })
    return render_template(
        "job_detail.html", job=job, files=files, statuses=STATUSES, priorities=PRIORITIES,
        billing_statuses=BILLING_STATUSES, payment_statuses=PAYMENT_STATUSES, technicians=technicians,
    )


@app.route("/jobs/<job_id>/upload", methods=["POST"])
@require_login
def upload(job_id: str):
    category = request.form.get("category", "field")
    if category == "billing" and session.get("role") == "Technician":
        return "Forbidden", 403
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        flash("Choose a file.", "error")
        return redirect(url_for("job_detail", job_id=job_id))
    job = get_job(job_id, force=True)
    if not job:
        return "Not found", 404
    filename = secure_filename(uploaded.filename) or "attachment"
    temp_folder = FILE_PATH / "temp"
    temp_folder.mkdir(parents=True, exist_ok=True)
    temp_path = temp_folder / f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}-{filename}"
    uploaded.save(temp_path)
    try:
        attachment = store.add_attachment(master_sheet_id(), int(job["row_id"]), temp_path, filename)
        attachment_id = int(attachment["id"])
        with db() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO file_categories(attachment_id,project_id,category,original_filename,uploaded_by,uploaded_at) VALUES(?,?,?,?,?,?)",
                (attachment_id, job_id, category, filename, session["user"], datetime.now().isoformat(timespec="seconds")),
            )
            connection.commit()
        flash(f"Uploaded {filename}.", "success")
    finally:
        temp_path.unlink(missing_ok=True)
    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/files/<int:file_id>")
@require_login
def download_file(file_id: int):
    with db() as connection:
        category = connection.execute("SELECT * FROM file_categories WHERE attachment_id=?", (file_id,)).fetchone()
    if category and category["category"] == "billing" and session.get("role") == "Technician":
        return "Forbidden", 403
    info = store.attachment_download_info(master_sheet_id(), file_id)
    url = info.get("url")
    if not url:
        return "Attachment download unavailable", 404
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    suffix = Path(info.get("name", "attachment")).suffix
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    temp.write(response.content)
    temp.close()
    return send_file(temp.name, as_attachment=True, download_name=info.get("name", "attachment"))


@app.route("/jobs/<job_id>/billing-package")
@require_login
@manager_required
def billing_package(job_id: str):
    job = get_job(job_id, force=True)
    if not job:
        return "Not found", 404
    attachments = store.list_row_attachments(master_sheet_id(), int(job["row_id"]))
    with db() as connection:
        billing_ids = {
            int(row["attachment_id"])
            for row in connection.execute(
                "SELECT attachment_id FROM file_categories WHERE project_id=? AND category='billing'", (job_id,)
            ).fetchall()
        }
    output_folder = FILE_PATH / job_id
    output_folder.mkdir(parents=True, exist_ok=True)
    output = output_folder / f"{job_id}-Billing-Package.zip"
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for attachment in attachments:
            attachment_id = int(attachment["id"])
            if attachment_id not in billing_ids:
                continue
            info = store.attachment_download_info(master_sheet_id(), attachment_id)
            if not info.get("url"):
                continue
            response = requests.get(info["url"], timeout=120)
            response.raise_for_status()
            archive.writestr(info.get("name", f"attachment-{attachment_id}"), response.content)
    store.update_record(master_sheet_id(), int(job["row_id"]), {"Billing Package Path": output.name})
    return send_file(output, as_attachment=True, download_name=output.name)


@app.route("/payments/review")
@require_login
@manager_required
def payment_review():
    with db() as connection:
        matches = connection.execute(
            "SELECT * FROM payment_matches WHERE status='Pending' ORDER BY id DESC"
        ).fetchall()
    return render_template("payment_review.html", matches=matches)


@app.route("/users", methods=["GET", "POST"])
@require_login
@admin_required
def users():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        display_name = request.form.get("display_name", "").strip()
        role = request.form.get("role", "Technician")
        markets = ",".join(request.form.getlist("markets"))
        password = request.form.get("password", "")
        if not email or not password:
            flash("Email and password are required.", "error")
        else:
            with db() as connection:
                connection.execute(
                    "INSERT INTO users(email,display_name,role,password_hash,markets,is_active) VALUES(?,?,?,?,?,1) "
                    "ON CONFLICT(email) DO UPDATE SET display_name=excluded.display_name,role=excluded.role,password_hash=excluded.password_hash,markets=excluded.markets,is_active=1",
                    (email, display_name or email, role, generate_password_hash(password), markets),
                )
                connection.commit()
            try:
                store.sync_user(email, display_name or email, role, markets, True)
            except Exception as exc:
                flash(f"User saved locally, but Smartsheet user directory sync failed: {exc}", "error")
            else:
                flash(f"Saved {email}.", "success")
        return redirect(url_for("users"))
    with db() as connection:
        rows = connection.execute("SELECT * FROM users ORDER BY display_name").fetchall()
    return render_template("users.html", users=rows, roles=["Admin", "Manager", "Billing", "Technician", "Graycliff Manager", "Graycliff Area User"], markets=MARKETS)


@app.route("/large-projects")
@require_login
def large_projects():
    return render_template("large_projects.html")


@app.context_processor
def context():
    return {
        "app_name": APP_NAME, "role": session.get("role"), "display_name": session.get("display_name"),
        "smartsheet_status": store.status() if session.get("user") else None,
    }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
