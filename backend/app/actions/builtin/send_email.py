"""send_email — SMTP send via env-configured credentials.

Env vars (set these before starting the server for the action to work):
    SMTP_HOST     - e.g. smtp.gmail.com
    SMTP_PORT     - int, default 587
    SMTP_USER     - the login/username
    SMTP_PASS     - app password or SMTP token (NOT your account password)
    SMTP_FROM     - the From: address (defaults to SMTP_USER)

For Gmail: create an App Password at
https://myaccount.google.com/apppasswords and use it as SMTP_PASS.
"""

from __future__ import annotations

import os
import smtplib
import socket
import time
import uuid
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any, Dict

from backend.app.actions.action_registry import ActionSpec


def _handler(args: Dict[str, Any]) -> str:
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        return "(SMTP_HOST not configured on the server; set SMTP_HOST/PORT/USER/PASS in env first)"
    port = int(os.environ.get("SMTP_PORT", "587") or 587)
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "").strip()
    from_addr = (os.environ.get("SMTP_FROM") or user).strip()

    to_addr = str(args.get("to") or "").strip()
    subject = str(args.get("subject") or "").strip()
    body = str(args.get("body") or "").strip()
    if not to_addr:
        return "(missing 'to' address)"

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject or "(no subject)"
    # Stamp our own Message-ID so we can return it to the caller (the
    # CEO / specialist) — future reply_email calls thread against this.
    domain = from_addr.split("@")[-1] if "@" in from_addr else "vision-ai.local"
    msg_id = make_msgid(domain=domain)
    msg["Message-ID"] = msg_id
    msg.set_content(body or "(empty body)")

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        if user:
            server.login(user, password)
        server.send_message(msg)

    return (
        f"Sent email to {to_addr} — subject: {subject or '(no subject)'}\n"
        f"Message-ID: {msg_id}\n"
        f"(Hand this Message-ID to reply_email later to thread a reply "
        f"or match against read_inbox output.)"
    )


def _preview(args: Dict[str, Any]) -> str:
    to_addr = str(args.get("to") or "").strip() or "?"
    subject = str(args.get("subject") or "").strip() or "(no subject)"
    body = str(args.get("body") or "").strip()
    snippet = body[:120] + ("…" if len(body) > 120 else "")
    return f"Email → {to_addr} | Subj: {subject} | {snippet}"


SPEC = ActionSpec(
    name="send_email",
    description="Send an email via SMTP. Requires SMTP_* env vars on the server. Mutating — asks the founder for approval before sending.",
    parameters=[
        {"name": "to", "type": "string", "description": "Recipient email address", "required": True},
        {"name": "subject", "type": "string", "description": "Email subject line", "required": True},
        {"name": "body", "type": "string", "description": "Plain-text body", "required": True},
    ],
    handler=_handler,
    preview=_preview,
    mutating=True,
)
