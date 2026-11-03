"""Google OAuth for Sift: read-only scope, PKCE loopback flow, tokens in the OS keychain.

Tokens never leave this module except as a `Credentials` object handed to GmailReader.
They are never logged, never returned by the API and never written inside the repo.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.parse
import urllib.request
from pathlib import Path

import keyring
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .base import PermanentError

log = logging.getLogger("sift.auth")

READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
# Phase 2: read-only only. gmail.modify is added in Phase 4 via an explicit "Enable cleanup" flow.
ALLOWED_SCOPES = {READONLY_SCOPE}
KEYRING_SERVICE = "sift-gmail"
KEYRING_USER = "oauth-token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"


class AuthError(Exception):
    pass


class ReauthRequired(AuthError, PermanentError):
    """Refresh token expired or revoked (Testing-mode refresh tokens last 7 days)."""


class TokenStore:
    """Keychain-backed token storage. `backend` is injectable for tests."""

    def __init__(self, backend=keyring):
        self.backend = backend

    def load(self) -> Credentials | None:
        raw = self.backend.get_password(KEYRING_SERVICE, KEYRING_USER)
        if not raw:
            return None
        info = json.loads(raw)
        return Credentials.from_authorized_user_info(info, scopes=info.get("scopes"))

    def save(self, creds: Credentials) -> None:
        self.backend.set_password(KEYRING_SERVICE, KEYRING_USER, creds.to_json())

    def delete(self) -> None:
        try:
            self.backend.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except keyring.errors.PasswordDeleteError:
            pass


def check_scopes(creds: Credentials) -> None:
    granted = set(creds.granted_scopes or creds.scopes or [])
    extra = granted - ALLOWED_SCOPES
    if extra:
        raise AuthError(f"refusing token with unexpected scopes: {sorted(extra)}")
    if READONLY_SCOPE not in granted:
        raise AuthError("Gmail read access was not granted")


def fresh(creds: Credentials, store: TokenStore, lock: threading.Lock) -> Credentials:
    """Refresh the access token if needed and persist it. Raises ReauthRequired if the refresh token is dead."""
    with lock:
        if creds.valid:
            return creds
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise ReauthRequired("Google sign-in expired; reconnect Gmail") from e
        store.save(creds)
        return creds


def run_consent_flow(client_secrets: Path, timeout_s: int = 300) -> Credentials:
    """Open the browser for Google consent on a loopback redirect (PKCE is automatic). Blocks until done."""
    if not client_secrets.exists():
        raise AuthError(f"OAuth client file not found at {client_secrets} (see docs/google-setup.md)")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), scopes=sorted(ALLOWED_SCOPES))
    creds = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True, timeout_seconds=timeout_s,
                                  authorization_prompt_message="Opening Google sign-in in your browser…",
                                  success_message="Sift is connected (read-only). You can close this tab.")
    check_scopes(creds)
    return creds


def revoke(creds: Credentials, opener=urllib.request.urlopen) -> bool:
    """Revoke the grant at Google. Returns True if Google confirmed it."""
    token = creds.refresh_token or creds.token
    if not token:
        return False
    data = urllib.parse.urlencode({"token": token}).encode()
    req = urllib.request.Request(REVOKE_URL, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with opener(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:  # message only; never the token
        log.warning("token revoke failed: %s", type(e).__name__)
        return False
