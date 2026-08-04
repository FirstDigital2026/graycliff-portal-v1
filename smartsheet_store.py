from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

BASE = "https://api.smartsheet.com/2.0"
TOKEN = os.environ.get("SMARTSHEET_ACCESS_TOKEN", "").strip()
WORKSPACE_NAME = os.environ.get("GRAYCLIFF_WORKSPACE_NAME", "Graycliff Portal")
CONFIG_PATH = Path(os.environ.get("SMARTSHEET_CONFIG_PATH", "/var/data/graycliff_smartsheet_ids.json"))
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

MASTER_SHEET = "Graycliff Small Projects - Master"
BILLING_SHEET = "Graycliff Billing Batches"
PAYMENTS_SHEET = "Graycliff Payments"
MATCHES_SHEET = "Graycliff Payment Matches"
USERS_SHEET = "Graycliff Users"
CONFIG_SHEET = "Graycliff Configuration"

MARKETS = ["Florence", "Columbia"]
STATUSES = ["New", "Unassigned", "Assigned", "In Progress", "Field Complete", "Billing Review", "Missing Documents", "Ready to Bill", "Billed", "Paid", "Closed"]
PRIORITIES = ["Low", "Normal", "High", "Urgent"]
BILLING_STATUSES = ["Not Ready", "Review", "Missing Documents", "Ready to Bill", "Invoiced", "Sent"]
PAYMENT_STATUSES = ["Unpaid", "Partially Paid", "Paid", "Retention Outstanding", "Exception"]
ROLES = ["Admin", "Manager", "Billing", "Technician", "Graycliff Manager", "Graycliff Area User"]
MATCH_METHODS = ["Automatic", "Date Tie-Breaker", "Manual"]

MASTER_COLUMNS = [
    ("Project ID", "TEXT_NUMBER", True, None),
    ("Market", "PICKLIST", False, MARKETS),
    ("Job Type", "PICKLIST", False, ["Standard", "Night Cut"]),
    ("Task Name", "TEXT_NUMBER", False, None),
    ("Address", "TEXT_NUMBER", False, None),
    ("City", "TEXT_NUMBER", False, None),
    ("CRQ Number", "TEXT_NUMBER", False, None),
    ("Daily No", "TEXT_NUMBER", False, None),
    ("Due Date", "DATE", False, None),
    ("Status", "PICKLIST", False, STATUSES),
    ("Assigned Technician", "TEXT_NUMBER", False, None),
    ("Priority", "PICKLIST", False, PRIORITIES),
    ("Date Received", "DATE", False, None),
    ("Date Assigned", "DATE", False, None),
    ("Date Started", "DATE", False, None),
    ("Date Field Completed", "DATE", False, None),
    ("Work Performed", "TEXT_NUMBER", False, None),
    ("Manager Notes", "TEXT_NUMBER", False, None),
    ("Customer Notes", "TEXT_NUMBER", False, None),
    ("Billing Status", "PICKLIST", False, BILLING_STATUSES),
    ("Zoho Invoice ID", "TEXT_NUMBER", False, None),
    ("Invoice Number", "TEXT_NUMBER", False, None),
    ("Invoice Date", "DATE", False, None),
    ("Invoice Amount", "TEXT_NUMBER", False, None),
    ("Payment Status", "PICKLIST", False, PAYMENT_STATUSES),
    ("Payment Date", "DATE", False, None),
    ("Payment Number", "TEXT_NUMBER", False, None),
    ("Amount Paid", "TEXT_NUMBER", False, None),
    ("Balance", "TEXT_NUMBER", False, None),
    ("Billing Fingerprint", "TEXT_NUMBER", False, None),
    ("Billing Package Path", "TEXT_NUMBER", False, None),
    ("Archived", "CHECKBOX", False, None),
]

SHEET_DEFINITIONS = {
    MASTER_SHEET: MASTER_COLUMNS,
    BILLING_SHEET: [
        ("Batch ID", "TEXT_NUMBER", True, None), ("Market", "PICKLIST", False, MARKETS),
        ("Invoice Number", "TEXT_NUMBER", False, None), ("Zoho Invoice ID", "TEXT_NUMBER", False, None),
        ("Invoice Date", "DATE", False, None), ("Total", "TEXT_NUMBER", False, None),
        ("Status", "TEXT_NUMBER", False, None), ("Job Count", "TEXT_NUMBER", False, None),
        ("ZIP Path", "TEXT_NUMBER", False, None), ("Created By", "TEXT_NUMBER", False, None),
    ],
    PAYMENTS_SHEET: [
        ("Payment Number", "TEXT_NUMBER", True, None), ("Payment Date", "DATE", False, None),
        ("Payment Total", "TEXT_NUMBER", False, None), ("Invoice Number", "TEXT_NUMBER", False, None),
        ("Amount Applied", "TEXT_NUMBER", False, None), ("Unapplied Amount", "TEXT_NUMBER", False, None),
        ("Remittance File", "TEXT_NUMBER", False, None), ("Status", "TEXT_NUMBER", False, None),
    ],
    MATCHES_SHEET: [
        ("Match ID", "TEXT_NUMBER", True, None), ("Payment Number", "TEXT_NUMBER", False, None),
        ("Daily No", "TEXT_NUMBER", False, None), ("Project ID", "TEXT_NUMBER", False, None),
        ("Quantity Fingerprint", "TEXT_NUMBER", False, None), ("Amount", "TEXT_NUMBER", False, None),
        ("Source Date", "DATE", False, None), ("Match Method", "PICKLIST", False, MATCH_METHODS),
        ("Confidence", "TEXT_NUMBER", False, None), ("Reviewed By", "TEXT_NUMBER", False, None),
        ("Reviewed At", "DATE", False, None), ("Notes", "TEXT_NUMBER", False, None),
        ("Status", "TEXT_NUMBER", False, None),
    ],
    USERS_SHEET: [
        ("Email", "TEXT_NUMBER", True, None), ("Display Name", "TEXT_NUMBER", False, None),
        ("Role", "PICKLIST", False, ROLES), ("Markets", "TEXT_NUMBER", False, None),
        ("Active", "CHECKBOX", False, None),
    ],
    CONFIG_SHEET: [
        ("Key", "TEXT_NUMBER", True, None), ("Value", "TEXT_NUMBER", False, None),
        ("Description", "TEXT_NUMBER", False, None),
    ],
}

class SmartsheetError(RuntimeError):
    pass

class SmartsheetStore:
    def __init__(self) -> None:
        self.token = TOKEN
        self._lock = threading.RLock()
        self._config: dict[str, Any] = {}
        self._sheet_cache: dict[int, tuple[float, dict[str, Any]]] = {}
        if CONFIG_PATH.exists():
            try:
                self._config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                self._config = {}

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def headers(self, json_content: bool = True) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.token}",
            "Smartsheet-Integration-Source": "APPLICATION,First Digital,Graycliff Portal",
        }
        if json_content:
            h["Content-Type"] = "application/json"
        return h

    def request(self, method: str, path: str, *, timeout: int = 60, **kwargs: Any) -> Any:
        if not self.token:
            raise SmartsheetError("SMARTSHEET_ACCESS_TOKEN is not configured.")
        headers = kwargs.pop("headers", self.headers())
        response = requests.request(method, BASE + path, headers=headers, timeout=timeout, **kwargs)
        if not response.ok:
            raise SmartsheetError(f"Smartsheet {method} {path} failed ({response.status_code}): {response.text[:1000]}")
        if not response.content:
            return {}
        return response.json()

    def save_config(self) -> None:
        CONFIG_PATH.write_text(json.dumps(self._config, indent=2), encoding="utf-8")

    @staticmethod
    def _column_payload(title: str, ctype: str, primary: bool, options: list[str] | None) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title, "type": ctype}
        if primary:
            payload["primary"] = True
        if options:
            payload["options"] = options
        return payload

    def _ensure_sheet_columns(self, sheet_id: int, definitions: list[tuple[str, str, bool, list[str] | None]]) -> None:
        sheet = self.get_sheet(sheet_id, force=True)
        existing = {column["title"] for column in sheet.get("columns", [])}
        missing = [column for column in definitions if column[0] not in existing and not column[2]]
        if not missing:
            return
        payload = [self._column_payload(*column) for column in missing]
        self.request("POST", f"/sheets/{sheet_id}/columns", json=payload)
        self._sheet_cache.pop(int(sheet_id), None)

    def ensure_workspace(self) -> dict[str, Any]:
        with self._lock:
            if self._config.get("workspace_id") and self._config.get("master_sheet_id"):
                # Keep existing installations current as the portal schema evolves.
                for name, key in [
                    (MASTER_SHEET, "master_sheet_id"),
                    (BILLING_SHEET, "billing_batches_sheet_id"),
                    (PAYMENTS_SHEET, "payments_sheet_id"),
                    (MATCHES_SHEET, "payment_matches_sheet_id"),
                    (USERS_SHEET, "users_sheet_id"),
                    (CONFIG_SHEET, "configuration_sheet_id"),
                ]:
                    sheet_id = self._config.get(key)
                    if sheet_id:
                        self._ensure_sheet_columns(int(sheet_id), SHEET_DEFINITIONS[name])
                return self._config

            all_workspaces: list[dict[str, Any]] = []
            page = 1
            while True:
                result = self.request("GET", f"/workspaces?pageSize=100&page={page}")
                data = result.get("data", [])
                all_workspaces.extend(data)
                total_pages = result.get("totalPages", 1)
                if not data or (isinstance(total_pages, int) and total_pages > 0 and page >= total_pages):
                    break
                page += 1
            workspace = next((w for w in all_workspaces if w.get("name") == WORKSPACE_NAME), None)
            if workspace is None:
                workspace = self.request("POST", "/workspaces", json={"name": WORKSPACE_NAME})["result"]
            workspace_id = int(workspace["id"])

            ws_detail = self.request("GET", f"/workspaces/{workspace_id}")
            existing = {sheet["name"]: int(sheet["id"]) for sheet in ws_detail.get("sheets", [])}
            ids: dict[str, int] = {}
            for sheet_name, columns in SHEET_DEFINITIONS.items():
                sheet_id = existing.get(sheet_name)
                if not sheet_id:
                    payload = {
                        "name": sheet_name,
                        "columns": [self._column_payload(*column) for column in columns],
                    }
                    result = self.request("POST", f"/workspaces/{workspace_id}/sheets", json=payload)
                    sheet_id = int(result["result"]["id"])
                ids[sheet_name] = sheet_id
                self._ensure_sheet_columns(sheet_id, columns)

            self._config = {
                "workspace_id": workspace_id,
                "master_sheet_id": ids[MASTER_SHEET],
                "billing_batches_sheet_id": ids[BILLING_SHEET],
                "payments_sheet_id": ids[PAYMENTS_SHEET],
                "payment_matches_sheet_id": ids[MATCHES_SHEET],
                "users_sheet_id": ids[USERS_SHEET],
                "configuration_sheet_id": ids[CONFIG_SHEET],
                "workspace_name": WORKSPACE_NAME,
            }
            self.save_config()
            return self._config

    def config(self) -> dict[str, Any]:
        return self.ensure_workspace()

    def get_sheet(self, sheet_id: int, *, include_attachments: bool = False, force: bool = False) -> dict[str, Any]:
        cache_key = int(sheet_id)
        cached = self._sheet_cache.get(cache_key)
        if not force and cached and time.time() - cached[0] < 8:
            return cached[1]
        include = "?include=attachments" if include_attachments else ""
        sheet = self.request("GET", f"/sheets/{sheet_id}{include}")
        self._sheet_cache[cache_key] = (time.time(), sheet)
        return sheet

    @staticmethod
    def column_map(sheet: dict[str, Any]) -> dict[str, int]:
        return {column["title"]: int(column["id"]) for column in sheet.get("columns", [])}

    @staticmethod
    def _display_value(cell: dict[str, Any]) -> Any:
        if "value" in cell:
            return cell.get("value")
        return cell.get("displayValue", "")

    def row_to_record(self, row: dict[str, Any], columns_by_id: dict[int, str]) -> dict[str, Any]:
        record: dict[str, Any] = {"row_id": int(row["id"]), "id": int(row["id"])}
        for cell in row.get("cells", []):
            title = columns_by_id.get(int(cell["columnId"]))
            if title:
                record[title] = self._display_value(cell)
        record["attachments"] = row.get("attachments", [])
        return record

    def list_records(self, sheet_id: int, *, include_attachments: bool = False, force: bool = False) -> list[dict[str, Any]]:
        sheet = self.get_sheet(sheet_id, include_attachments=include_attachments, force=force)
        columns_by_id = {int(column["id"]): column["title"] for column in sheet.get("columns", [])}
        return [self.row_to_record(row, columns_by_id) for row in sheet.get("rows", [])]

    def find_record(self, sheet_id: int, primary_title: str, value: str, *, include_attachments: bool = False, force: bool = False) -> dict[str, Any] | None:
        for record in self.list_records(sheet_id, include_attachments=include_attachments, force=force):
            if str(record.get(primary_title, "")).strip().lower() == str(value).strip().lower():
                return record
        return None

    def _cells(self, sheet_id: int, values: dict[str, Any]) -> list[dict[str, Any]]:
        sheet = self.get_sheet(sheet_id)
        columns = self.column_map(sheet)
        cells: list[dict[str, Any]] = []
        for title, value in values.items():
            column_id = columns.get(title)
            if not column_id:
                continue
            if value is None:
                value = ""
            cells.append({"columnId": column_id, "value": value, "strict": False})
        return cells

    def add_record(self, sheet_id: int, values: dict[str, Any]) -> dict[str, Any]:
        payload = [{"toBottom": True, "cells": self._cells(sheet_id, values)}]
        result = self.request("POST", f"/sheets/{sheet_id}/rows", json=payload)
        self._sheet_cache.pop(int(sheet_id), None)
        rows = result.get("result", [])
        return rows[0] if isinstance(rows, list) and rows else rows

    def update_record(self, sheet_id: int, row_id: int, values: dict[str, Any]) -> dict[str, Any]:
        payload = [{"id": int(row_id), "cells": self._cells(sheet_id, values)}]
        result = self.request("PUT", f"/sheets/{sheet_id}/rows", json=payload)
        self._sheet_cache.pop(int(sheet_id), None)
        rows = result.get("result", [])
        return rows[0] if isinstance(rows, list) and rows else rows

    def next_project_id(self, market: str) -> str:
        prefix = "GCF-SP-FLO" if market == "Florence" else "GCF-SP-COL"
        maximum = 0
        config = self.config()
        for record in self.list_records(config["master_sheet_id"], force=True):
            project_id = str(record.get("Project ID", ""))
            if project_id.startswith(prefix + "-"):
                try:
                    maximum = max(maximum, int(project_id.rsplit("-", 1)[1]))
                except ValueError:
                    pass
        return f"{prefix}-{maximum + 1:06d}"

    def add_attachment(self, sheet_id: int, row_id: int, path: Path, display_name: str | None = None) -> dict[str, Any]:
        filename = display_name or path.name
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        data = path.read_bytes()
        headers = self.headers(json_content=False)
        headers.update({
            "Content-Disposition": f'attachment; filename="{quote(filename)}"',
            "Content-Type": mime,
            "Content-Length": str(len(data)),
        })
        result = self.request("POST", f"/sheets/{sheet_id}/rows/{row_id}/attachments", headers=headers, data=data, timeout=180)
        self._sheet_cache.pop(int(sheet_id), None)
        return result.get("result", result)

    def list_row_attachments(self, sheet_id: int, row_id: int) -> list[dict[str, Any]]:
        result = self.request("GET", f"/sheets/{sheet_id}/rows/{row_id}/attachments?includeAll=true")
        return result.get("data", [])

    def attachment_download_info(self, sheet_id: int, attachment_id: int) -> dict[str, Any]:
        return self.request("GET", f"/sheets/{sheet_id}/attachments/{attachment_id}")

    def sync_user(self, email: str, display_name: str, role: str, markets: str, active: bool = True) -> None:
        config = self.config()
        sheet_id = config["users_sheet_id"]
        existing = self.find_record(sheet_id, "Email", email, force=True)
        values = {"Email": email, "Display Name": display_name, "Role": role, "Markets": markets, "Active": active}
        if existing:
            self.update_record(sheet_id, existing["row_id"], values)
        else:
            self.add_record(sheet_id, values)

    def status(self) -> dict[str, Any]:
        if not self.enabled:
            return {"connected": False, "error": "SMARTSHEET_ACCESS_TOKEN is not configured."}
        try:
            cfg = self.ensure_workspace()
            return {"connected": True, **cfg}
        except Exception as exc:
            return {"connected": False, "error": str(exc)}

store = SmartsheetStore()
