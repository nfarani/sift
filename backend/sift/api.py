"""FastAPI app. The UI reads every number from here; nothing is computed or invented client-side."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .analysis import DEFAULT_OLDER_THAN, aggregate, apply_protection, build_recommendations, classify
from .classifier.base import CATEGORIES, KEEP, Classifier
from .classifier.rules import RuleOnlyClassifier
from .config import Settings
from .db import Database, now_iso
from .executor import ExecutionError, Executor, unlocked
from .jobs import JobRunner, recover_interrupted
from .plans import PlanError, PlanItem, approve_plan, create_plan, get_plan
from .providers.base import MailProvider
from .providers.mock import MockProvider
from .retry import RetryPolicy
from .scanner import Scanner

log = logging.getLogger("sift")


@dataclass
class State:
    settings: Settings
    db: Database
    provider: MailProvider
    classifier: Classifier
    scanner: Scanner
    executor: Executor
    jobs: JobRunner


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


class RuleIn(BaseModel):
    kind: str
    value: str


def build_state(settings: Settings, provider: MailProvider | None = None, retry: RetryPolicy | None = None) -> State:
    db = Database(settings.app_db)
    if provider is None:
        if settings.provider != "mock":
            raise RuntimeError(f"provider {settings.provider!r} is not available until Phase 2")
        provider = MockProvider(settings.mock_db, size=settings.mock_size, seed=settings.mock_seed,
                                latency_ms=settings.mock_latency_ms)
    retry = retry or RetryPolicy(settings.max_attempts, settings.backoff_base, settings.backoff_max)
    classifier = RuleOnlyClassifier()
    return State(settings, db, provider, classifier, Scanner(db, provider, classifier, retry),
                 Executor(db, provider, settings.first_run_cap, retry), JobRunner(db))


def _ms_to_iso(ms):
    return None if ms is None else time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def create_app(state: State) -> FastAPI:
    app = FastAPI(title="Sift", version="0.1.0")
    app.state.sift = state
    db = state.db
    recover_interrupted(db)

    def bad(e: Exception, code: int = 400):
        raise HTTPException(code, str(e))

    # -- status / dashboard ---------------------------------------------------
    @app.get("/api/status")
    def status():
        s = state.settings.public_status()
        return {**s, "account": state.provider.account_email(), "provider_name": state.provider.name,
                "synthetic": state.provider.synthetic,
                "classifier": {"name": state.classifier.name, "label": state.classifier.label,
                               "uses_ai": state.classifier.uses_ai},
                "mode": "read-only (mock)" if state.provider.synthetic else "read-only",
                "full_runs_unlocked": unlocked(db), "busy": state.jobs.busy()}

    @app.get("/api/dashboard")
    def dashboard():
        with db.connect() as c:
            one = lambda q, *a: c.execute(q, a).fetchone()[0]
            total = one("SELECT COUNT(*) FROM messages WHERE is_sent=0")
            inbox = one("SELECT COUNT(*) FROM messages WHERE is_sent=0 AND in_inbox=1")
            unread = one("SELECT COUNT(*) FROM messages WHERE is_sent=0 AND is_unread=1")
            promos = one("SELECT COUNT(*) FROM messages WHERE is_sent=0 AND labels LIKE '%CATEGORY_PROMOTIONS%'")
            subs = one("SELECT COUNT(*) FROM sender_groups WHERE list_id IS NOT NULL OR has_unsub=1")
            protected = one("SELECT COUNT(*) FROM messages WHERE is_sent=0 AND protected=1")
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
        return {"account": state.provider.account_email(), "synthetic": state.provider.synthetic,
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
            state.scanner._error(scan_id, msg)
        return f

    @app.get("/api/scan")
    def scan_status():
        return _scan_row() or {"status": "none"}

    @app.post("/api/scan")
    def scan_start():
        if state.jobs.busy():
            bad("another job is running", 409)
        sid = state.scanner.start()
        state.jobs.submit("scan", lambda: state.scanner.run(sid), _scan_crash(sid))
        return _scan_row(sid)

    @app.post("/api/scan/resume")
    def scan_resume():
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
        state.scanner.request_cancel(r["id"])
        return _scan_row(r["id"])

    # -- recommendations ------------------------------------------------------
    def _rec_detail(c, rec, older_than=None, excluded=()):
        older = rec["older_than_days"] if older_than is None else older_than
        cutoff = int(time.time() * 1000) - older * 86_400_000
        excl = [e.lower() for e in excluded]
        senders = []
        for s in c.execute(
            "SELECT sender, display_name, total, in_inbox, unread, protected, first_date, last_date, list_id, "
            "has_unsub, one_click_unsub, confidence, reason FROM sender_groups WHERE category=? ORDER BY total DESC",
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
        return {"classifier": {"name": state.classifier.name, "label": state.classifier.label,
                               "uses_ai": state.classifier.uses_ai},
                "defaults": DEFAULT_OLDER_THAN, "items": out}

    @app.post("/api/recommendations/{rec_id}/preview")
    def preview(rec_id: str, body: PreviewIn):
        with db.connect() as c:
            rec = c.execute("SELECT * FROM recommendations WHERE id=?", (rec_id,)).fetchone()
            if not rec:
                bad("unknown recommendation", 404)
            return _rec_detail(c, rec, body.older_than_days, body.excluded_senders)

    # -- protection rules -----------------------------------------------------
    def _reanalyse():
        user = state.provider.account_email()
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

    return app


def default_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app(build_state(Settings()))
