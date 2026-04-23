"""
Gmail reader for Indeed job alert emails.

Connects to Gmail via OAuth 2.0, fetches Indeed alert emails,
and returns their content (plaintext + HTML) for Claude to parse.

No brittle HTML parsing here -- Claude does the extraction.
"""
import os
import json
import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_PATH = Path(__file__).parent / "token.json"


@dataclass
class RawEmail:
    """A single email ready to be handed to Claude for parsing."""
    message_id: str
    sender: str
    subject: str
    received_date: str
    body_text: str
    body_html: str


# ============================================================
# AUTH
# ============================================================

def _load_credentials_from_env() -> Optional[Credentials]:
    token_str = os.environ.get("GMAIL_TOKEN_JSON")
    if not token_str:
        return None
    try:
        token_data = json.loads(token_str)
        creds = Credentials.from_authorized_user_info(token_data, SCOPES)
        logger.info("Loaded Gmail credentials from GMAIL_TOKEN_JSON env var.")
        return creds
    except Exception as e:
        logger.error(f"Failed to parse GMAIL_TOKEN_JSON: {e}")
        return None


def _load_credentials_from_file() -> Optional[Credentials]:
    if not TOKEN_PATH.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        logger.info(f"Loaded Gmail credentials from {TOKEN_PATH}")
        return creds
    except Exception as e:
        logger.error(f"Failed to load token.json: {e}")
        return None


def get_gmail_service():
    creds = _load_credentials_from_env() or _load_credentials_from_file()

    if not creds:
        raise RuntimeError(
            "No Gmail credentials found. "
            "Run `python authorize.py` locally, then copy token.json contents "
            "to GMAIL_TOKEN_JSON env var on Railway."
        )

    if creds.expired and creds.refresh_token:
        logger.info("Gmail token expired, refreshing...")
        creds.refresh(Request())
        if TOKEN_PATH.exists():
            TOKEN_PATH.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ============================================================
# FETCH
# ============================================================

def _list_indeed_emails(service, hours_back: int = 48) -> list[str]:
    """Return message IDs of Indeed emails from the last N hours."""
    days_back = max(1, hours_back // 24 + 1)
    query = f"(from:indeed.com OR from:indeed.ca) newer_than:{days_back}d"

    logger.info(f"Searching Gmail: {query}")
    all_ids = []
    page_token = None

    while True:
        resp = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=100,
            pageToken=page_token,
        ).execute()

        messages = resp.get("messages", [])
        all_ids.extend(m["id"] for m in messages)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    logger.info(f"Found {len(all_ids)} Indeed emails in last {hours_back}h window.")
    return all_ids


def _decode_body(data: str) -> str:
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_bodies(payload: dict) -> tuple[str, str]:
    """Walk MIME parts and return (plaintext, html) bodies."""
    text_parts = []
    html_parts = []

    def walk(part: dict):
        mime_type = part.get("mimeType", "")
        data = part.get("body", {}).get("data", "")

        if mime_type == "text/plain" and data:
            text_parts.append(_decode_body(data))
        elif mime_type == "text/html" and data:
            html_parts.append(_decode_body(data))

        for sub in part.get("parts", []):
            walk(sub)

    walk(payload)
    return "\n".join(text_parts), "\n".join(html_parts)


def _get_header(headers: list[dict], name: str) -> str:
    name_low = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_low:
            return h.get("value", "")
    return ""


def _fetch_email(service, message_id: str) -> Optional[RawEmail]:
    msg = service.users().messages().get(
        userId="me",
        id=message_id,
        format="full",
    ).execute()

    payload = msg.get("payload", {})
    headers = payload.get("headers", [])

    sender = _get_header(headers, "From")
    subject = _get_header(headers, "Subject")
    received_date = _get_header(headers, "Date")

    body_text, body_html = _extract_bodies(payload)

    return RawEmail(
        message_id=message_id,
        sender=sender,
        subject=subject,
        received_date=received_date,
        body_text=body_text,
        body_html=body_html,
    )


# ============================================================
# PUBLIC API
# ============================================================

def fetch_indeed_emails(hours_back: int = 48) -> list[RawEmail]:
    """Fetch all Indeed alert emails in the window."""
    service = get_gmail_service()
    message_ids = _list_indeed_emails(service, hours_back=hours_back)

    if not message_ids:
        logger.warning("No Indeed emails found in the window.")
        return []

    emails = []
    for msg_id in message_ids:
        try:
            email = _fetch_email(service, msg_id)
            if email:
                emails.append(email)
                logger.info(
                    f"Fetched email {msg_id}: '{email.subject[:60]}' "
                    f"(text: {len(email.body_text)} chars, html: {len(email.body_html)} chars)"
                )
        except Exception as e:
            logger.error(f"Failed to fetch email {msg_id}: {e}")

    return emails
