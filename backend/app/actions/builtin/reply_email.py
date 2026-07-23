"""reply_email — send an SMTP reply threaded to an existing message.

Sends via the same SMTP creds as send_email but sets In-Reply-To and
References headers so the reply threads correctly in Gmail / Outlook.

Args:
    in_reply_to_message_id: the target message's Message-ID header
                            (pulled from read_inbox output).
    to:      recipient (usually the From of the original message).
    subject: reply subject (typically 'Re: <original subject>').
    body:    plain-text reply body.

Mutating — asks the founder for approval before sending.
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from typing import Any, Dict

from backend.app.actions.action_registry import ActionSpec


def _handler(args: Dict[str, Any]) -> str:
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        return "(SMTP_HOST not configured on the server)"
    port = int(os.environ.get("SMTP_PORT", "587") or 587)
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "").strip()
    from_addr = (os.environ.get("SMTP_FROM") or user).strip()

    to_addr = str(args.get("to") or "").strip()
    subject = str(args.get("subject") or "").strip()
    body = str(args.get("body") or "").strip()
    in_reply_to = str(args.get("in_reply_to_message_id") or "").strip()
    if not to_addr:
        return "(missing 'to' address)"
    if not in_reply_to:
        return "(missing 'in_reply_to_message_id' — use send_email for a fresh message instead)"

    # Gmail expects Message-IDs wrapped in <…>. Accept either shape.
    if not in_reply_to.startswith("<"):
        in_reply_to = f"<{in_reply_to}>"

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject or "(no subject)"
    msg["In-Reply-To"] = in_reply_to
    msg["References"] = in_reply_to  # single-hop reference chain
    msg.set_content(body or "(empty body)")

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        if user:
            server.login(user, password)
        server.send_message(msg)

    return f"Sent reply to {to_addr} in thread {in_reply_to}"


def _preview(args: Dict[str, Any]) -> str:
    to_addr = str(args.get("to") or "").strip() or "?"
    subject = str(args.get("subject") or "").strip() or "(no subject)"
    body = str(args.get("body") or "").strip()
    snippet = body[:120] + ("…" if len(body) > 120 else "")
    return f"Reply → {to_addr} | Subj: {subject} | {snippet}"


SPEC = ActionSpec(
    name="reply_email",
    description=(
        "Send a threaded reply to an existing email. Sets In-Reply-To and References "
        "so the reply lands in the original thread (Gmail groups it under the original "
        "subject). Uses the same SMTP_* env as send_email. Mutating — asks the founder "
        "for approval before sending."
    ),
    parameters=[
        {"name": "to", "type": "string", "description": "Recipient — usually the From of the original message", "required": True},
        {"name": "subject", "type": "string", "description": "Reply subject, typically 'Re: <original subject>'", "required": True},
        {"name": "body", "type": "string", "description": "Plain-text reply body", "required": True},
        {"name": "in_reply_to_message_id", "type": "string", "description": "The original message's Message-ID (from read_inbox output)", "required": True},
    ],
    handler=_handler,
    preview=_preview,
    mutating=True,
)
