from __future__ import annotations
import hashlib, json, os, sqlite3, zipfile
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from flask import Flask, flash, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

APP_NAME="First Digital Graycliff Portal"
DB_PATH=Path(os.environ.get("DATA_PATH","/tmp/graycliff.db")); DB_PATH.parent.mkdir(parents=True,exist_ok=True)
FILE_PATH=Path(os.environ.get("FILE_PATH","/tmp/graycliff-files")); FILE_PATH.mkdir(parents=True,exist_ok=True)
app=Flask(__name__); app.secret_key=os.environ.get("FLASK_SECRET_KEY","dev-change-me")
ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD","ChangeMeNow!")

STATUSES=["New","Unassigned","Assigned","In Progress","Field Complete","Billing Review","Missing Documents","Ready to Bill","Billed","Paid","Closed"]
MARKETS=["Florence","Columbia"]

def db():
    c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,email TEXT UNIQUE,display_name TEXT,role TEXT,password_hash TEXT,markets TEXT,is_active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,project_id TEXT UNIQUE,market TEXT,task_name TEXT,address TEXT,city TEXT,crq TEXT,daily_no TEXT,due_date TEXT,status TEXT,assigned_to TEXT,priority TEXT,date_received TEXT,date_assigned TEXT,date_started TEXT,date_field_completed TEXT,work_performed TEXT,manager_notes TEXT,customer_notes TEXT,billing_status TEXT,invoice_number TEXT,invoice_date TEXT,invoice_amount REAL DEFAULT 0,payment_status TEXT DEFAULT 'Unpaid',payment_date TEXT,payment_number TEXT,amount_paid REAL DEFAULT 0,balance REAL DEFAULT 0,billing_fingerprint TEXT,billing_package_path TEXT,archived INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY AUTOINCREMENT,job_id INTEGER,category TEXT,filename TEXT,stored_path TEXT,uploaded_by TEXT,uploaded_at TEXT);
    CREATE TABLE IF NOT EXISTS payment_matches(id INTEGER PRIMARY KEY AUTOINCREMENT,payment_number TEXT,daily_no TEXT,source_date TEXT,amount REAL,fingerprint TEXT,job_id INTEGER,method TEXT,confidence TEXT,status TEXT DEFAULT 'Pending',notes TEXT);
    ''')
    if not c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        c.execute("INSERT INTO users(email,display_name,role,password_hash,markets) VALUES(?,?,?,?,?)",("admin@firstdigitalsc.com","Administrator","Admin",generate_password_hash(ADMIN_PASSWORD),"Florence,Columbia"))
    c.commit(); return c

def require_login(fn):
    @wraps(fn)
    def w(*a,**k):
        if not session.get("user"): return redirect(url_for("login",next=request.path))
        return fn(*a,**k)
    return w

def manager_required(fn):
    @wraps(fn)
    def w(*a,**k):
        if session.get("role") not in ["Admin","Manager","Billing"]: flash("Manager access required.","error"); return redirect(url_for("dashboard"))
        return fn(*a,**k)
    return w

def next_project_id(market):
    prefix="GCF-SP-"+("FLO" if market=="Florence" else "COL")
    with db() as c: n=c.execute("SELECT COUNT(*) n FROM jobs WHERE project_id LIKE ?",(prefix+"%",)).fetchone()["n"]+1
    return f"{prefix}-{n:06d}"

@app.route("/login",methods=["GET","POST"])
def login():
    if request.method=="POST":
        with db() as c: u=c.execute("SELECT * FROM users WHERE lower(email)=lower(?) AND is_active=1",(request.form["email"],)).fetchone()
        if u and check_password_hash(u["password_hash"],request.form["password"]):
            session.update(user=u["email"],display_name=u["display_name"],role=u["role"],markets=u["markets"])
            next_url=request.args.get("next","")
            if not next_url.startswith("/") or next_url.startswith("//"):
                next_url=url_for("dashboard")
            return redirect(next_url)
        flash("Invalid login.","error")
    return render_template("login.html")

@app.route("/logout")
def logout(): session.clear(); return redirect(url_for("login"))

@app.route("/healthz")
def healthz(): return {"status":"ok"},200

@app.route("/")
@app.route("/dashboard")
@require_login
def dashboard():
    with db() as c:
        rows=c.execute("SELECT status,COUNT(*) n FROM jobs WHERE archived=0 GROUP BY status").fetchall(); counts={r["status"]:r["n"] for r in rows}
        recent=c.execute("SELECT * FROM jobs WHERE archived=0 ORDER BY id DESC LIMIT 8").fetchall()
        exceptions=c.execute("SELECT COUNT(*) n FROM payment_matches WHERE status='Pending'").fetchone()["n"]
    return render_template("dashboard.html",counts=counts,recent=recent,exceptions=exceptions)

@app.route("/jobs")
@require_login
def jobs():
    q=request.args.get("q","").strip(); market=request.args.get("market",""); status=request.args.get("status",""); mine=request.args.get("mine","")
    sql="SELECT * FROM jobs WHERE archived=0"; args=[]
    if q: sql+=" AND (project_id LIKE ? OR task_name LIKE ? OR address LIKE ? OR crq LIKE ?)"; args += [f"%{q}%"]*4
    if market: sql+=" AND market=?"; args.append(market)
    if status: sql+=" AND status=?"; args.append(status)
    if mine: sql+=" AND assigned_to=?"; args.append(session.get("user"))
    sql+=" ORDER BY CASE priority WHEN 'Urgent' THEN 1 WHEN 'High' THEN 2 ELSE 3 END,due_date,id DESC"
    with db() as c: rows=c.execute(sql,args).fetchall()
    return render_template("jobs.html",jobs=rows,markets=MARKETS,statuses=STATUSES)

@app.route("/jobs/new",methods=["GET","POST"])
@require_login
@manager_required
def new_job():
    if request.method=="POST":
        market=request.form["market"]; pid=next_project_id(market)
        with db() as c:
            c.execute("INSERT INTO jobs(project_id,market,task_name,address,city,crq,due_date,status,priority,date_received,billing_status,balance) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",(pid,market,request.form.get("task_name"),request.form.get("address"),request.form.get("city"),request.form.get("crq"),request.form.get("due_date"),"Unassigned",request.form.get("priority","Normal"),date.today().isoformat(),"Not Ready")); c.commit()
        return redirect(url_for("job_detail",job_id=pid))
    return render_template("job_form.html",markets=MARKETS)

@app.route("/jobs/<job_id>",methods=["GET","POST"])
@require_login
def job_detail(job_id):
    with db() as c: job=c.execute("SELECT * FROM jobs WHERE project_id=?",(job_id,)).fetchone()
    if not job: return "Not found",404
    if request.method=="POST":
        action=request.form.get("action")
        with db() as c:
            if action=="claim": c.execute("UPDATE jobs SET assigned_to=?,status='Assigned',date_assigned=? WHERE project_id=? AND (assigned_to IS NULL OR assigned_to='')",(session["user"],date.today().isoformat(),job_id))
            elif action=="save":
                c.execute("UPDATE jobs SET status=?,assigned_to=?,work_performed=?,manager_notes=?,customer_notes=?,due_date=?,priority=? WHERE project_id=?",(request.form.get("status"),request.form.get("assigned_to"),request.form.get("work_performed"),request.form.get("manager_notes"),request.form.get("customer_notes"),request.form.get("due_date"),request.form.get("priority"),job_id))
            elif action=="complete": c.execute("UPDATE jobs SET status='Field Complete',date_field_completed=?,billing_status='Review',work_performed=? WHERE project_id=?",(date.today().isoformat(),request.form.get("work_performed"),job_id))
            c.commit()
        return redirect(url_for("job_detail",job_id=job_id))
    with db() as c: fs=c.execute("SELECT * FROM files WHERE job_id=? ORDER BY id DESC",(job["id"],)).fetchall()
    return render_template("job_detail.html",job=job,files=fs,statuses=STATUSES)

@app.route("/jobs/<job_id>/upload",methods=["POST"])
@require_login
def upload(job_id):
    category=request.form.get("category","field"); f=request.files.get("file")
    if not f or not f.filename: flash("Choose a file.","error"); return redirect(url_for("job_detail",job_id=job_id))
    with db() as c: job=c.execute("SELECT * FROM jobs WHERE project_id=?",(job_id,)).fetchone()
    folder=FILE_PATH/job_id/category; folder.mkdir(parents=True,exist_ok=True); safe=Path(f.filename).name; path=folder/safe; f.save(path)
    with db() as c: c.execute("INSERT INTO files(job_id,category,filename,stored_path,uploaded_by,uploaded_at) VALUES(?,?,?,?,?,?)",(job["id"],category,safe,str(path),session["user"],datetime.now().isoformat(timespec="seconds"))); c.commit()
    return redirect(url_for("job_detail",job_id=job_id))

@app.route("/files/<int:file_id>")
@require_login
def download_file(file_id):
    with db() as c: f=c.execute("SELECT * FROM files WHERE id=?",(file_id,)).fetchone()
    if not f: return "Not found",404
    if f["category"]=="billing" and session.get("role")=="Technician": return "Forbidden",403
    return send_file(f["stored_path"],as_attachment=True,download_name=f["filename"])

@app.route("/jobs/<job_id>/billing-package")
@require_login
def billing_package(job_id):
    with db() as c:
        job=c.execute("SELECT * FROM jobs WHERE project_id=?",(job_id,)).fetchone(); fs=c.execute("SELECT * FROM files WHERE job_id=? AND category='billing'",(job["id"],)).fetchall()
    out=FILE_PATH/job_id/f"{job_id}-Billing-Package.zip"; out.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED) as z:
        for f in fs:
            p=Path(f["stored_path"])
            if p.exists(): z.write(p,arcname=p.name)
    with db() as c: c.execute("UPDATE jobs SET billing_package_path=? WHERE project_id=?",(str(out),job_id)); c.commit()
    return send_file(out,as_attachment=True,download_name=out.name)

@app.route("/payments/review")
@require_login
@manager_required
def payment_review():
    with db() as c: matches=c.execute("SELECT pm.*,j.project_id,j.task_name,j.address FROM payment_matches pm LEFT JOIN jobs j ON j.id=pm.job_id WHERE pm.status='Pending' ORDER BY pm.id DESC").fetchall()
    return render_template("payment_review.html",matches=matches)

@app.route("/large-projects")
@require_login
def large_projects(): return render_template("large_projects.html")

@app.context_processor
def ctx(): return dict(app_name=APP_NAME,role=session.get("role"),display_name=session.get("display_name"))

if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.environ.get("PORT","5000")),debug=True)
