from __future__ import annotations

import base64
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import io
import zipfile
from dataclasses import dataclass
from typing import Any

from pypdf import PdfReader

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
TOKEN_ROOT = "https://login.microsoftonline.com"

NTP_SUBJECT_RE = re.compile(
    r"PO\s+Number\s+for\s+NTP\s+(?P<work_order>\d+)\s+PRISM\s+(?P<prism>\d+)\s+Created(?:/|_|\s*)Revised",
    re.IGNORECASE,
)

ADDRESS_PATTERNS = [
    re.compile(r"(?:work\s+location|job\s+location|service\s+address|address)\s*[:\-]\s*([^\r\n<]{5,120})", re.I),
    re.compile(r"\b(\d{1,6}\s+[A-Za-z0-9.'#\- ]{3,70}\s(?:Rd|Road|St|Street|Ave|Avenue|Ln|Lane|Dr|Drive|Blvd|Boulevard|Hwy|Highway|Ct|Court|Cir|Circle|Way|Pkwy|Parkway))\b", re.I),
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
    state_zip: str = ""
    due_date: str = ""
    market: str = ""
    customer_name: str = ""


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


def list_recent_messages(token: str, *, top: int = 100) -> list[dict[str, Any]]:
    user = urllib.parse.quote(mailbox())
    query = urllib.parse.urlencode(
        {
            "$top": str(top),
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,receivedDateTime,body,bodyPreview,hasAttachments,internetMessageId,isRead,conversationId,from",
        }
    )
    result = _request(
        "GET",
        f"{GRAPH_ROOT}/users/{user}/mailFolders/inbox/messages?{query}",
        token=token,
    )
    return result.get("value", []) if isinstance(result, dict) else []



def get_message_details(token: str, message_id: str) -> dict[str, Any]:
    user = urllib.parse.quote(mailbox())
    mid = urllib.parse.quote(message_id, safe="")
    query = urllib.parse.urlencode({
        "$select": "id,subject,receivedDateTime,body,bodyPreview,hasAttachments,internetMessageId,isRead,conversationId,from,toRecipients,ccRecipients",
    })
    result = _request("GET", f"{GRAPH_ROOT}/users/{user}/messages/{mid}?{query}", token=token)
    return result if isinstance(result, dict) else {}


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



def _pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return ""


def _candidate_document_texts(attachments: list[dict[str, Any]]) -> list[tuple[str, str]]:
    docs: list[tuple[str, str]] = []
    for item in attachments:
        decoded = attachment_bytes(item)
        if not decoded:
            continue
        filename, mime_type, data = decoded
        lower = filename.lower()
        if lower.endswith(".pdf") or mime_type == "application/pdf":
            text = _pdf_text(data)
            if text:
                docs.append((filename, text))
        elif lower.endswith(".zip") or mime_type in {"application/zip", "application/x-zip-compressed"}:
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    for member in archive.namelist():
                        if member.lower().endswith(".pdf"):
                            text = _pdf_text(archive.read(member))
                            if text:
                                docs.append((member, text))
            except Exception:
                continue
    return docs


def _extract_work_order_document(
    docs: list[tuple[str, str]], expected_work_order: str
) -> dict[str, str]:
    best = None
    for filename, raw_text in docs:
        text = raw_text.replace("\r", "\n")
        compact = re.sub(r"[ \t]+", " ", text)

        wo_match = re.search(r"Work\s+Order\s*#?\s*:\s*(\d+)", compact, re.I)
        work_order = wo_match.group(1) if wo_match else ""
        if work_order and expected_work_order and work_order != expected_work_order:
            continue

        prism_match = re.search(r"PRISM\s+ID\s*:\s*(\d+)", compact, re.I)
        due_match = re.search(r"Est\.?\s*Completion\s*:\s*(\d{1,2}/\d{1,2}/\d{2,4})", compact, re.I)

        address = ""
        city = ""
        state_zip = ""

        # Formal work-order PDF.
        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
        for i, line in enumerate(lines):
            if re.fullmatch(r"\d{1,6}\s+.+\s(?:RD|ROAD|ST|STREET|AVE|AVENUE|LN|LANE|DR|DRIVE|BLVD|BOULEVARD|HWY|HIGHWAY|CT|COURT|CIR|CIRCLE|WAY|PKWY|PARKWAY)", line, re.I):
                address = line
                for follow in lines[i + 1:i + 6]:
                    city_match = re.fullmatch(r"([A-Za-z .'-]+),\s*SC\s+(\d{5})", follow, re.I)
                    if city_match:
                        city = city_match.group(1).strip().title()
                        state_zip = f"SC {city_match.group(2)}"
                        break
                break

        # FWM fallback.
        if not address:
            address_match = re.search(r"Address\s*:\s*([^\n]{5,120})", text, re.I)
            if address_match:
                address = re.sub(r"\s+", " ", address_match.group(1)).strip()
        if not city:
            city_match = re.search(r"City/State/Zip\s*:\s*([A-Za-z .'-]+),\s*SC\s+(\d{5})", text, re.I)
            if city_match:
                city = city_match.group(1).strip().title()
                state_zip = f"SC {city_match.group(2)}"

        customer_name = ""
        for i, line in enumerate(lines):
            if line.lower() == "job":
                for candidate in lines[i + 1:i + 5]:
                    if "," in candidate and not re.search(r"\d", candidate):
                        customer_name = candidate.title()
                        break
                if customer_name:
                    break

        parsed = {
            "work_order": work_order or expected_work_order,
            "prism": prism_match.group(1) if prism_match else "",
            "due_date": _normalize_date(due_match.group(1)) if due_match else "",
            "address": address.title() if address.isupper() else address,
            "city": city,
            "state_zip": state_zip,
            "customer_name": customer_name,
        }
        score = sum(bool(parsed[k]) for k in ("work_order", "prism", "due_date", "address", "city"))
        if re.search(r"WO_\d+_\d+\.pdf$", filename, re.I):
            score += 5
        elif "fwm" not in filename.lower():
            score += 1
        if best is None or score > best[0]:
            best = (score, parsed)
    return best[1] if best else {}


def enrich_ntp_from_attachments(
    parsed: ParsedNtp, attachments: list[dict[str, Any]]
) -> ParsedNtp:
    details = _extract_work_order_document(
        _candidate_document_texts(attachments), parsed.work_order
    )
    if not details:
        return parsed
    parsed.prism = details.get("prism") or parsed.prism
    parsed.address = details.get("address") or parsed.address
    parsed.city = details.get("city") or parsed.city
    parsed.state_zip = details.get("state_zip") or parsed.state_zip
    parsed.due_date = details.get("due_date") or parsed.due_date
    parsed.customer_name = details.get("customer_name") or parsed.customer_name
    parsed.market = _market_for(parsed.city, parsed.address)
    return parsed


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




def find_mail_folder_id(token: str, display_name: str) -> str:
    wanted = display_name.strip().lower()
    for folder in list_mail_folders(token):
        if str(folder.get("displayName", "")).strip().lower() == wanted:
            return str(folder.get("id", ""))
    return ""


def list_folder_messages(token: str, folder_id: str, *, top: int = 100) -> list[dict[str, Any]]:
    user = urllib.parse.quote(mailbox())
    fid = urllib.parse.quote(folder_id, safe="")
    query = urllib.parse.urlencode(
        {
            "$top": str(top),
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,receivedDateTime,bodyPreview,hasAttachments,internetMessageId,isRead,conversationId,from",
        }
    )
    result = _request(
        "GET",
        f"{GRAPH_ROOT}/users/{user}/mailFolders/{fid}/messages?{query}",
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


def mark_message_read(token: str, message_id: str) -> None:
    user = urllib.parse.quote(mailbox())
    mid = urllib.parse.quote(message_id, safe="")
    payload = json.dumps({"isRead": True}).encode("utf-8")
    _request(
        "PATCH",
        f"{GRAPH_ROOT}/users/{user}/messages/{mid}",
        token=token,
        body=payload,
        headers={"Content-Type": "application/json"},
    )


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



def mailbox_diagnostics(token: str) -> dict[str, Any]:
    user = urllib.parse.quote(mailbox())

    folders_result = _request(
        "GET",
        f"{GRAPH_ROOT}/users/{user}/mailFolders?$top=100&includeHiddenFolders=true",
        token=token,
    )
    folders = folders_result.get("value", []) if isinstance(folders_result, dict) else []

    inbox_result = _request(
        "GET",
        (
            f"{GRAPH_ROOT}/users/{user}/mailFolders/inbox/messages"
            "?$top=25&$orderby=receivedDateTime%20desc"
            "&$select=id,subject,receivedDateTime,isRead,parentFolderId,hasAttachments"
        ),
        token=token,
    )
    inbox_messages = inbox_result.get("value", []) if isinstance(inbox_result, dict) else []

    all_result = _request(
        "GET",
        (
            f"{GRAPH_ROOT}/users/{user}/messages"
            "?$top=25&$orderby=receivedDateTime%20desc"
            "&$select=id,subject,receivedDateTime,isRead,parentFolderId,hasAttachments"
        ),
        token=token,
    )
    all_messages = all_result.get("value", []) if isinstance(all_result, dict) else []

    return {
        "mailbox": mailbox(),
        "folders": [
            {
                "id": f.get("id"),
                "displayName": f.get("displayName"),
                "totalItemCount": f.get("totalItemCount"),
                "unreadItemCount": f.get("unreadItemCount"),
            }
            for f in folders
        ],
        "inbox_messages": inbox_messages,
        "all_messages": all_messages,
    }


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
