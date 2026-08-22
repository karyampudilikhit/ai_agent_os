"""OAuth2 connect flow — the "Connect GitHub" button's endpoints.

The whole point: a user clicks Connect, approves on the provider's own
page, and comes back connected. They never generate a token, never
paste one, and can revoke from the provider's settings whenever they
want. Replaces the "every user must create a PAT and edit .env" step,
which is fine for the developer and a dealbreaker for everyone else.

Flow:
  GET  /api/oauth/{provider}/start     -> 302 to the provider's consent screen
  GET  /api/oauth/{provider}/callback  -> provider redirects here with ?code
                                          -> exchange for a token, store, show
                                             a small "connected" page
  GET  /api/oauth/status               -> which providers are connected (no secrets)
  POST /api/oauth/{provider}/disconnect

`state` is a signed-ish random nonce held server-side and checked on
callback — without it, a third party could hand the user a crafted
callback URL and bind an attacker-controlled account to this instance
(CSRF). It's single-use and expires.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.app.auth.oauth_providers import (
    get_provider,
    list_providers,
    redirect_uri,
)
from backend.app.auth.oauth_store import get_store as get_oauth_store

logger = logging.getLogger(__name__)

router = APIRouter()

# In-memory, single-use CSRF nonces: state -> (provider, created_at).
# In-process is fine — the whole handshake completes in under a minute
# and a server restart mid-flow just means the user clicks Connect again.
_PENDING_STATES: Dict[str, Dict[str, Any]] = {}
STATE_TTL_SECONDS = 600


def _new_state(provider_key: str) -> str:
    _prune_states()
    state = secrets.token_urlsafe(24)
    _PENDING_STATES[state] = {"provider": provider_key, "created_at": time.time()}
    return state


def _consume_state(state: str) -> Optional[str]:
    """Returns the provider key this state was issued for, or None if it
    is unknown/expired/already used. Single-use by design."""
    _prune_states()
    rec = _PENDING_STATES.pop(state, None)
    return rec["provider"] if rec else None


def _prune_states() -> None:
    cutoff = time.time() - STATE_TTL_SECONDS
    for s in [s for s, r in _PENDING_STATES.items() if r["created_at"] < cutoff]:
        _PENDING_STATES.pop(s, None)


def _page(title: str, body_html: str, ok: bool = True) -> HTMLResponse:
    accent = "#7dbf8a" if ok else "#c13350"
    return HTMLResponse(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
 body{{background:#0c0c0c;color:#f5f0e8;font-family:Inter,-apple-system,sans-serif;
       display:grid;place-items:center;height:100vh;margin:0;text-align:center}}
 .box{{max-width:520px;padding:36px}}
 h1{{font-size:22px;margin:0 0 12px;color:{accent}}}
 p{{color:#a5a5ae;line-height:1.6;margin:0 0 20px}}
 code{{background:#1e1e22;padding:2px 6px;border-radius:4px;font-size:13px}}
 a{{color:#c13350;font-weight:600;text-decoration:none}}
</style></head>
<body><div class="box"><h1>{title}</h1>{body_html}
<p><a href="/app/">← Back to Vision AI</a></p></div></body></html>""")


@router.get("/oauth/status")
def oauth_status() -> Dict[str, Any]:
    """What the UI renders the Connect buttons from. Never returns tokens.

    `configured` tells the UI whether the PRODUCT OWNER has registered
    the OAuth App yet — so the button can say "needs setup" instead of
    failing mysteriously when a user clicks it.
    """
    store = get_oauth_store()
    live = store.status()
    out = []
    for p in list_providers():
        rec = live.get(p.key) or {}
        out.append({
            "provider": p.key,
            "label": p.label,
            "configured": p.configured(),
            "connected": bool(rec.get("connected")),
            "account": rec.get("account"),
            "scopes": p.scopes,
        })
    return {"providers": out}


@router.get("/oauth/{provider_key}/start")
def oauth_start(provider_key: str):
    provider = get_provider(provider_key)
    if not provider:
        raise HTTPException(404, f"Unknown OAuth provider {provider_key!r}")
    if not provider.configured():
        return _page(
            f"{provider.label} isn't set up yet",
            f"<p>Vision AI needs a {provider.label} OAuth App before anyone can "
            f"connect. The product owner sets this up once:</p>"
            f"<p style='text-align:left'>1. Register an OAuth App on {provider.label}<br>"
            f"2. Set the callback URL to <code>{redirect_uri(provider.key)}</code><br>"
            f"3. Put the client id/secret in the server env as "
            f"<code>{provider.client_id_env}</code> and "
            f"<code>{provider.client_secret_env}</code></p>",
            ok=False,
        )

    state = _new_state(provider.key)
    params = {
        "client_id": provider.client_id(),
        "redirect_uri": redirect_uri(provider.key),
        "scope": " ".join(provider.scopes),
        "state": state,
        "response_type": "code",
        **provider.extra_authorize_params,
    }
    url = f"{provider.authorize_url}?{urlencode(params)}"
    logger.info("OAuth start for %s", provider.key)
    return RedirectResponse(url, status_code=302)


@router.get("/oauth/{provider_key}/callback")
def oauth_callback(
    provider_key: str,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
):
    provider = get_provider(provider_key)
    if not provider:
        raise HTTPException(404, f"Unknown OAuth provider {provider_key!r}")

    # User clicked "Cancel" on the provider's consent screen, or the
    # provider rejected the request outright.
    if error:
        return _page(
            f"{provider.label} connection cancelled",
            f"<p>{error_description or error}</p><p>Nothing was connected.</p>",
            ok=False,
        )

    if not code or not state:
        return _page(provider.label + " connection failed",
                     "<p>The provider didn't send back an authorization code.</p>", ok=False)

    expected_provider = _consume_state(state)
    if expected_provider != provider.key:
        # Unknown / expired / replayed state — refuse rather than bind
        # a token from a request we didn't initiate.
        logger.warning("OAuth callback with bad state for %s", provider.key)
        return _page(
            "Connection rejected",
            "<p>That connection link was invalid or expired. Start again from "
            "the Connect button.</p>",
            ok=False,
        )

    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                provider.token_url,
                data={
                    "client_id": provider.client_id(),
                    "client_secret": provider.client_secret(),
                    "code": code,
                    "redirect_uri": redirect_uri(provider.key),
                    "grant_type": "authorization_code",
                },
                headers={"Accept": "application/json"},
            )
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("OAuth token exchange failed for %s: %s", provider.key, exc)
        return _page(provider.label + " connection failed",
                     f"<p>Couldn't exchange the code for a token: {exc}</p>", ok=False)

    access_token = payload.get("access_token")
    if not access_token:
        detail = payload.get("error_description") or payload.get("error") or str(payload)[:200]
        return _page(provider.label + " connection failed", f"<p>{detail}</p>", ok=False)

    account = _fetch_account_label(provider.key, access_token)
    get_oauth_store().save(
        provider=provider.key,
        access_token=access_token,
        refresh_token=payload.get("refresh_token"),
        token_type=payload.get("token_type", "bearer"),
        scope=payload.get("scope", ""),
        expires_in=payload.get("expires_in"),
        account=account,
    )
    logger.info("OAuth connected: %s (%s)", provider.key, account or "unknown account")
    return _page(
        f"{provider.label} connected",
        f"<p>Vision AI can now act on {provider.label}"
        + (f" as <code>{account}</code>" if account else "")
        + ". You can revoke access anytime from your "
        f"{provider.label} settings.</p>",
    )


@router.post("/oauth/{provider_key}/disconnect")
def oauth_disconnect(provider_key: str) -> Dict[str, Any]:
    provider = get_provider(provider_key)
    if not provider:
        raise HTTPException(404, f"Unknown OAuth provider {provider_key!r}")
    removed = get_oauth_store().disconnect(provider.key)
    return {"provider": provider.key, "disconnected": removed}


def _fetch_account_label(provider_key: str, token: str) -> Optional[str]:
    """Display-only: whose account did we just connect. Purely cosmetic —
    a failure here must never fail the connection itself."""
    try:
        if provider_key == "github":
            with httpx.Client(timeout=10.0) as client:
                r = client.get(
                    "https://api.github.com/user",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
            if r.status_code == 200:
                return r.json().get("login")
    except Exception:  # noqa: BLE001
        pass
    return None
