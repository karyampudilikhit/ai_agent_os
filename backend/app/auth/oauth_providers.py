"""OAuth2 provider registry — the "Connect X" button's backing config.

Why this exists: making every user generate and paste a personal access
token is a non-starter for a real product. Users expect the flow they
get everywhere else — click "Connect GitHub", approve on GitHub's own
page, done. That's OAuth2 authorization-code, and this module is the
provider-agnostic half of it.

Two distinct roles, easy to conflate:

  - The PRODUCT OWNER (you) registers ONE OAuth App per provider, once
    ever, and puts its client_id/client_secret in the server env. This
    is Vision AI's identity to GitHub — it is not per-user and never
    changes.
  - EVERY USER then just clicks Connect. They never see a token, never
    paste anything, and can revoke access from their own GitHub
    settings at any time.

Adding a provider later (Google Calendar, LinkedIn, Slack) is a dict
entry here plus its env vars — the routes, token store, and refresh
logic are all shared.

Scopes are deliberately minimal per provider: ask for the least that
makes the built-in actions work, because a broad scope prompt is the
main reason users bounce off a Connect screen.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class OAuthProvider:
    key: str                    # "github"
    label: str                  # "GitHub"
    authorize_url: str
    token_url: str
    scopes: List[str]
    client_id_env: str
    client_secret_env: str
    # Some providers (GitHub) return a non-expiring token and no
    # refresh_token; others (Google) always send one. Drives whether we
    # attempt refresh.
    supports_refresh: bool = False
    # Extra params some providers require on the authorize call.
    extra_authorize_params: Dict[str, str] = field(default_factory=dict)

    def client_id(self) -> str:
        return os.environ.get(self.client_id_env, "").strip()

    def client_secret(self) -> str:
        return os.environ.get(self.client_secret_env, "").strip()

    def configured(self) -> bool:
        """True when the product owner has registered the OAuth App and
        set both env vars. Until then the Connect button must tell the
        owner what to do rather than 500."""
        return bool(self.client_id() and self.client_secret())


PROVIDERS: Dict[str, OAuthProvider] = {
    "github": OAuthProvider(
        key="github",
        label="GitHub",
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        # "repo" covers create/read/write on repos, which is what
        # create_github_repo needs. Deliberately NOT asking for
        # admin:org, delete_repo, or user scopes.
        scopes=["repo"],
        client_id_env="GITHUB_OAUTH_CLIENT_ID",
        client_secret_env="GITHUB_OAUTH_CLIENT_SECRET",
        supports_refresh=False,
    ),
    # --- Ready to enable; each needs its OAuth App registered first ---
    # "google": OAuthProvider(
    #     key="google", label="Google Calendar",
    #     authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    #     token_url="https://oauth2.googleapis.com/token",
    #     scopes=["https://www.googleapis.com/auth/calendar.events"],
    #     client_id_env="GOOGLE_OAUTH_CLIENT_ID",
    #     client_secret_env="GOOGLE_OAUTH_CLIENT_SECRET",
    #     supports_refresh=True,
    #     extra_authorize_params={"access_type": "offline", "prompt": "consent"},
    # ),
}


def get_provider(key: str) -> Optional[OAuthProvider]:
    return PROVIDERS.get((key or "").strip().lower())


def list_providers() -> List[OAuthProvider]:
    return list(PROVIDERS.values())


def redirect_uri(provider_key: str) -> str:
    """Where the provider sends the user back after they approve.

    Must EXACTLY match the callback URL registered on the OAuth App —
    a mismatch is the single most common cause of a failed connect, and
    providers reject it rather than warn. PUBLIC_BASE_URL lets a
    deployed instance (neutron-ai.fly.dev) differ from local dev
    without touching the registered app for the other environment.
    """
    base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not base:
        base = "http://127.0.0.1:8000"
    return f"{base}/api/oauth/{provider_key}/callback"
