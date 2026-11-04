"""GmailWriter: the only Gmail write access in Sift (gmail.modify scope). Implements MailWriter only.

Held exclusively by the Executor, and only exists once the user has clicked "Enable cleanup" (a separate
consent and a separate keychain token, see gmail_auth). Methods: create/find a label, batchModify (add/remove
labels, archive = remove INBOX), trash and untrash. There is deliberately no delete, batch delete, send, draft
or "empty Trash" method; gmail.modify could in principle send, so the absence of those calls (enforced by
.claude/hooks/guard.py and tests) is what keeps Sift from sending.

Each method makes exactly one API call (or one list + one create for a new label). Retrying is the caller's
job: the Executor wraps every call in RetryPolicy, which backs off exponentially with jitter on TransientError
(HTTP 429, 5xx, 403 rate-limit reasons, network errors).

Quota (https://developers.google.com/workspace/gmail/api/reference/quota, checked 2026-11-04):
messages.batchModify = 50 units, messages.trash = 20, messages.untrash = 5, labels.create = 5,
labels.list = 1; 6,000 units per user per minute shared with the reader, so pass the reader's QuotaLimiter.
batchModify: "There is a limit of 1000 ids per request."
(https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/batchModify)
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .base import MailWriter, PermanentError
from .gmail import QuotaLimiter, _classify
from .gmail_auth import WRITER, CleanupReauthRequired, ReauthRequired, REAUTH_MSG, TokenStore, fresh

log = logging.getLogger("sift.gmail.writer")

WRITE_COST = {"batch_modify": 50, "trash": 20, "untrash": 5, "label_create": 5, "label_list": 1, "profile": 1}
MAX_BATCH_MODIFY_IDS = 1000
# Labels that must not be set through batchModify: Trash goes through trash()/untrash(), and Sift never
# files anything as spam.
NOT_VIA_MODIFY = {"TRASH", "SPAM", "UNREAD"}  # UNREAD: Sift never marks mail read or unread


class GmailWriter(MailWriter):
    name = "gmail-writer"
    max_modify_batch = MAX_BATCH_MODIFY_IDS

    def __init__(self, creds: Credentials, store: TokenStore, limiter: QuotaLimiter | None = None,
                 service_factory: Callable[[Credentials], object] | None = None,
                 on_reauth: Callable[[], None] | None = None):
        self._creds = creds
        self._store = store
        self._creds_lock = threading.Lock()
        self.limiter = limiter or QuotaLimiter()
        self._factory = service_factory or (lambda c: build("gmail", "v1", credentials=c, cache_discovery=False))
        self._local = threading.local()
        self._on_reauth = on_reauth
        self._labels: dict[str, str] = {}  # casefolded name -> id

    # -- plumbing -------------------------------------------------------------
    def _reauth(self, e: Exception) -> CleanupReauthRequired:
        if self._on_reauth:
            try:
                self._on_reauth()
            except Exception:  # the hook must never mask the real error
                log.warning("cleanup reauth hook failed")
        return CleanupReauthRequired(REAUTH_MSG[WRITER])

    def _svc(self):
        try:
            creds = fresh(self._creds, self._store, self._creds_lock, role=WRITER)
        except ReauthRequired as e:
            raise self._reauth(e) from None
        if getattr(self._local, "svc", None) is None:
            self._local.svc = self._factory(creds)
        return self._local.svc

    def _exec(self, make_request: Callable[[object], object], cost: str):
        svc = self._svc()
        self.limiter.acquire(WRITE_COST[cost])
        try:
            return make_request(svc).execute(num_retries=0)
        except Exception as e:
            err = _classify(e)
            if isinstance(err, ReauthRequired):
                raise self._reauth(err) from None
            raise err from e

    # -- identity (used once, to check the cleanup consent is for the connected account) -------
    def account_email(self) -> str:
        p = self._exec(lambda s: s.users().getProfile(userId="me"), "profile")
        return p["emailAddress"].lower()

    # -- MailWriter -----------------------------------------------------------
    def _find_label(self, name: str) -> str | None:
        r = self._exec(lambda s: s.users().labels().list(userId="me"), "label_list")
        for lbl in r.get("labels", []):
            self._labels[lbl["name"].casefold()] = lbl["id"]
        return self._labels.get(name.casefold())

    def ensure_label(self, name: str) -> str:
        """Idempotent: returns the existing label's id if one with this name exists, else creates it.
        Safe to retry: a create that succeeded but whose response was lost comes back as 409 next time."""
        if not name or not name.strip():
            raise PermanentError("label name is empty")
        key = name.casefold()
        if key in self._labels:
            return self._labels[key]
        found = self._find_label(name)
        if found:
            return found
        body = {"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}
        try:
            r = self._exec(lambda s: s.users().labels().create(userId="me", body=body), "label_create")
        except PermanentError as e:
            cause = e.__cause__
            if isinstance(cause, HttpError) and cause.status_code == 409:  # created meanwhile
                found = self._find_label(name)
                if found:
                    return found
            raise
        self._labels[key] = r["id"]
        return r["id"]

    def batch_modify(self, ids: list[str], add: list[str], remove: list[str]) -> None:
        ids = list(dict.fromkeys(ids))
        if not ids:
            return
        if len(ids) > self.max_modify_batch:
            raise PermanentError(f"batchModify accepts at most {self.max_modify_batch} ids")
        if NOT_VIA_MODIFY & (set(add) | set(remove)):
            raise PermanentError("use trash()/untrash() for Trash; Sift never applies SPAM or changes UNREAD")
        if not add and not remove:
            raise PermanentError("batchModify needs at least one label to add or remove")
        body = {"ids": ids, "addLabelIds": list(add), "removeLabelIds": list(remove)}
        self._exec(lambda s: s.users().messages().batchModify(userId="me", body=body), "batch_modify")

    def trash(self, message_id: str) -> None:
        self._exec(lambda s: s.users().messages().trash(userId="me", id=message_id), "trash")

    def untrash(self, message_id: str) -> None:
        self._exec(lambda s: s.users().messages().untrash(userId="me", id=message_id), "untrash")
