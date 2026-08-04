#!/usr/bin/env python3
import json, os, sys, requests
from graycliff_schema import SHEET_DEFINITIONS

BASE = "https://api.smartsheet.com/2.0"
TOKEN = os.environ.get("SMARTSHEET_ACCESS_TOKEN", "").strip()
WORKSPACE_NAME = os.environ.get("GRAYCLIFF_WORKSPACE_NAME", "Graycliff Portal")

if not TOKEN:
    raise SystemExit("Set SMARTSHEET_ACCESS_TOKEN first.")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

def req(method, path, **kwargs):
    r = requests.request(method, BASE + path, headers=HEADERS, timeout=60, **kwargs)
    if not r.ok:
        raise RuntimeError(f"{method} {path}: {r.status_code} {r.text}")
    return r.json() if r.content else {}

def column(title, ctype, primary=False):
    c = {"title": title, "type": ctype, "primary": primary}
    options = {
      "Market": ["Florence", "Columbia"],
      "Status": ["New", "Unassigned", "Assigned", "In Progress", "Field Complete", "Billing Review", "Missing Documents", "Ready to Bill", "Billed", "Paid", "Closed"],
      "Priority": ["Low", "Normal", "High", "Urgent"],
      "Billing Status": ["Not Ready", "Review", "Missing Documents", "Ready to Bill", "Invoiced", "Sent"],
      "Payment Status": ["Unpaid", "Partially Paid", "Paid", "Retention Outstanding", "Exception"],
      "Role": ["Admin", "Manager", "Billing", "Technician", "Graycliff Manager", "Graycliff Area User"],
      "Match Method": ["Automatic", "Date Tie-Breaker", "Manual"],
    }
    if ctype == "PICKLIST" and title in options: c["options"] = options[title]
    return c

def main():
    workspaces = req("GET", "/workspaces").get("data", [])
    existing = next((w for w in workspaces if w.get("name") == WORKSPACE_NAME), None)
    if existing:
        workspace_id = existing["id"]
        print(f"Using existing workspace {WORKSPACE_NAME}: {workspace_id}")
    else:
        workspace_id = req("POST", "/workspaces", json={"name": WORKSPACE_NAME})["result"]["id"]
        print(f"Created workspace {WORKSPACE_NAME}: {workspace_id}")

    ws = req("GET", f"/workspaces/{workspace_id}")
    existing_sheets = {s["name"]: s["id"] for s in ws.get("sheets", [])}
    ids = {"workspace_id": workspace_id}
    for name, cols in SHEET_DEFINITIONS.items():
        if name in existing_sheets:
            sid = existing_sheets[name]
            print(f"Exists: {name} ({sid})")
        else:
            payload = {"name": name, "columns": [column(*c) for c in cols]}
            sid = req("POST", f"/workspaces/{workspace_id}/sheets", json=payload)["result"]["id"]
            print(f"Created: {name} ({sid})")
        key = name.lower().replace("graycliff ", "").replace(" - ", "_").replace(" ", "_").replace("projects", "sheet").replace("configuration", "config")
        ids[key + "_id"] = sid

    with open("graycliff_smartsheet_ids.json", "w", encoding="utf-8") as f:
        json.dump(ids, f, indent=2)
    print("\nSaved graycliff_smartsheet_ids.json")
    print(json.dumps(ids, indent=2))

if __name__ == "__main__": main()
