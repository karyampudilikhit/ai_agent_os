"""post_slack — POST a message to a Slack Incoming Webhook.

Simpler than the Bot API — no OAuth, one URL. Configure at
https://api.slack.com/apps -> Incoming Webhooks. Set the URL in env
var SLACK_WEBHOOK_URL. Messages post to whichever channel the webhook
was created for.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

import httpx

from backend.app.actions.action_registry import ActionSpec


def _handler(args: Dict[str, Any]) -> str:
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        return "(SLACK_WEBHOOK_URL not configured on the server)"
    text = str(args.get("text") or "").strip()
    if not text:
        return "(missing 'text')"
    payload = {"text": text}
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(url, json=payload)
    if resp.status_code == 200 and resp.text.strip() in {"ok", "OK"}:
        return f"Posted to Slack ({len(text)} chars)"
    return f"Slack returned HTTP {resp.status_code}: {resp.text[:200]}"


def _preview(args: Dict[str, Any]) -> str:
    text = str(args.get("text") or "").strip()
    snippet = text[:140] + ("…" if len(text) > 140 else "")
    return f"Slack post: {snippet}"


SPEC = ActionSpec(
    name="post_slack",
    description="Post a message to Slack via the configured Incoming Webhook. Requires SLACK_WEBHOOK_URL env var. Mutating — asks the founder for approval before posting.",
    parameters=[
        {"name": "text", "type": "string", "description": "Message text (Slack mrkdwn supported)", "required": True},
    ],
    handler=_handler,
    preview=_preview,
    mutating=True,
)
