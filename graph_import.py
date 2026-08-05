from __future__ import annotations

import base64
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
TOKEN_ROOT = "https://login.microsoftonline.com"

NTP_SUBJECT_RE = re.compile(
    r"PO\s+Number\s+for\s+NTP\s+(?P<work_order>\d+)\s+PRISM\s+(?P<prism>\d+)\s+Created(?:/|_|\s*)Revised",
    re.IGNORECASE,
)

ADDRESS_PATTERNS = [
    re.compile(r"(?:work\s+location|job\s+location|service\s+address|address)\s*[:\-]\s*([^\r\n<]{5,120})", re.I),
    re.compile(r"\b(\d{1,6}\s+[A-Za-z0-9.'#\- ]{3,70}\s(?:Rd|Road|St|Street|Ave|Avenue|Ln|Lane|Dr|Drive|Blvd|Boulevard|Hwy|Highway|Ct|Court|Cir|Circle|Way|Pkwy|Parkway)\b[^\r\n<]{0,60})", re.I),
]
DUE_PATTERNS = [
    re.compile(r"(?:estimated\s+completion|completion\s+date|due\s+date)\s*[:\-]\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.I),
    re.compile(r"(?:estimated\s+completion|completion\s+date|due\s+date)\s*[:\-]\s*(\d{4}-\d{2}-\d{2})", re.I),
]

FLORENCE_CITIES = {
    "florence", "darlington", "hartsville", "scranton", "lake city", "marion",
    "mullins", "dillon", "latta", "timmonsville", "pamplico", "gresham",
    "effingham", "quinby", "coward", "johnsonville", "hemingway",
}
COLUMBIA_CITIES = {
    "columbia", "lexington", "west columbia", "cayce", "irmo", "chapin",
    "blythewood", "lugoff", "elgin", "camden", "hopkins", "gaston",
    "pelion", "swansea", "newberry",
}


class GraphImportError(RuntimeError):
    pass


@dataclass
class ParsedNtp:
    work_order: str
    prism: str
    subject: str
    address: str = ""
    city: str = ""
    due_date: str = ""
    market: str = ""


def _request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    all_headers = {"Accept": "application/json"}
    if token:
        all_headers["Authorization"] = f"Bearer {token}"
    if headers:
        all_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=all_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type and raw:
                return json.loads(raw.decode("utf-8"))
            return raw
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise GraphImportError(f"Microsoft Graph request failed ({exc.code}): {payload[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise GraphImportError(f"Unable to reach Microsoft Graph: {exc}") from exc


def configured() -> bool:
    return all(
        os.getenv(name, "").strip()
        for name in ("MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET", "GRAYCLIFF_JOBS_MAILBOX")
    )


def mailbox() -> str:
    return os.getenv("GRAYCLIFF_JOBS_MAILBOX", "graycliffjobs@firstdigitalsc.com").strip().lower()


def access_token() -> str:
    tenant = os.getenv("MS_TENANT_ID", "").strip()
    client = os.getenv("MS_CLIENT_ID", "").strip()
    secret = os.getenv("MS_CLIENT_SECRET", "").strip()
    if not all((tenant, client, secret)):
        raise GraphImportError("Microsoft mailbox credentials are not configured in Render.")

    payload = urllib.parse.urlencode(
        {
            "client_id": client,
            "client_secret": secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }
    ).encode("utf-8")
    result = _request(
        "POST",
        f"{TOKEN_ROOT}/{urllib.parse.quote(tenant)}/oauth2/v2.0/token",
        body=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    token = result.get("access_token") if isinstance(result, dict) else None
    if not token:
        raise GraphImportError("Microsoft did not return an application access token.")
    return str(token)


def list_recent_messages(token: str, *, top: int = 30) -> list[dict[str, Any]]:
    user = urllib.parse.quote(mailbox())
    query = urllib.parse.urlencode(
        {
            "$top": str(top),
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,receivedDateTime,body,bodyPreview,hasAttachments,internetMessageId",
        }
    )
    result = _request("GET", f"{GRAPH_ROOT}/users/{user}/mailFolders/inbox/messages?{query}", token=token)
    return result.get("value", []) if isinstance(result, dict) else []


def list_attachments(token: str, message_id: str) -> list[dict[str, Any]]:
    user = urllib.parse.quote(mailbox())
    mid = urllib.parse.quote(message_id, safe="")
    result = _request(
        "GET",
        f"{GRAPH_ROOT}/users/{user}/messages/{mid}/attachments?$top=100",
        token=token,
    )
    return result.get("value", []) if isinstance(result, dict) else []


def get_message_mime(token: str, message_id: str) -> bytes:
    user = urllib.parse.quote(mailbox())
    mid = urllib.parse.quote(message_id, safe="")
    result = _request("GET", f"{GRAPH_ROOT}/users/{user}/messages/{mid}/$value", token=token)
    return result if isinstance(result, bytes) else b""


def _plain_text(body: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", body or "", flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", text)


def _normalize_date(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", value)
    if not match:
        return ""
    month, day, year = match.groups()
    if len(year) == 2:
        year = "20" + year
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _extract_address(text: str, subject: str) -> tuple[str, str]:
    combined = f"{subject}\n{text}"
    address = ""
    for pattern in ADDRESS_PATTERNS:
        match = pattern.search(combined)
        if match:
            address = re.sub(r"\s+", " ", match.group(1)).strip(" .,-")
            break

    city = ""
    if address:
        # Capture city from common "address, City, SC" or "address City SC" forms.
        city_match = re.search(r",\s*([A-Za-z .'-]{2,40}),?\s+SC\b", address, re.I)
        if city_match:
            city = city_match.group(1).strip()
        else:
            for candidate in sorted(FLORENCE_CITIES | COLUMBIA_CITIES, key=len, reverse=True):
                if re.search(rf"\b{re.escape(candidate)}\b", address, re.I):
                    city = candidate.title()
                    break
    return address, city


def _market_for(city: str, address: str) -> str:
    haystack = f"{city} {address}".lower()
    if any(re.search(rf"\b{re.escape(name)}\b", haystack) for name in FLORENCE_CITIES):
        return "Florence"
    if any(re.search(rf"\b{re.escape(name)}\b", haystack) for name in COLUMBIA_CITIES):
        return "Columbia"
    return ""


def parse_recognized_ntp(message: dict[str, Any]) -> ParsedNtp | None:
    subject = str(message.get("subject", "")).strip()
    match = NTP_SUBJECT_RE.search(subject)
    if not match:
        return None

    body_obj = message.get("body") or {}
    body_text = _plain_text(str(body_obj.get("content", "")))
    address, city = _extract_address(body_text, subject)

    due_date = ""
    for pattern in DUE_PATTERNS:
        due_match = pattern.search(body_text)
        if due_match:
            due_date = _normalize_date(due_match.group(1))
            break

    return ParsedNtp(
        work_order=match.group("work_order"),
        prism=match.group("prism"),
        subject=subject,
        address=address,
        city=city,
        due_date=due_date,
        market=_market_for(city, address),
    )



def list_mail_folders(token: str) -> list[dict[str, Any]]:
    user = urllib.parse.quote(mailbox())
    result = _request(
        "GET",
        f"{GRAPH_ROOT}/users/{user}/mailFolders?$top=100&includeHiddenFolders=true",
        token=token,
    )
    return result.get("value", []) if isinstance(result, dict) else []


def ensure_mail_folder(token: str, display_name: str) -> str:
    for folder in list_mail_folders(token):
        if str(folder.get("displayName", "")).strip().lower() == display_name.strip().lower():
            return str(folder.get("id"))
    user = urllib.parse.quote(mailbox())
    payload = json.dumps({"displayName": display_name}).encode("utf-8")
    result = _request(
        "POST",
        f"{GRAPH_ROOT}/users/{user}/mailFolders",
        token=token,
        body=payload,
        headers={"Content-Type": "application/json"},
    )
    folder_id = result.get("id") if isinstance(result, dict) else None
    if not folder_id:
        raise GraphImportError(f"Unable to create mailbox folder {display_name}.")
    return str(folder_id)


def move_message(token: str, message_id: str, destination_folder_id: str) -> None:
    user = urllib.parse.quote(mailbox())
    mid = urllib.parse.quote(message_id, safe="")
    payload = json.dumps({"destinationId": destination_folder_id}).encode("utf-8")
    _request(
        "POST",
        f"{GRAPH_ROOT}/users/{user}/messages/{mid}/move",
        token=token,
        body=payload,
        headers={"Content-Type": "application/json"},
    )


def attachment_bytes(item: dict[str, Any]) -> tuple[str, str, bytes] | None:
    if item.get("@odata.type") != "#microsoft.graph.fileAttachment":
        return None
    encoded = item.get("contentBytes")
    if not encoded:
        return None
    try:
        data = base64.b64decode(encoded)
    except Exception:
        return None
    return (
        str(item.get("name") or "attachment.bin"),
        str(item.get("contentType") or "application/octet-stream"),
        data,
    )
