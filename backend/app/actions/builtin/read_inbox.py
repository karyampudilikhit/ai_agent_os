"""read_inbox — pull recent messages from an IMAP inbox.

Read-only, so it runs inline without approval. Companion to
send_email — together they close the "send → wait → read reply →
continue" loop that turns Vision AI from "sends emails" into
"runs outbound campaigns."

Env vars:
    IMAP_HOST   - defaults to imap.gmail.com
    IMAP_PORT   - defaults to 993 (SSL)
    IMAP_USER   - defaults to SMTP_USER (typical case: same account)
    IMAP_PASS   - defaults to SMTP_PASS (typical case: same App Password)

For Gmail: the same App Password minted for SMTP works for IMAP.
Enable IMAP in Gmail: Settings -> Forwarding and POP/IMAP -> Enable IMAP.
"""

from __future__ import annotations

import email
import imaplib
import os
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Dict, List, Optional

from backend.app.actions.action_registry import ActionSpec

MAX_MESSAGES = 20  # cap the listing so a big inbox doesn't blow the prompt
MAX_BODY_CHARS_PER_MESSAGE = 2500


def _config() -> Dict[str, str]:
    return {
        "host": os.environ.get("IMAP_HOST", "imap.gmail.com").strip(),
        "port": int(os.environ.get("IMAP_PORT", "993") or 993),
        "user": (os.environ.get("IMAP_USER") or os.environ.get("SMTP_USER") or "").strip(),
        "password": (os.environ.get("IMAP_PASS") or os.environ.get("SMTP_PASS") or "").strip(),
    }


def _decode(value: Optional[str]) -> str:
    """Decode RFC-2047 encoded headers (=?UTF-8?B?…?=) into plain text."""
    if not value:
        return ""
    out = []
    for chunk, enc in decode_header(value):
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _extract_body(msg: Message) -> str:
    """Return the message body as plain text. Prefer text/plain, fall
    back to text/html with tags stripped roughly."""
    plain_parts: List[str] = []
    html_parts: List[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if part.get("Content-Disposition", "").startswith("attachment"):
                continue
            if ctype == "text/plain":
                plain_parts.append(_part_payload(part))
            elif ctype == "text/html":
                html_parts.append(_part_payload(part))
    else:
        ctype = msg.get_content_type()
        if ctype == "text/plain":
            plain_parts.append(_part_payload(msg))
        elif ctype == "text/html":
            html_parts.append(_part_payload(msg))
    body = "\n".join(p for p in plain_parts if p).strip()
    if not body:
        raw_html = "\n".join(p for p in html_parts if p).strip()
        body = _strip_tags(raw_html)
    return body


def _part_payload(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _strip_tags(html: str) -> str:
    """Quick-and-dirty HTML -> text. Good enough for reading Gmail replies
    without pulling in beautifulsoup for this one path."""
    import re
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    import html as html_module
    return html_module.unescape(text).strip()


def _build_search(args: Dict[str, Any]) -> List[bytes]:
    """Translate the LLM-supplied filters into IMAP SEARCH tokens."""
    parts: List[bytes] = []
    if args.get("unread_only"):
        parts.append(b"UNSEEN")
    frm = str(args.get("from") or "").strip()
    if frm:
        parts.append(b"FROM")
        parts.append(('"' + frm + '"').encode("utf-8"))
    subj = str(args.get("subject_contains") or "").strip()
    if subj:
        parts.append(b"SUBJECT")
        parts.append(('"' + subj + '"').encode("utf-8"))
    return parts or [b"ALL"]


def _handler(args: Dict[str, Any]) -> str:
    cfg = _config()
    if not cfg["user"] or not cfg["password"]:
        return "(IMAP_USER/IMAP_PASS not configured — set them or SMTP_USER/SMTP_PASS in env)"

    limit = int(args.get("limit") or 10)
    limit = max(1, min(limit, MAX_MESSAGES))
    folder = str(args.get("folder") or "INBOX").strip() or "INBOX"

    try:
        imap = imaplib.IMAP4_SSL(cfg["host"], cfg["port"])
        imap.login(cfg["user"], cfg["password"])
    except Exception as exc:  # noqa: BLE001
        return f"(IMAP login failed: {exc})"

    try:
        status, _ = imap.select(folder, readonly=True)
        if status != "OK":
            return f"(could not open folder {folder!r})"

        search_criteria = _build_search(args)
        status, data = imap.search(None, *search_criteria)
        if status != "OK" or not data or not data[0]:
            return f"No messages match filter in {folder} (query: {b' '.join(search_criteria).decode(errors='replace')})"

        ids = data[0].split()
        # Newest first — IMAP returns oldest first, slice from tail.
        ids = ids[-limit:][::-1]

        out_lines: List[str] = [f"=== {len(ids)} message(s) in {folder} (newest first) ==="]
        for msg_id in ids:
            status, msg_data = imap.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            frm = _decode(msg.get("From"))
            to = _decode(msg.get("To"))
            subject = _decode(msg.get("Subject"))
            date = msg.get("Date") or ""
            msg_id_hdr = msg.get("Message-ID") or ""
            body = _extract_body(msg)
            if len(body) > MAX_BODY_CHARS_PER_MESSAGE:
                body = body[:MAX_BODY_CHARS_PER_MESSAGE].rstrip() + "\n[…body truncated…]"
            out_lines.append(
                f"\n--- Message ---\n"
                f"From: {frm}\n"
                f"To: {to}\n"
                f"Date: {date}\n"
                f"Subject: {subject}\n"
                f"Message-ID: {msg_id_hdr}\n"
                f"---\n{body}"
            )
        return "\n".join(out_lines)
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


def _preview(args: Dict[str, Any]) -> str:
    parts = [f"limit={args.get('limit') or 10}"]
    if args.get("unread_only"):
        parts.append("unread")
    if args.get("from"):
        parts.append(f"from={args['from']}")
    if args.get("subject_contains"):
        parts.append(f"subj~{args['subject_contains']!r}")
    return "Read inbox: " + ", ".join(parts)


SPEC = ActionSpec(
    name="read_inbox",
    description=(
        "Fetch recent email messages from an IMAP inbox (Gmail by default). "
        "Filters: unread_only (bool), from (email substring), subject_contains "
        "(subject substring), folder (default INBOX), limit (default 10, max 20). "
        "Read-only — runs immediately without approval. Requires IMAP_USER / IMAP_PASS "
        "env (falls back to SMTP_USER / SMTP_PASS)."
    ),
    parameters=[
        {"name": "unread_only", "type": "boolean", "description": "Only messages IMAP flagged UNSEEN"},
        {"name": "from", "type": "string", "description": "Match sender substring (Gmail-safe)"},
        {"name": "subject_contains", "type": "string", "description": "Match subject substring"},
        {"name": "folder", "type": "string", "description": "IMAP folder name (default INBOX)"},
        {"name": "limit", "type": "integer", "description": "How many messages to return (default 10, max 20)"},
    ],
    handler=_handler,
    preview=_preview,
    mutating=False,
)
