"""Google OAuth for Sift: PKCE loopback flow, tokens in the OS keychain.

Two tokens, stored separately (PROGRESS item 8):
- reader (`sift-gmail`): gmail.readonly only. Used by GmailReader for scanning and revalidation.
- writer (`sift-gmail-writer`): gmail.modify (+ gmail.readonly). Only created when the user clicks
  "Enable cleanup", and only ever handed to GmailWriter, which only the Executor holds.

Google does not support incremental authorization for installed apps
(https://developers.google.com/identity/protocols/oauth2/native-app, "Incremental authorization is not
supported for installed apps or devices"), so "Enable cleanup" is a second, separate consent that yields its
own refresh token. Each token refreshes with its own stored scope list, so the reader's access tokens stay
read-only; scopes are re-checked after every refresh anyway.

Scopes are an allow-list: anything outside the role's allowed set (in particular the full-access scope and
any send/compose scope) is refused and the grant revoked.

Tokens never leave this module except as a `Credentials` object handed to GmailReader/GmailWriter.
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

# https://developers.google.com/workspace/gmail/api/auth/scopes (checked 2026-11-04)
READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
# gmail.modify: archive/label/trash; Google: "does not allow immediate, permanent deletion of threads and
# messages, bypassing the trash". Sift never calls a send/delete method (see .claude/hooks/guard.py).
MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"

READER, WRITER = "reader", "writer"
ALLOWED_SCOPES = {READONLY_SCOPE}                      # reader (kept for back-compat)
ROLE_ALLOWED = {READER: {READONLY_SCOPE}, WRITER: {READONLY_SCOPE, MODIFY_SCOPE}}
ROLE_REQUIRED = {READER: READONLY_SCOPE, WRITER: MODIFY_SCOPE}
ROLE_REQUESTED = {READER: [READONLY_SCOPE], WRITER: [MODIFY_SCOPE, READONLY_SCOPE]}
KNOWN_SCOPES = ROLE_ALLOWED[READER] | ROLE_ALLOWED[WRITER]

KEYRING_SERVICE = "sift-gmail"
WRITER_KEYRING_SERVICE = "sift-gmail-writer"
KEYRING_USER = "oauth-token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

REAUTH_MSG = {READER: "Google sign-in expired; reconnect Gmail",
              WRITER: "Cleanup permission expired; reconnect cleanup"}


class AuthError(Exception):
    pass


class ReauthRequired(AuthError, PermanentError):
    """Refresh token expired or revoked (Testing-mode refresh tokens last 7 days)."""
    reauth = True


class CleanupReauthRequired(ReauthRequired):
    """The writer (cleanup) token is dead. The read-only connection may still be fine."""


class CorruptToken(AuthError):
    """The keychain entry exists but can't be read or parsed."""


class TokenStore:
    """Keychain-backed token storage, one entry per role. `backend` is injectable for tests."""

    def __init__(self, backend=keyring, service: str = KEYRING_SERVICE):
        self.backend = backend
        self.service = service

    def for_writer(self) -> "TokenStore":
        return TokenStore(self.backend, WRITER_KEYRING_SERVICE)

    def load(self) -> Credentials | None:
        """Returns None if there is no entry. Raises CorruptToken if the entry can't be read or parsed."""
        try:
            raw = self.backend.get_password(self.service, KEYRING_USER)
            if not raw:
                return None
            info = json.loads(raw)
            return Credentials.from_authorized_user_info(info, scopes=info.get("scopes"))
        except Exception as e:  # never include the raw value in the message
            raise CorruptToken(f"stored token unreadable ({type(e).__name__})") from None

    def load_or_discard(self) -> tuple[Credentials | None, str | None]:
        """Load the token; a corrupt entry is treated as missing and deleted (PROGRESS item 12).
        Returns (creds, problem) where problem is a fixed, UI-safe sentence or None."""
        try:
            return self.load(), None
        except CorruptToken:
            gone = self.delete()
            log.warning("discarded unreadable keychain entry %s (deleted=%s)", self.service, gone)
            return None, ("The saved Google sign-in was unreadable and has been removed." if gone else
                          "The saved Google sign-in is unreadable and could not be removed; "
                          f"delete the '{self.service}' item in Keychain Access.")

    def save(self, creds: Credentials) -> None:
        self.backend.set_password(self.service, KEYRING_USER, creds.to_json())

    def delete(self) -> bool:
        """Delete the entry. True if it is gone afterwards (including when it never existed)."""
        try:
            self.backend.delete_password(self.service, KEYRING_USER)
        except keyring.errors.PasswordDeleteError:
            pass  # nothing stored
        except Exception as e:
            log.warning("keychain delete failed for %s: %s", self.service, type(e).__name__)
            return False
        return True


def granted(creds: Credentials) -> set[str]:
    return set(getattr(creds, "granted_scopes", None) or creds.scopes or [])


def check_scopes(creds: Credentials, role: str = READER) -> None:
    got = granted(creds)
    extra = got - ROLE_ALLOWED[role]
    if extra:
        raise AuthError(f"refusing token with unexpected scopes: {sorted(extra)}")
    if ROLE_REQUIRED[role] not in got:
        raise AuthError("Gmail read access was not granted" if role == READER else
                        "Gmail modify access was not granted, so cleanup stays off")


def dangerous(creds: Credentials) -> bool:
    """True if the token carries any scope Sift never asks for (full access, send, compose, ...)."""
    return bool(granted(creds) - KNOWN_SCOPES)


def fresh(creds: Credentials, store: TokenStore, lock: threading.Lock, role: str = READER) -> Credentials:
    """Refresh the access token if needed, re-check its scopes, and persist it.
    Raises ReauthRequired (CleanupReauthRequired for the writer) if the refresh token is dead or the refreshed
    token's scopes are no longer acceptable."""
    exc = CleanupReauthRequired if role == WRITER else ReauthRequired
    with lock:
        if creds.valid:
            return creds
        try:
            creds.refresh(Request())
        except RefreshError as e:  # invalid_grant: expired (Testing: 7 days) or revoked
            raise exc(REAUTH_MSG[role]) from e
        try:
            check_scopes(creds, role)
        except AuthError:
            # Never persist or use a token whose scopes drifted. Drop it; reconnecting issues a clean one.
            log.warning("refreshed %s token has unexpected scopes; discarding it", role)
            if dangerous(creds):
                revoke(creds)
            store.delete()
            raise exc(REAUTH_MSG[role]) from None
        store.save(creds)
        return creds


def run_consent_flow(client_secrets: Path, timeout_s: int = 300, role: str = READER) -> Credentials:
    """Open the browser for Google consent on a loopback redirect (PKCE is automatic). Blocks until done.
    role=WRITER asks for gmail.modify (the "Enable cleanup" consent)."""
    if not client_secrets.exists():
        raise AuthError(f"OAuth client file not found at {client_secrets} (see docs/google-setup.md)")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), scopes=list(ROLE_REQUESTED[role]))
    done = ("Sift is connected (read-only). You can close this tab." if role == READER else
            "Cleanup is enabled. Sift still changes nothing until you approve a plan. You can close this tab.")
    creds = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True, timeout_seconds=timeout_s,
                                  authorization_prompt_message="Opening Google sign-in in your browser…",
                                  success_message=done)
    try:
        check_scopes(creds, role)
    except AuthError:
        revoke(creds)  # don't leave a grant we refuse to use live at Google
        raise
    return creds


def credential_store_name() -> str:
    kr = keyring.get_keyring()
    return "macOS Keychain" if "macOS" in type(kr).__module__ else type(kr).__name__


def revoke(creds: Credentials, opener=urllib.request.urlopen) -> bool:
    """Revoke the grant at Google. Returns True if Google confirmed it.
    https://developers.google.com/identity/protocols/oauth2/native-app#tokenrevoke
    Note: Google ties tokens to the user's grant for the project, so revoking one Sift token can end the
    other Sift token too. Callers must cope with the reader needing a reconnect afterwards."""
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
