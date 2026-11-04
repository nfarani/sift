"""FastAPI app. The UI reads every number from here; nothing is computed or invented client-side."""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .analysis import DEFAULT_OLDER_THAN, aggregate, apply_protection, build_recommendations, classify
from .classifier.base import CATEGORIES, KEEP, Classifier
from .classifier.rules import RuleOnlyClassifier
from .config import Settings
from .db import Database, now_iso
from .executor import ExecutionError, Executor, unlocked
from .jobs import JobRunner, recover_interrupted
from .plans import PlanError, PlanItem, approve_plan, create_plan, get_plan
from .providers.base import MailReader, MailWriter, ProviderError
from .providers.gmail_auth import (WRITER, AuthError, ReauthRequired, TokenStore, check_scopes,
                                   credential_store_name, dangerous, revoke, run_consent_flow)
from . import unsubscribe as unsub
from .providers.mock import MockProvider
from .protection import RULES_VERSION
from .ai import run as ai_run
from .ai.ledger import Ledger, SpendCapReached
from .ai.planner import PlannerError, QueryPlanner
from .classifier.claude import ClaudeClassifier
from .query import SpecError, parse_spec
from .query import preview as query_preview
from .retry import RetryPolicy
from .scanner import Scanner

log = logging.getLogger("sift")


@dataclass
class State:
    settings: Settings
    db: Database
    provider: MailReader | None      # read access (scanner, analysis, revalidation)
    writer: MailWriter | None        # write access: only ever passed to the Executor
    classifier: Classifier
    scanner: Scanner | None
    executor: Executor
    jobs: JobRunner
    retry: RetryPolicy
    token_store: TokenStore | None = None
    reader_factory: object = None
    ledger: Ledger | None = None
    claude: ClaudeClassifier | None = None
    planner: QueryPlanner | None = None
    ai_unavailable_reason: str | None = None
    connection: dict = field(default_factory=lambda: {"state": "disconnected", "error": None})
    # Gmail cleanup (gmail.modify): a separate writer token in its own keychain entry (PROGRESS item 8).
    writer_store: TokenStore | None = None
    writer_factory: object = None
    cleanup: dict = field(default_factory=lambda: {"state": "disabled", "error": None})
    unsub_poster: object = None

    def attach(self, reader: MailReader | None, writer: MailWriter | None) -> None:
        self.provider, self.writer = reader, writer
        self.scanner = Scanner(self.db, reader, self.classifier, self.retry) if reader else None
        self.executor = Executor(self.db, reader, writer, self.settings.first_run_cap, self.retry)

    def detach_writer(self, cleanup_state: str = "disabled", error: str | None = None) -> None:
        """Drop write access only; the read-only connection (and any scan in progress) is untouched."""
        self.writer = None
        self.executor = Executor(self.db, self.provider, None, self.settings.first_run_cap, self.retry)
        self.cleanup = {"state": cleanup_state, "error": error}

    def writer_reauth(self) -> None:
        """Called by GmailWriter when its refresh token is dead (Testing mode: 7 days) or its scopes drifted.
        Surfaces as "reconnect cleanup"; never crashes the app."""
        if self.writer_store is not None:
            self.writer_store.delete()
        self.detach_writer("reauth_required", "Cleanup permission expired; reconnect cleanup")

    def make_writer(self, creds) -> MailWriter:
        return self.writer_factory(creds, self.writer_store, getattr(self.provider, "limiter", None),
                                   self.writer_reauth)


class PlanItemIn(BaseModel):
    rec_id: str
    action: str = "archive"
    older_than_days: int = Field(0, ge=0, le=36500)
    excluded_senders: list[str] = []


class PlanIn(BaseModel):
    items: list[PlanItemIn]


class ApproveIn(BaseModel):
    confirm_hash: str


class PreviewIn(BaseModel):
    older_than_days: int = Field(0, ge=0, le=36500)
    excluded_senders: list[str] = []


class ScanIn(BaseModel):
    max_messages: int | None = Field(None, ge=1, le=1_000_000)  # test scan: cap Sent and Inbox each


class AiRunIn(BaseModel):
    within_cap: bool = False          # run biggest groups first and stop at the cap
    approved_estimate_usd: float      # what the user saw and approved


class QueryIn(BaseModel):
    text: str


class SpecIn(BaseModel):
    spec: dict


class RuleIn(BaseModel):
    kind: str
    value: str


class UnsubBatchIn(BaseModel):
    senders: list[str] = Field(..., min_length=1, max_length=unsub.MAX_BATCH_SENDERS)


def build_state(settings: Settings, provider: MailReader | None = None, retry: RetryPolicy | None = None,
                token_store: TokenStore | None = None, reader_factory=None, ai_client=None,
                writer_factory=None, unsub_poster=None) -> State:
    db = Database(settings.app_db)
    retry = retry or RetryPolicy(settings.max_attempts, settings.backoff_base, settings.backoff_max)
    classifier = RuleOnlyClassifier()
    state = State(settings, db, None, None, classifier, None, Executor(db, None, None), JobRunner(db), retry)
    state.ledger = Ledger(db, settings.spend_cap_usd)
    _attach_ai(state, ai_client)
    if provider is None and settings.provider == "mock":
        provider = MockProvider(settings.mock_db, size=settings.mock_size, seed=settings.mock_seed,
                                latency_ms=settings.mock_latency_ms)
    if provider is not None:
        # The mock can write to its own synthetic mailbox; a real reader never doubles as a writer.
        state.attach(provider, provider if isinstance(provider, MockProvider) else None)
        state.connection = {"state": "connected", "error": None}
        return state
    if settings.provider != "gmail":
        raise RuntimeError(f"unknown provider {settings.provider!r}")
    state.token_store = token_store or TokenStore()
    state.writer_store = state.token_store.for_writer()
    state.reader_factory = reader_factory or _gmail_reader
    state.writer_factory = writer_factory or _gmail_writer
    state.unsub_poster = unsub_poster
    # A corrupt keychain entry is treated as missing: deleted and reported, never a startup crash (item 12).
    creds, problem = state.token_store.load_or_discard()
    if problem:
        state.connection = {"state": "disconnected", "error": problem}
    if creds is not None:
        try:
            check_scopes(creds)
            state.attach(state.reader_factory(creds, state.token_store), None)  # read-only until the writer loads
            state.connection = {"state": "connected", "error": None}
        except AuthError as e:
            state.connection = {"state": "error", "error": str(e)}
    _load_writer(state)
    return state


def _load_writer(state: State) -> None:
    """Attach the cleanup writer at startup only if the reader is connected and the writer token exists and
    carries exactly the allowed scopes. A bad token is deleted (and revoked if it carries scopes Sift never
    asks for). No network call here: a dead refresh token surfaces on first use as "reconnect cleanup"."""
    wcreds, problem = state.writer_store.load_or_discard()
    if problem:
        state.cleanup = {"state": "error", "error": problem.replace("Google sign-in", "cleanup permission")}
        return
    if wcreds is None or state.provider is None or state.connection["state"] != "connected":
        return  # a writer without a working reader stays stored but unused; disconnect deletes it
    try:
        check_scopes(wcreds, WRITER)
    except AuthError as e:
        if dangerous(wcreds):
            revoke(wcreds)
        state.writer_store.delete()
        state.cleanup = {"state": "error", "error": f"{e}. The cleanup token was removed; enable cleanup again."}
        return
    state.attach(state.provider, state.make_writer(wcreds))
    state.cleanup = {"state": "enabled", "error": None}
    if not state.db.get_setting("cleanup.enabled_at"):
        state.db.set_setting("cleanup.enabled_at", now_iso())  # unknown grant time: earlier approvals are void


def public_error(e: Exception) -> str:
    """Text safe to show in the UI. Our own error types carry fixed, authored messages; anything else
    (library or network exceptions) is reduced to a fixed sentence plus its type name."""
    if isinstance(e, (AuthError, ProviderError)):
        return str(e)[:300]
    return f"Could not connect to Gmail ({type(e).__name__}). Check your network and try again."


def _attach_ai(state: State, client=None) -> None:
    """Claude is optional. Without a key the app runs rules-only and says so; it never fakes AI output."""
    s = state.settings
    if client is None:
        if not s.anthropic_key_set:
            state.ai_unavailable_reason = "ANTHROPIC_API_KEY is not set in .env"
            return
        import anthropic
        client = anthropic.Anthropic(max_retries=3)
    try:
        state.claude = ClaudeClassifier(client, state.ledger, s.model_bulk, s.model_review)
        state.planner = QueryPlanner(client, state.ledger, s.model_review)
        from .ai.ledger import price
        price(s.model_bulk), price(s.model_review)  # refuse unknown models up front
        state.ai_unavailable_reason = None
    except ValueError as e:
        state.claude = state.planner = None
        state.ai_unavailable_reason = str(e)


def _gmail_reader(creds, store):
    from .providers.gmail import GmailReader
    return GmailReader(creds, store)


def _gmail_writer(creds, store, limiter, on_reauth):
    # Shares the reader's QuotaLimiter: Gmail's 6,000 units/minute is per user, not per token.
    from .providers.gmail_writer import GmailWriter
    return GmailWriter(creds, store, limiter=limiter, on_reauth=on_reauth)


def _ms_to_iso(ms):
    return None if ms is None else time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def create_app(state: State) -> FastAPI:
    app = FastAPI(title="Sift", version="0.1.0")
    app.state.sift = state
    allowed_hosts = state.settings.allowed_hosts
    allowed_origins = state.settings.allowed_origins

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        """Block other websites and DNS-rebinding pages from driving this local API.
        Every request needs a local Host; every state-changing request also needs the custom
        X-Sift header (which a cross-site page cannot send without a CORS preflight we never allow)
        and, if the browser sent one, a local Origin."""
        if request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if request.headers.get("x-sift") != "1" or (origin and origin not in allowed_origins):
                return JSONResponse({"detail": "forbidden: cross-site request"}, status_code=403)
        return await call_next(request)
    db = state.db
    recover_interrupted(db)

    def bad(e: Exception, code: int = 400):
        raise HTTPException(code, str(e))

    def need_reader() -> MailReader:
        if state.provider is None:
            bad("Gmail is not connected", 409)
        return state.provider

    def account() -> str | None:
        """Connected account address. Cached locally so status checks don't spend Gmail quota."""
        if state.provider is None:
            return None
        cached = db.get_setting("account.email")
        if cached:
            return cached
        try:
            email = state.provider.account_email()
        except ReauthRequired as e:
            state.connection = {"state": "reauth_required", "error": str(e)}
            return None
        except ProviderError as e:
            state.connection = {**state.connection, "error": str(e)}
            return None
        db.set_setting("account.email", email)
        return email

    def classifier_info() -> dict:
        with db.connect() as c:
            n_ai = c.execute("SELECT COUNT(*) FROM sender_groups WHERE classifier LIKE 'claude%'").fetchone()[0]
        if n_ai:
            return {"name": "rules+claude", "label": "Rules + Claude", "uses_ai": True, "ai_groups": n_ai}
        label = "Rules only, no AI" if state.claude is None else "Rules only (Claude not run yet)"
        return {"name": "rules", "label": label, "uses_ai": False, "ai_groups": 0}

    # -- status / dashboard ---------------------------------------------------
    @app.get("/api/status")
    def status():
        s = state.settings.public_status()
        p = state.provider
        synthetic = bool(p and p.synthetic)
        if synthetic:
            mode = "synthetic mailbox: approved plans change synthetic data only"
        elif state.writer is None:
            mode = "read-only (gmail.readonly): Sift cannot change your mail"
            if state.cleanup["state"] == "reauth_required":
                mode += ". Cleanup permission expired: reconnect cleanup"
        else:
            mode = "cleanup enabled (gmail.modify): changes still need an approved plan"
        # Reported from what is actually attached, so it can't claim write access Sift doesn't hold.
        if synthetic or state.writer is not None:
            cleanup = {"state": "enabled", "error": None}
        elif state.cleanup["state"] == "enabled":
            cleanup = {"state": "disabled", "error": None}
        else:
            cleanup = dict(state.cleanup)
        return {**s, "account": account(), "provider_name": p.name if p else s["provider"],
                "synthetic": synthetic, "connection": state.connection,
                "can_modify": state.writer is not None, "cleanup": cleanup,
                "credential_store": credential_store_name() if state.token_store else None,
                "classifier": classifier_info(),
                "ai": {"available": state.claude is not None, "reason": state.ai_unavailable_reason},
                "mode": mode, "full_runs_unlocked": unlocked(db), "busy": state.jobs.busy()}

    # -- Gmail connection (read-only) -----------------------------------------
    connect_lock = threading.Lock()

    @app.post("/api/gmail/connect")
    def gmail_connect():
        if state.token_store is None:
            bad("not running in Gmail mode (set SIFT_PROVIDER=gmail)", 404)
        if not state.settings.client_secrets.exists():
            bad(f"OAuth client file not found at {state.settings.client_secrets}. See docs/google-setup.md.")
        if state.jobs.busy():
            bad("wait for the current job to finish before reconnecting", 409)

        def flow():
            try:
                creds = run_consent_flow(state.settings.client_secrets)
                old, _ = state.token_store.load_or_discard()
                previous = db.get_setting("account.email")
                reader = state.reader_factory(creds, state.token_store)
                new_email = reader.account_email()
                switched = bool(previous and previous != new_email)
                if switched:
                    if state.jobs.busy():
                        raise AuthError("a scan is running for the previous account; cancel it and reconnect")
                    db.wipe_analysis()  # never show one account's data under another
                state.token_store.save(creds)
                if switched and old is not None:
                    # Only for a different account: revoking the same account's grant would kill the new token.
                    revoke(old)
                state.attach(reader, None)  # read-only; the writer is re-attached below only for the same account
                db.set_setting("account.email", new_email)
                _reattach_writer(new_email, switched)
                state.connection = {"state": "connected", "error": None}
            except Exception as e:  # message only, never tokens
                log.warning("gmail connect failed: %s", type(e).__name__)
                state.connection = {"state": "error", "error": public_error(e)}

        with connect_lock:
            if state.connection["state"] == "connecting":
                return status()
            state.connection = {"state": "connecting", "error": None}
            threading.Thread(target=flow, name="sift-oauth", daemon=True).start()
        return status()

    def _drop_writer_token() -> tuple[bool, bool, str | None]:
        """Revoke and delete the stored writer token. Returns (revoked_at_google, deleted, problem)."""
        wcreds, problem = state.writer_store.load_or_discard()
        revoked = revoke(wcreds) if wcreds else False
        return revoked, state.writer_store.delete(), problem

    def _candidate_writer(creds) -> MailWriter:
        """A writer used only to check which account a new/stored cleanup token belongs to. It has no reauth
        hook, so a failure here can't delete or detach the cleanup token that is currently in use."""
        return state.writer_factory(creds, state.writer_store, getattr(state.provider, "limiter", None), None)

    def _reattach_writer(email: str, switched: bool) -> None:
        """After a (re)connect: keep cleanup only if its token belongs to this same account."""
        if switched:
            _drop_writer_token()
            state.detach_writer()
            return
        wcreds, problem = state.writer_store.load_or_discard()
        if wcreds is None:
            state.detach_writer("error" if problem else "disabled", problem)
            return
        try:
            check_scopes(wcreds, WRITER)
            if _candidate_writer(wcreds).account_email() != email:
                raise AuthError("cleanup token belongs to another account")
        except ReauthRequired:
            state.writer_store.delete()
            state.detach_writer("reauth_required", "Cleanup permission expired; reconnect cleanup")
            return
        except (AuthError, ProviderError):
            revoke(wcreds)
            state.writer_store.delete()
            state.detach_writer("disabled")
            return
        state.attach(state.provider, state.make_writer(wcreds))
        state.cleanup = {"state": "enabled", "error": None}

    @app.post("/api/gmail/disconnect")
    def gmail_disconnect():
        if state.token_store is None:
            bad("not running in Gmail mode", 404)
        if state.jobs.busy():
            bad("wait for the current job to finish (or cancel it) before disconnecting", 409)
        # Never let an unreadable keychain entry stop a disconnect (item 12): it is deleted and reported.
        creds, problem = state.token_store.load_or_discard()
        revoked = revoke(creds) if creds else False
        deleted = state.token_store.delete()
        w_revoked, w_deleted, w_problem = _drop_writer_token()
        state.attach(None, None)
        state.cleanup = {"state": "disabled", "error": None}
        db.wipe_analysis()  # scanned metadata from this account; the audit log is kept
        unsub.discard_batches(db)  # they hold one-click URLs; the unsubscribe log is kept
        db.set_setting("account.email", None)
        state.connection = {"state": "disconnected", "error": None}
        return {"revoked_at_google": revoked, "local_token_deleted": deleted, "local_metadata_deleted": True,
                "cleanup_revoked_at_google": w_revoked, "cleanup_token_deleted": w_deleted,
                "problems": [p for p in (problem, w_problem) if p]}

    # -- Gmail cleanup (gmail.modify) -----------------------------------------
    cleanup_lock = threading.Lock()

    @app.post("/api/cleanup/enable")
    def cleanup_enable():
        """Start the separate "Enable cleanup" consent (gmail.modify). Runs in the background like connect;
        poll /api/status -> cleanup.state (enabling | enabled | error)."""
        if state.token_store is None:
            bad("not running in Gmail mode (the synthetic mailbox needs no permission)", 404)
        if state.provider is None or state.connection["state"] != "connected":
            bad("connect Gmail (read-only) first", 409)
        if not state.settings.client_secrets.exists():
            bad(f"OAuth client file not found at {state.settings.client_secrets}. See docs/google-setup.md.")
        if state.jobs.busy():
            bad("wait for the current job to finish before enabling cleanup", 409)
        expected = account()
        if not expected:
            bad("could not confirm which account is connected; reconnect Gmail", 409)

        def flow():
            try:
                creds = run_consent_flow(state.settings.client_secrets, role=WRITER)  # refuses+revokes bad scopes
                got = _candidate_writer(creds).account_email()
                if got != expected:
                    revoke(creds)
                    raise AuthError("You approved cleanup for a different Google account than the one connected. "
                                    "Nothing was saved; enable cleanup again with the connected account.")
                state.writer_store.save(creds)
                db.set_setting("cleanup.enabled_at", now_iso())  # plans approved before this need re-approval
                state.attach(state.provider, state.make_writer(creds))
                state.cleanup = {"state": "enabled", "error": None}
            except Exception as e:  # message only, never tokens
                log.warning("enable cleanup failed: %s", type(e).__name__)
                state.cleanup = {"state": "error", "error": public_error(e)}

        with cleanup_lock:
            if state.cleanup["state"] == "enabling":
                return status()
            state.cleanup = {"state": "enabling", "error": None}
            threading.Thread(target=flow, name="sift-oauth-cleanup", daemon=True).start()
        return status()

    @app.post("/api/cleanup/disable")
    def cleanup_disable():
        """Revoke and delete only the writer token. The read-only connection stays."""
        if state.token_store is None:
            bad("not running in Gmail mode", 404)
        if state.jobs.busy():
            bad("wait for the current job to finish (or cancel it) before disabling cleanup", 409)
        revoked, deleted, problem = _drop_writer_token()
        state.detach_writer()
        return {"revoked_at_google": revoked, "local_writer_token_deleted": deleted,
                "problems": [problem] if problem else [],
                "note": "Google may end Sift's read-only sign-in too when a grant is revoked. "
                        "If so, Sift will ask you to reconnect Gmail.", "status": status()}

    # -- unsubscribe (RFC 8058 one-click, separate approval per batch) --------
    def need_cleanup():
        if state.writer is None:
            bad("enable cleanup first: unsubscribing needs the same explicit permission as changing mail", 409)

    def _poster():
        if state.provider is not None and state.provider.synthetic:
            return unsub.SimulatedPoster()  # synthetic senders: never contact a real host
        return state.unsub_poster or unsub.OneClickPoster()

    def _unsub(fn, *a):
        try:
            return fn(*a)
        except unsub.UnsubscribeError as e:
            bad(e)

    @app.post("/api/unsubscribe/batches")
    def unsub_create(body: UnsubBatchIn):
        """Freeze the chosen sender groups into a batch. Sends nothing."""
        reader = need_reader()
        need_cleanup()
        user = account() or reader.account_email()
        try:
            return unsub.create_batch(db, reader, body.senders, user)
        except unsub.UnsubscribeError as e:
            bad(e)
        except ProviderError as e:
            bad(public_error(e), 502)

    @app.get("/api/unsubscribe/batches/{batch_id}")
    def unsub_get(batch_id: int):
        try:
            return unsub.get_batch(db, batch_id)
        except unsub.UnsubscribeError as e:
            bad(e, 404)

    @app.post("/api/unsubscribe/batches/{batch_id}/approve")
    def unsub_approve(batch_id: int, body: ApproveIn):
        need_cleanup()
        return _unsub(unsub.approve_batch, db, batch_id, body.confirm_hash)

    @app.post("/api/unsubscribe/batches/{batch_id}/cancel")
    def unsub_cancel(batch_id: int):
        return _unsub(unsub.cancel_batch, db, batch_id)

    @app.post("/api/unsubscribe/batches/{batch_id}/execute")
    def unsub_execute(batch_id: int):
        need_cleanup()
        if state.jobs.busy():
            bad("another job is running", 409)
        synthetic = bool(state.provider and state.provider.synthetic)
        # Gmail: the batch must have been approved after cleanup was last enabled. Synthetic: no grant exists.
        enabled_at = "" if synthetic else db.get_setting("cleanup.enabled_at")
        _unsub(unsub.start_batch, db, batch_id, enabled_at)
        poster = _poster()

        def crash(msg):
            with db.connect() as c:
                c.execute("UPDATE unsubscribe_batches SET status='failed', error=?, finished_at=? "
                          "WHERE id=? AND status='running'", ("stopped unexpectedly", now_iso(), batch_id))

        state.jobs.submit("unsubscribe", lambda: unsub.run_batch(db, batch_id, poster), crash)
        return unsub.get_batch(db, batch_id)

    @app.get("/api/unsubscribe/log")
    def unsub_log(limit: int = 200):
        with db.connect() as c:
            return [dict(r) for r in c.execute(
                "SELECT ts, batch_id, sender, method, url_host, status_code, outcome, detail FROM unsubscribe_log "
                "ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),))]

    @app.get("/api/dashboard")
    def dashboard():
        with db.connect() as c:
            one = lambda q, *a: c.execute(q, a).fetchone()[0]
            total = one("SELECT COUNT(*) FROM messages WHERE is_sent=0")
            inbox = one("SELECT COUNT(*) FROM messages WHERE is_sent=0 AND in_inbox=1")
            unread = one("SELECT COUNT(*) FROM messages WHERE is_sent=0 AND in_inbox=1 AND is_unread=1")
            promos = one("SELECT COUNT(*) FROM messages WHERE is_sent=0 AND in_inbox=1 "
                         "AND labels LIKE '%CATEGORY_PROMOTIONS%'")
            subs = one("SELECT COUNT(*) FROM sender_groups WHERE list_id IS NOT NULL OR has_unsub=1")
            protected = one("SELECT COUNT(*) FROM messages WHERE is_sent=0 AND in_inbox=1 AND protected=1")
            sent = one("SELECT COUNT(*) FROM messages WHERE is_sent=1")
            cats = [dict(r) for r in c.execute(
                "SELECT category, COUNT(*) total, SUM(in_inbox) inbox, SUM(is_unread) unread, SUM(protected) protected "
                "FROM messages WHERE is_sent=0 AND category IS NOT NULL GROUP BY category ORDER BY total DESC")]
            top = [dict(r) for r in c.execute(
                "SELECT sender, display_name, unread, total, category FROM sender_groups ORDER BY unread DESC LIMIT 8")]
            now_ms = int(time.time() * 1000)
            recommended = 0
            for r in c.execute("SELECT id, older_than_days FROM recommendations WHERE action!='review'"):
                recommended += one("SELECT COUNT(*) FROM messages WHERE is_sent=0 AND in_inbox=1 AND protected=0 "
                                   "AND category=? AND internal_date < ?", r["id"], now_ms - r["older_than_days"] * 86_400_000)
            scan = c.execute("SELECT id, status, finished_at FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        for cat in cats:
            cat["label"] = CATEGORIES.get(cat["category"], cat["category"])
            cat["keep"] = cat["category"] in KEEP
        return {"account": account(), "synthetic": bool(state.provider and state.provider.synthetic),
                "scanned": total, "sent_scanned": sent, "inbox": inbox, "unread": unread, "promotional": promos,
                "subscriptions": subs, "protected": protected, "recommended": recommended,
                "impact_pct": round(100 * recommended / inbox, 1) if inbox else 0,
                "categories": cats, "top_unread_senders": top, "last_scan": dict(scan) if scan else None}

    # -- scan -----------------------------------------------------------------
    def _scan_row(scan_id=None):
        with db.connect() as c:
            r = (c.execute("SELECT * FROM scans WHERE id=?", (scan_id,)) if scan_id else
                 c.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1")).fetchone()
        if not r:
            return None
        d = dict(r)
        d["error_log"] = json.loads(d["error_log"])
        d["checkpoint"] = json.loads(d["checkpoint"])
        d["resumable"] = d["status"] in ("cancelled", "failed", "interrupted")
        return d

    def _scan_crash(scan_id):
        def f(msg):
            with db.connect() as c:
                c.execute("UPDATE scans SET status='failed', updated_at=? WHERE id=?", (now_iso(), scan_id))
            if state.scanner:
                state.scanner._error(scan_id, msg)
            if "ReauthRequired" in msg:
                state.connection = {"state": "reauth_required", "error": "Google sign-in expired; reconnect Gmail"}
        return f

    @app.get("/api/scan")
    def scan_status():
        return _scan_row() or {"status": "none"}

    @app.post("/api/scan")
    def scan_start(body: ScanIn | None = None):
        need_reader()
        if state.connection["state"] == "connecting":
            bad("finish connecting Gmail first", 409)
        if state.jobs.busy():
            bad("another job is running", 409)
        sid = state.scanner.start(max_per_phase=body.max_messages if body else None)
        state.jobs.submit("scan", lambda: state.scanner.run(sid), _scan_crash(sid))
        return _scan_row(sid)

    @app.post("/api/scan/resume")
    def scan_resume():
        need_reader()
        if state.connection["state"] == "connecting":
            bad("finish connecting Gmail first", 409)
        if state.jobs.busy():
            bad("another job is running", 409)
        r = state.scanner.resumable()
        if not r:
            bad("no interrupted scan to resume")
        state.scanner.prepare_resume(r["id"])
        state.jobs.submit("scan", lambda: state.scanner.run(r["id"]), _scan_crash(r["id"]))
        return _scan_row(r["id"])

    @app.post("/api/scan/cancel")
    def scan_cancel():
        r = _scan_row()
        if not r or r["status"] != "running":
            bad("no scan is running")
        need_reader()
        state.scanner.request_cancel(r["id"])
        return _scan_row(r["id"])

    # -- recommendations ------------------------------------------------------
    def _spec_rec_detail(rec, older_than=None, excluded=()):
        """Custom recommendation from a saved natural-language query: same deterministic filter as the plan."""
        spec = parse_spec(json.loads(rec["spec"]))
        older = rec["older_than_days"] if older_than is None else older_than
        excl = tuple(e.lower() for e in excluded)
        pv = query_preview(db, spec, extra_older_than_days=older)
        pv_ex = query_preview(db, spec, excluded_senders=excl, extra_older_than_days=older) if excl else pv
        with db.connect() as c:
            inbox = c.execute("SELECT COUNT(*) FROM messages WHERE is_sent=0 AND in_inbox=1").fetchone()[0]
        senders = [{"sender": x["sender"], "display_name": x["display_name"], "total": x["n"], "in_inbox": x["n"],
                    "unread": x["unread"], "eligible": x["n"], "protected_in_inbox": None, "excluded": x["sender"] in excl,
                    "first_date": _ms_to_iso(x["first"]), "last_date": _ms_to_iso(x["last"]), "list_id": None,
                    "has_unsub": None, "one_click_unsub": None, "confidence": None, "reason": None} for x in pv["senders"]]
        return {**dict(rec), "spec": spec.to_dict(), "category_label": "Your request", "older_than_days": older,
                "count": pv_ex["count"], "sender_count": len(senders), "senders": senders,
                "exceptions": [{"reason": "protected (receipts, bookings, security, starred, your threads)",
                                "count": pv["protected_excluded"]}] if pv["protected_excluded"] else [],
                "protected_count": pv["protected_excluded"], "examples": pv_ex["samples"][:5], "protected_examples": [],
                "date_range": [_ms_to_iso(pv_ex["date_range"][0]), _ms_to_iso(pv_ex["date_range"][1])],
                "impact_pct": round(100 * pv_ex["count"] / inbox, 1) if inbox else 0,
                "unsubscribe_one_click": 0, "unsubscribe_manual": 0}

    def _rec_detail(c, rec, older_than=None, excluded=()):
        if rec["spec"]:
            return _spec_rec_detail(rec, older_than, excluded)
        older = rec["older_than_days"] if older_than is None else older_than
        cutoff = int(time.time() * 1000) - older * 86_400_000
        excl = [e.lower() for e in excluded]
        senders = []
        for s in c.execute(
            "SELECT sender, display_name, total, in_inbox, unread, protected, first_date, last_date, list_id, "
            "has_unsub, one_click_unsub, confidence, reason, classifier FROM sender_groups WHERE category=? ORDER BY total DESC",
            (rec["id"],)):
            eligible = c.execute("SELECT COUNT(*) FROM messages WHERE from_addr=? AND is_sent=0 AND in_inbox=1 "
                                 "AND protected=0 AND internal_date < ?", (s["sender"], cutoff)).fetchone()[0]
            prot = c.execute("SELECT COUNT(*) FROM messages WHERE from_addr=? AND is_sent=0 AND in_inbox=1 "
                             "AND protected=1", (s["sender"],)).fetchone()[0]
            d = dict(s)
            d.update(eligible=eligible, protected_in_inbox=prot, excluded=s["sender"] in excl,
                     first_date=_ms_to_iso(s["first_date"]), last_date=_ms_to_iso(s["last_date"]))
            senders.append(d)
        count = sum(s["eligible"] for s in senders if not s["excluded"])
        exceptions = [{"reason": r[0], "count": r[1]} for r in c.execute(
            "SELECT protect_reason, COUNT(*) FROM messages WHERE category=? AND is_sent=0 AND in_inbox=1 AND protected=1 "
            "GROUP BY protect_reason ORDER BY 2 DESC", (rec["id"],))]
        examples = [dict(r) for r in c.execute(
            "SELECT id, from_addr, from_name, subject, internal_date FROM messages WHERE category=? AND is_sent=0 "
            "AND in_inbox=1 AND protected=0 ORDER BY id LIMIT 5", (rec["id"],))]
        protected_examples = [dict(r) for r in c.execute(
            "SELECT id, from_addr, from_name, subject, internal_date, protect_reason FROM messages WHERE category=? "
            "AND is_sent=0 AND in_inbox=1 AND protected=1 ORDER BY id LIMIT 3", (rec["id"],))]
        dr = c.execute("SELECT MIN(internal_date), MAX(internal_date) FROM messages WHERE category=? AND is_sent=0 "
                       "AND in_inbox=1 AND protected=0 AND internal_date < ?", (rec["id"], cutoff)).fetchone()
        inbox = c.execute("SELECT COUNT(*) FROM messages WHERE is_sent=0 AND in_inbox=1").fetchone()[0]
        return {**dict(rec), "category_label": CATEGORIES.get(rec["category"]), "older_than_days": older,
                "count": count, "sender_count": len(senders), "senders": senders, "exceptions": exceptions,
                "protected_count": sum(e["count"] for e in exceptions), "examples": examples,
                "protected_examples": protected_examples,
                "date_range": [_ms_to_iso(dr[0]), _ms_to_iso(dr[1])],
                "impact_pct": round(100 * count / inbox, 1) if inbox else 0,
                "unsubscribe_one_click": sum(1 for s in senders if s["one_click_unsub"]),
                "unsubscribe_manual": sum(1 for s in senders if s["has_unsub"] and not s["one_click_unsub"])}

    @app.get("/api/recommendations")
    def recommendations():
        with db.connect() as c:
            recs = c.execute("SELECT * FROM recommendations ORDER BY action='review', id").fetchall()
            out = [_rec_detail(c, r) for r in recs]
        out.sort(key=lambda r: (r["action"] == "review", -r["count"]))
        return {"classifier": classifier_info(), "defaults": DEFAULT_OLDER_THAN, "items": out}

    @app.post("/api/recommendations/{rec_id}/preview")
    def preview(rec_id: str, body: PreviewIn):
        with db.connect() as c:
            rec = c.execute("SELECT * FROM recommendations WHERE id=?", (rec_id,)).fetchone()
            if not rec:
                bad("unknown recommendation", 404)
            return _rec_detail(c, rec, body.older_than_days, body.excluded_senders)

    @app.delete("/api/recommendations/{rec_id}")
    def delete_rec(rec_id: str):
        with db.connect() as c:
            n = c.execute("DELETE FROM recommendations WHERE id=? AND source='query'", (rec_id,)).rowcount
        if not n:
            bad("only saved requests can be removed", 404)
        return {"ok": True}

    # -- Claude: classification -----------------------------------------------
    def need_ai():
        if state.claude is None:
            bad(f"Claude is not available: {state.ai_unavailable_reason}", 409)

    def _ai_run_row(run_id=None):
        with db.connect() as c:
            r = (c.execute("SELECT * FROM ai_runs WHERE id=?", (run_id,)) if run_id else
                 c.execute("SELECT * FROM ai_runs ORDER BY id DESC LIMIT 1")).fetchone()
        if not r:
            return None
        d = dict(r)
        d["estimate"] = json.loads(d["estimate"])
        return d

    @app.get("/api/ai")
    def ai_status():
        with db.connect() as c:
            labelled = c.execute("SELECT COUNT(*) FROM ai_labels").fetchone()[0]
        return {"available": state.claude is not None, "reason": state.ai_unavailable_reason,
                "models": {"bulk": state.settings.model_bulk, "review": state.settings.model_review},
                "cap_usd": state.settings.spend_cap_usd, "usage": state.ledger.totals(),
                "labelled_senders": labelled, "last_run": _ai_run_row(), "classifier": classifier_info()}

    @app.post("/api/ai/estimate")
    def ai_estimate():
        """Local estimate only. Nothing is sent to Anthropic."""
        need_ai()
        need_reader()
        return ai_run.estimate(db, state.claude, state.settings.spend_cap_usd)

    @app.post("/api/ai/run")
    def ai_start(body: AiRunIn):
        need_ai()
        need_reader()
        if state.jobs.busy():
            bad("another job is running", 409)
        est = ai_run.estimate(db, state.claude, state.settings.spend_cap_usd)
        if est["groups"] == 0:
            bad("nothing to classify: every sender is settled by rules or already labelled")
        if est["usd"] > state.settings.spend_cap_usd and not body.within_cap:
            bad(f"estimated ${est['usd']:.2f} exceeds your ${state.settings.spend_cap_usd:.2f} cap; "
                "run within the cap or raise SIFT_AI_SPEND_CAP_USD in .env")
        # The approval must match what the user saw (the estimate can move if data changed).
        shown = min(est["usd"], state.settings.spend_cap_usd) if body.within_cap else est["usd"]
        if abs(body.approved_estimate_usd - shown) > 0.01 + 0.05 * shown:
            bad("the estimate changed since you approved it; review the new estimate")
        with db.connect() as c:
            rid = c.execute("INSERT INTO ai_runs(status,started_at,cap_usd,estimate,current_op) "
                            "VALUES ('running',?,?,?,'starting')",
                            (now_iso(), state.settings.spend_cap_usd, json.dumps(est))).lastrowid

        def crash(msg):
            with db.connect() as c:
                c.execute("UPDATE ai_runs SET status='failed', error=?, finished_at=? WHERE id=?",
                          (msg[:300], now_iso(), rid))

        limit = est["within_cap"]["groups"] if body.within_cap else None
        state.jobs.submit("ai", lambda: ai_run.run(db, state.claude, rid, state.provider.account_email(), limit),
                          crash)
        return _ai_run_row(rid)

    @app.get("/api/ai/run")
    def ai_run_status():
        return _ai_run_row() or {"status": "none"}

    @app.post("/api/ai/run/cancel")
    def ai_cancel():
        with db.connect() as c:
            c.execute("UPDATE ai_runs SET cancel_requested=1 WHERE status='running'")
        return _ai_run_row() or {"status": "none"}

    # -- Claude: natural-language requests ------------------------------------
    @app.post("/api/query/plan")
    def query_plan(body: QueryIn):
        """Sentence -> filter spec. Returns the spec and a local preview. Creates nothing."""
        need_ai()
        try:
            spec = state.planner.plan(body.text)
        except (PlannerError, SpendCapReached) as e:
            bad(e)
        return {"spec": spec.to_dict(), "preview": query_preview(db, spec)}

    @app.post("/api/query/preview")
    def query_preview_ep(body: SpecIn):
        try:
            spec = parse_spec(body.spec)
        except SpecError as e:
            bad(e)
        return {"spec": spec.to_dict(), "preview": query_preview(db, spec)}

    @app.post("/api/query/save")
    def query_save(body: SpecIn):
        """Turn a spec you reviewed into an editable recommendation. It still needs the normal plan approval."""
        try:
            spec = parse_spec(body.spec)
        except SpecError as e:
            bad(e)
        if spec.mode != "messages":
            bad("only message filters can be saved as recommendations")
        with db.connect() as c:
            rid = f"q{uuid.uuid4().hex[:12]}"
            c.execute("INSERT INTO recommendations(id,category,title,action,older_than_days,reason,confidence,"
                      "classifier,created_at,spec,source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (rid, "custom", spec.title or "Your request", spec.suggested_action, 0,
                       spec.explanation or "Saved from your request.", 1.0, "saved request", now_iso(),
                       json.dumps(spec.to_dict()), "query"))
        return {"id": rid}

    # -- protection rules -----------------------------------------------------
    def _reanalyse():
        user = account() or need_reader().account_email()  # cached address first: no Gmail call
        apply_protection(db, user)
        groups = aggregate(db, user)
        classify(db, state.classifier, groups)
        build_recommendations(db, state.classifier, None)

    @app.get("/api/protection-rules")
    def rules():
        with db.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM protection_rules ORDER BY id")]

    @app.post("/api/protection-rules")
    def add_rule(body: RuleIn):
        if body.kind not in ("sender", "domain", "label") or not body.value.strip():
            bad("kind must be sender, domain or label")
        if state.jobs.busy():
            bad("wait for the current job to finish", 409)
        value = body.value.strip() if body.kind == "label" else body.value.strip().lower()
        with db.connect() as c:
            c.execute("INSERT OR IGNORE INTO protection_rules(kind,value,created_at) VALUES (?,?,?)",
                      (body.kind, value, now_iso()))
        _reanalyse()
        return rules()

    @app.delete("/api/protection-rules/{rule_id}")
    def del_rule(rule_id: int):
        if state.jobs.busy():
            bad("wait for the current job to finish", 409)
        with db.connect() as c:
            c.execute("DELETE FROM protection_rules WHERE id=?", (rule_id,))
        _reanalyse()
        return rules()

    # -- plans ----------------------------------------------------------------
    @app.post("/api/plans")
    def new_plan(body: PlanIn):
        try:
            return create_plan(db, [PlanItem(**i.model_dump()) for i in body.items])
        except PlanError as e:
            bad(e)

    @app.get("/api/plans/{plan_id}")
    def plan(plan_id: int):
        try:
            return get_plan(db, plan_id)
        except PlanError as e:
            bad(e, 404)

    @app.post("/api/plans/{plan_id}/approve")
    def approve(plan_id: int, body: ApproveIn):
        try:
            return approve_plan(db, plan_id, body.confirm_hash)
        except PlanError as e:
            bad(e)

    @app.post("/api/plans/{plan_id}/execute")
    def execute(plan_id: int):
        if state.jobs.busy():
            bad("another job is running", 409)
        try:
            run_id = state.executor.create_run(plan_id)
        except ExecutionError as e:
            bad(e)
        state.jobs.submit("execute", lambda: state.executor.run(run_id), _run_crash(run_id))
        return run_detail(run_id)

    # -- runs -----------------------------------------------------------------
    def _run_crash(run_id):
        def f(msg):
            with db.tx() as c:
                c.execute("UPDATE runs SET status='failed', error=?, finished_at=? WHERE id=?", (msg, now_iso(), run_id))
                c.execute("UPDATE plans SET status='partial' WHERE status='running'")
        return f

    @app.get("/api/runs")
    def runs():
        with db.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 50")]

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: int):
        with db.connect() as c:
            r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not r:
                bad("unknown run", 404)
            d = dict(r)
            d["audit_counts"] = {f"{a[0]}:{a[1]}": a[2] for a in c.execute(
                "SELECT action, status, COUNT(*) FROM audit_log WHERE run_id=? GROUP BY action, status", (run_id,))}
            d["skip_reasons"] = {a[0]: a[1] for a in c.execute(
                "SELECT detail, COUNT(*) FROM audit_log WHERE run_id=? AND status='skipped' GROUP BY detail", (run_id,))}
            d["failures"] = [dict(a) for a in c.execute(
                "SELECT message_id, action, detail FROM audit_log WHERE run_id=? AND status='failed' LIMIT 20", (run_id,))]
            if d["plan_id"]:
                p = c.execute("SELECT inbox_before, total, status FROM plans WHERE id=?", (d["plan_id"],)).fetchone()
                d["plan"] = dict(p) if p else None
                d["plan_remaining"] = c.execute(
                    "SELECT COUNT(*) FROM plan_messages WHERE plan_id=? AND status IN ('pending','failed')",
                    (d["plan_id"],)).fetchone()[0]
            d["inbox_now"] = c.execute("SELECT COUNT(*) FROM messages WHERE is_sent=0 AND in_inbox=1").fetchone()[0]
            # Restore covers every change the run attempted (write-ahead rows), not just the confirmed ones.
            d["restorable"] = c.execute(
                "SELECT COUNT(DISTINCT message_id) FROM audit_log WHERE run_id=? AND action IN ('archive','trash') "
                "AND status='attempted'", (run_id,)).fetchone()[0]
            d["restores"] = [dict(x) for x in c.execute(
                "SELECT id, status, confirmed, failed FROM runs WHERE source_run_id=? ORDER BY id", (run_id,))]
        return d

    @app.post("/api/runs/{run_id}/cancel")
    def run_cancel(run_id: int):
        state.executor.request_cancel(run_id)
        return run_detail(run_id)

    @app.post("/api/runs/{run_id}/restore")
    def run_restore(run_id: int):
        if state.jobs.busy():
            bad("another job is running", 409)
        try:
            rid = state.executor.create_restore(run_id)
        except ExecutionError as e:
            bad(e)
        state.jobs.submit("restore", lambda: state.executor.restore(rid), _run_crash(rid))
        return run_detail(rid)

    @app.get("/api/runs/{run_id}/audit")
    def run_audit(run_id: int, limit: int = 200):
        with db.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM audit_log WHERE run_id=? ORDER BY id LIMIT ?",
                                               (run_id, min(limit, 1000)))]

    @app.post("/api/cleanup/unlock")
    def unlock():
        try:
            state.executor.unlock_full_runs()
        except ExecutionError as e:
            bad(e)
        return status()

    # -- mock-only ------------------------------------------------------------
    @app.post("/api/mock/reset")
    def mock_reset():
        if not isinstance(state.provider, MockProvider):
            bad("only available with the synthetic mailbox", 404)
        if state.jobs.busy():
            bad("another job is running", 409)
        state.provider.regenerate(state.settings.mock_size, state.settings.mock_seed)
        db.wipe_analysis()
        return {"ok": True}

    # Protection flags are stored with the scan; re-derive them if the built-in rules changed since.
    if db.get_setting("protection.rules_version") != RULES_VERSION and not state.jobs.busy():
        with db.connect() as c:
            has_scan = c.execute("SELECT 1 FROM messages LIMIT 1").fetchone() is not None
        if has_scan and (db.get_setting("account.email") or state.settings.provider != "gmail"):
            try:
                _reanalyse()  # apply_protection records the rules version
            except Exception as e:  # never block startup; the executor re-checks protection at run time anyway
                log.warning("protection refresh postponed: %s", type(e).__name__)
        elif not has_scan:
            db.set_setting("protection.rules_version", RULES_VERSION)

    return app


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # The OAuth redirect handler logs the full request line, which contains the one-time authorization code.
    for noisy in ("google_auth_oauthlib", "googleapiclient", "google.auth", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def default_app() -> FastAPI:
    configure_logging()
    return create_app(build_state(Settings()))
