"""GmailReader: read-only Gmail access (gmail.readonly scope). Implements MailReader only.

Quota (verified 2026-11-03, developers.google.com/workspace/gmail/api/reference/quota):
6,000 units per user per minute; messages.get = 20 units (metadata format), messages.list = 5,
threads.get = 40, labels.get/list = 1, getProfile = 1. Batches: max 100 calls, Google recommends
<= 50. We throttle client-side to stay under the per-user limit and back off on 429/403-rate/5xx.

Never requests bodies (format=metadata / minimal only) and never calls anything that marks read.
"""
from __future__ import annotations

import email.utils
import logging
import socket
import threading
import time
from typing import Callable

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .base import MailReader, MessageMeta, Page, PermanentError, TransientError
from .gmail_auth import ReauthRequired, TokenStore, fresh

log = logging.getLogger("sift.gmail")

METADATA_HEADERS = ["From", "To", "Cc", "Bcc", "Subject", "List-Id", "List-Unsubscribe", "List-Unsubscribe-Post"]
MESSAGE_FIELDS = "id,threadId,labelIds,snippet,internalDate,payload/headers"
COST = {"get": 20, "list": 5, "thread": 40, "label": 1, "profile": 1}
BATCH = 50
RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}


class QuotaLimiter:
    """Token bucket over quota units. Default: 90% of Gmail's 6,000 units/user/minute."""

    def __init__(self, units_per_minute: float = 5400, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.rate = units_per_minute / 60.0
        # Burst allowance kept small so burst + one minute of refill stays under Gmail's 6,000/min.
        self.capacity = min(600.0, units_per_minute / 9)
        self.tokens = self.capacity
        self.clock, self.sleep = clock, sleep
        self.last = clock()
        self.lock = threading.Lock()

    def acquire(self, units: float) -> None:
        # A request larger than the bucket (e.g. a 50 x 20-unit batch) is paid for in bucket-sized parts,
        # otherwise it could never be admitted.
        while units > self.capacity:
            self._take(self.capacity)
            units -= self.capacity
        self._take(units)

    def _take(self, units: float) -> None:
        with self.lock:
            while True:
                now = self.clock()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= units - 1e-6:  # tolerance: float rounding must not stall the loop
                    self.tokens = max(0.0, self.tokens - units)
                    return
                self.sleep(max(0.01, (units - self.tokens) / self.rate))


def _classify(e: Exception) -> Exception:
    """Map client errors onto the provider error types the retry policy understands."""
    if isinstance(e, RefreshError):  # grant revoked/expired mid-request
        return ReauthRequired("Google sign-in expired; reconnect Gmail")
    if isinstance(e, HttpError):
        status = e.status_code
        reason = ""
        try:
            reason = e._get_reason() or ""
        except Exception:
            pass
        if status == 429 or status >= 500 or (status == 403 and any(r in str(e.content) for r in RATE_REASONS)):
            return TransientError(status, "Gmail rate limit or server error")
        return PermanentError(f"Gmail HTTP {status}: {reason[:120]}")
    if isinstance(e, (socket.timeout, TimeoutError, ConnectionError, OSError)):
        return TransientError(0, f"network: {type(e).__name__}")
    return e


def _parse(msg: dict) -> MessageMeta:
    headers = {}
    for h in msg.get("payload", {}).get("headers", []):
        headers.setdefault(h["name"].lower(), h["value"])
    name, addr = email.utils.parseaddr(headers.get("from", ""))
    recipients = [a for _, a in email.utils.getaddresses(
        [headers.get(k, "") for k in ("to", "cc", "bcc") if headers.get(k)]) if a]
    return MessageMeta(
        id=msg["id"], thread_id=msg.get("threadId", msg["id"]), from_addr=(addr or "unknown@unknown").lower(),
        from_name=name, to_addrs=recipients, subject=headers.get("subject", ""),
        snippet=(msg.get("snippet") or "")[:200], internal_date=int(msg.get("internalDate", 0)),
        labels=list(msg.get("labelIds", [])), list_id=headers.get("list-id"),
        list_unsubscribe=headers.get("list-unsubscribe"), list_unsubscribe_post=headers.get("list-unsubscribe-post"))


class GmailReader(MailReader):
    name = "gmail"
    synthetic = False

    def __init__(self, creds: Credentials, store: TokenStore, limiter: QuotaLimiter | None = None,
                 service_factory: Callable[[Credentials], object] | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_attempts: int = 6):
        self._creds = creds
        self._store = store
        self._creds_lock = threading.Lock()
        self.limiter = limiter or QuotaLimiter()
        self._factory = service_factory or (lambda c: build("gmail", "v1", credentials=c, cache_discovery=False))
        self._local = threading.local()  # httplib2 is not thread-safe: one service per thread
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._email: str | None = None

    # -- plumbing -------------------------------------------------------------
    def _svc(self):
        creds = fresh(self._creds, self._store, self._creds_lock)
        if getattr(self._local, "svc", None) is None:
            self._local.svc = self._factory(creds)
        return self._local.svc

    def _exec(self, request, cost: str):
        self.limiter.acquire(COST[cost])
        try:
            return request.execute(num_retries=0)
        except ReauthRequired:
            raise
        except Exception as e:
            raise _classify(e) from e

    # -- MailReader -----------------------------------------------------------
    def account_email(self) -> str:
        if self._email is None:
            self._email = self.profile()["email"]
        return self._email

    def profile(self) -> dict:
        p = self._exec(self._svc().users().getProfile(userId="me"), "profile")
        self._email = p["emailAddress"].lower()
        return {"email": self._email, "messages_total": p.get("messagesTotal"), "threads_total": p.get("threadsTotal")}

    def count_label(self, label: str) -> int:
        r = self._exec(self._svc().users().labels().get(userId="me", id=label), "label")
        return int(r.get("messagesTotal", 0))

    def label_names(self) -> dict[str, str]:
        r = self._exec(self._svc().users().labels().list(userId="me"), "label")
        return {l["id"]: l["name"] for l in r.get("labels", [])}

    def list_message_ids(self, label: str, page_token: str | None = None, max_results: int = 500) -> Page:
        r = self._exec(self._svc().users().messages().list(
            userId="me", labelIds=[label], maxResults=max_results, pageToken=page_token,
            includeSpamTrash=False, fields="messages/id,nextPageToken,resultSizeEstimate"), "list")
        return Page(ids=[m["id"] for m in r.get("messages", [])], next_page_token=r.get("nextPageToken"),
                    result_size_estimate=r.get("resultSizeEstimate"))

    def _batch(self, ids: list[str], make_request: Callable, cost: str) -> dict[str, dict]:
        """Run per-id GETs in HTTP batches, retrying only the sub-requests that hit rate limits.
        404s are dropped (message gone). Raises TransientError if retries run out."""
        results: dict[str, dict] = {}
        pending = list(dict.fromkeys(ids))
        for attempt in range(self._max_attempts):
            failed: list[str] = []
            fatal: list[Exception] = []
            for i in range(0, len(pending), BATCH):
                part = pending[i:i + BATCH]
                svc = self._svc()

                def cb(request_id, response, exception, _part=part):
                    if exception is None:
                        results[request_id] = response
                        return
                    err = _classify(exception)
                    if isinstance(exception, HttpError) and exception.status_code == 404:
                        return
                    if isinstance(err, TransientError):
                        failed.append(request_id)
                    else:
                        fatal.append(err)

                batch = svc.new_batch_http_request(callback=cb)
                for mid in part:
                    batch.add(make_request(svc, mid), request_id=mid)
                self.limiter.acquire(COST[cost] * len(part))
                try:
                    batch.execute()
                except Exception as e:
                    err = _classify(e)
                    if isinstance(err, TransientError):
                        failed.extend(m for m in part if m not in results)
                    else:
                        raise err from e
            if fatal:
                raise fatal[0]
            if not failed:
                return results
            pending = failed
            if attempt < self._max_attempts - 1:
                self._sleep(min(30.0, 0.5 * 2 ** attempt))
        raise TransientError(429, f"{len(pending)} requests still rate-limited after {self._max_attempts} attempts")

    def get_metadata(self, ids: list[str]) -> list[MessageMeta]:
        got = self._batch(ids, lambda svc, mid: svc.users().messages().get(
            userId="me", id=mid, format="metadata", metadataHeaders=METADATA_HEADERS, fields=MESSAGE_FIELDS), "get")
        return [_parse(got[i]) for i in ids if i in got]

    def threads_with_sent(self, thread_ids: list[str]) -> set[str]:
        got = self._batch(thread_ids, lambda svc, tid: svc.users().threads().get(
            userId="me", id=tid, format="minimal", fields="id,messages/labelIds"), "thread")
        return {tid for tid, t in got.items() if any("SENT" in m.get("labelIds", []) for m in t.get("messages", []))}
