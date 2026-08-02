"""OAuthTokenStore — persisted per-provider access tokens.

Same JSON-on-disk trust model as HTTPToolStore and the ApprovalQueue
(both already hold secrets in plaintext): file lives under the data
root, gitignored, single-user MVP. Called out explicitly rather than
implied, because these ARE live credentials — a GitHub token here can
create and delete repos.

Shape:
{
  "github": {
      "access_token":  "gho_…",
      "refresh_token": null,
      "token_type":    "bearer",
      "scope":         "repo",
      "expires_at":    null,          # epoch seconds, null = never
      "account":       "karyampudilikhit",   # display only
      "connected_at":  1785600000.0
  }
}

Deliberately NOT per-end-user keyed yet — the app is still single-user
(no accounts/auth layer). When accounts land, this becomes
{user_id: {provider: {...}}} and the store API keeps its shape.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

STORE_FILENAME = ".oauth_tokens.json"
# Refresh a little early so a call never fires with a token that expires
# mid-flight.
EXPIRY_SKEW_SECONDS = 120


def _store_path() -> Path:
    from backend.app.utils.paths import under_data
    return under_data(STORE_FILENAME)


class OAuthTokenStore:
    def __init__(self, path: Optional[Path] = None):
        self._path = path or _store_path()
        self._lock = threading.Lock()

    def _read_all(self) -> Dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("oauth token store read failed (%s); starting empty", exc)
            return {}
        return data if isinstance(data, dict) else {}

    def _write_all(self, data: Dict[str, Any]) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)
        try:
            # Best-effort: keep creds owner-only where the OS supports it.
            os.chmod(self._path, 0o600)
        except Exception:  # noqa: BLE001
            pass

    def save(
        self,
        provider: str,
        access_token: str,
        refresh_token: Optional[str] = None,
        token_type: str = "bearer",
        scope: str = "",
        expires_in: Optional[int] = None,
        account: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            data = self._read_all()
            data[provider] = {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": token_type or "bearer",
                "scope": scope or "",
                "expires_at": (time.time() + expires_in) if expires_in else None,
                "account": account,
                "connected_at": time.time(),
            }
            self._write_all(data)
            return dict(data[provider])

    def get(self, provider: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            rec = self._read_all().get(provider)
            return dict(rec) if rec else None

    def access_token(self, provider: str) -> Optional[str]:
        """The token an action tool should use, or None if not connected.
        Returns None (rather than an expired token) when it has lapsed —
        callers surface 'not connected' instead of a confusing 401."""
        rec = self.get(provider)
        if not rec:
            return None
        expires_at = rec.get("expires_at")
        if expires_at and time.time() > (expires_at - EXPIRY_SKEW_SECONDS):
            logger.info("oauth token for %s is expired", provider)
            return None
        return rec.get("access_token") or None

    def is_connected(self, provider: str) -> bool:
        return bool(self.access_token(provider))

    def disconnect(self, provider: str) -> bool:
        with self._lock:
            data = self._read_all()
            existed = provider in data
            data.pop(provider, None)
            self._write_all(data)
            return existed

    def status(self) -> Dict[str, Dict[str, Any]]:
        """Non-secret summary for the UI — never returns the token."""
        with self._lock:
            data = self._read_all()
        out: Dict[str, Dict[str, Any]] = {}
        for provider, rec in data.items():
            expires_at = rec.get("expires_at")
            expired = bool(expires_at and time.time() > (expires_at - EXPIRY_SKEW_SECONDS))
            out[provider] = {
                "connected": not expired,
                "account": rec.get("account"),
                "scope": rec.get("scope"),
                "connected_at": rec.get("connected_at"),
                "expired": expired,
            }
        return out


_store: Optional[OAuthTokenStore] = None


def get_store() -> OAuthTokenStore:
    global _store
    if _store is None:
        _store = OAuthTokenStore()
    return _store
