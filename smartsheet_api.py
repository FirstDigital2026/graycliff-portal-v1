from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

BASE_URL = "https://api.smartsheet.com/2.0"
INTEGRATION_SOURCE = "APPLICATION,FirstDigital,GraycliffCloudPortal"


class SmartsheetError(RuntimeError):
    pass


@dataclass
class CacheItem:
    expires_at: float
    value: Any


class SmartsheetClient:
    def __init__(self, token: str | None = None, ttl: int = 60):
        self.token = token or os.getenv("SMARTSHEET_ACCESS_TOKEN", "")
        self.ttl = ttl
        self._cache: dict[str, CacheItem] = {}
        self._lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any | None = None,
        query: dict[str, Any] | None = None,
        retries: int = 5,
    ) -> Any:
        if not self.enabled:
            raise SmartsheetError("SMARTSHEET_ACCESS_TOKEN is not configured.")

        url = BASE_URL.rstrip("/") + "/" + path.lstrip("/")
        if query:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in query.items() if v is not None},
                doseq=True,
            )

        data = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "smartsheet-integration-source": INTEGRATION_SOURCE,
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=90) as response:
                    raw = response.read()
                    return json.loads(raw.decode("utf-8")) if raw else {}
            except urllib.error.HTTPError as exc:
                payload = exc.read().decode("utf-8", errors="replace")
                if exc.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    retry_after = exc.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else min(2 ** attempt, 12)
                    time.sleep(wait)
                    continue
                raise SmartsheetError(
                    f"{method} {path} failed ({exc.code}): {payload[:1200]}"
                ) from exc
            except urllib.error.URLError as exc:
                if attempt < retries - 1:
                    time.sleep(min(2 ** attempt, 12))
                    continue
                raise SmartsheetError(f"Unable to reach Smartsheet: {exc}") from exc
        raise SmartsheetError(f"{method} {path} failed after retries.")

    def invalidate(self, prefix: str = "") -> None:
        with self._lock:
            if not prefix:
                self._cache.clear()
            else:
                for key in list(self._cache):
                    if key.startswith(prefix):
                        self._cache.pop(key, None)

    def get_sheet(self, sheet_id: int, *, force: bool = False) -> dict[str, Any]:
        key = f"sheet:{sheet_id}"
        now = time.time()
        with self._lock:
            cached = self._cache.get(key)
            if not force and cached and cached.expires_at > now:
                return cached.value

        value = self._request(
            "GET",
            f"/sheets/{sheet_id}",
            query={"include": "attachments,discussions", "exclude": "filteredOutRows"},
        )
        with self._lock:
            self._cache[key] = CacheItem(now + self.ttl, value)
        return value

    def get_column(self, sheet_id: int, column_id: int) -> dict[str, Any]:
        return self._request("GET", f"/sheets/{sheet_id}/columns/{column_id}")

    def list_reports(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/reports", query={"pageSize": 100})
        return payload.get("data", [])

    def create_report(self, body: dict[str, Any]) -> dict[str, Any]:
        result = self._request("POST", "/reports", body=body)
        return result.get("result", result)

    def update_column(self, sheet_id: int, column_id: int, body: dict[str, Any]) -> Any:
        result = self._request("PUT", f"/sheets/{sheet_id}/columns/{column_id}", body=body)
        self.invalidate(f"sheet:{sheet_id}")
        return result

    def add_row(self, sheet_id: int, values: dict[str, Any]) -> dict[str, Any]:
        sheet = self.get_sheet(sheet_id)
        columns = {c["title"]: c["id"] for c in sheet.get("columns", [])}
        cells = [
            {"columnId": columns[title], "value": value}
            for title, value in values.items()
            if title in columns and value not in (None, "")
        ]
        result = self._request(
            "POST",
            f"/sheets/{sheet_id}/rows",
            body=[{"toBottom": True, "cells": cells}],
        )
        self.invalidate(f"sheet:{sheet_id}")
        rows = result.get("result", [])
        return rows[0] if rows else {}

    def update_row(self, sheet_id: int, row_id: int, values: dict[str, Any]) -> Any:
        sheet = self.get_sheet(sheet_id)
        columns = {c["title"]: c["id"] for c in sheet.get("columns", [])}
        cells = [
            {"columnId": columns[title], "value": value}
            for title, value in values.items()
            if title in columns
        ]
        result = self._request(
            "PUT",
            f"/sheets/{sheet_id}/rows",
            body=[{"id": row_id, "cells": cells}],
        )
        self.invalidate(f"sheet:{sheet_id}")
        return result

    def list_row_attachments(self, sheet_id: int, row_id: int) -> list[dict[str, Any]]:
        payload = self._request("GET", f"/sheets/{sheet_id}/rows/{row_id}/attachments")
        return payload.get("data", [])

    def download_attachment(self, attachment_id: int) -> tuple[bytes, str, str]:
        meta = self._request("GET", f"/attachments/{attachment_id}")
        url = meta.get("url")
        if not url:
            raise SmartsheetError("Attachment download URL was not returned.")
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
        return data, meta.get("name", "attachment"), meta.get("mimeType", "application/octet-stream")


def rows_as_records(sheet: dict[str, Any]) -> list[dict[str, Any]]:
    columns = {int(c["id"]): c["title"] for c in sheet.get("columns", [])}
    records: list[dict[str, Any]] = []
    for row in sheet.get("rows", []):
        record: dict[str, Any] = {
            "_row_id": int(row["id"]),
            "_attachments": row.get("attachments", []),
        }
        for cell in row.get("cells", []):
            title = columns.get(int(cell.get("columnId", 0)))
            if not title:
                continue
            value = cell.get("value")
            if value is None:
                value = cell.get("displayValue", "")
            record[title] = value
        records.append(record)
    return records
