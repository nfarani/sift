"""Plan builder: turns the user's edited recommendations into a frozen list of message ids.

A plan is created as `draft` with its exact ids already written. Approval requires the
caller to echo the plan's ids hash (so you approve exactly what you previewed); after that
the DB triggers forbid adding rows or changing any row's id/action. Any edit means a new plan.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field

from .db import Database, now_iso
from .query import Spec, matching_ids, parse_spec, where

ACTIONS = {"archive", "trash"}


class PlanError(ValueError):
    pass


@dataclass
class PlanItem:
    rec_id: str
    action: str = "archive"
    older_than_days: int = 0
    excluded_senders: list[str] = field(default_factory=list)


def ids_hash(rows: list[tuple[str, str]]) -> str:
    h = hashlib.sha256()
    for mid, action in sorted(rows):
        h.update(f"{mid}:{action}\n".encode())
    return h.hexdigest()


def _select(c, item: PlanItem, cutoff_ms: int) -> tuple[list[str], dict]:
    excl = [s.lower() for s in item.excluded_senders]
    q_excl = f" AND from_addr NOT IN ({','.join('?' * len(excl))})" if excl else ""
    base = "FROM messages WHERE is_sent=0 AND in_inbox=1 AND category=?"
    ids = [r[0] for r in c.execute(
        f"SELECT id {base} AND protected=0 AND internal_date < ?{q_excl} ORDER BY internal_date",
        (item.rec_id, cutoff_ms, *excl))]
    protected = c.execute(f"SELECT COUNT(*) {base} AND protected=1", (item.rec_id,)).fetchone()[0]
    by_reason = {r[0]: r[1] for r in c.execute(
        f"SELECT protect_reason, COUNT(*) {base} AND protected=1 GROUP BY protect_reason", (item.rec_id,))}
    too_recent = c.execute(f"SELECT COUNT(*) {base} AND protected=0 AND internal_date >= ?{q_excl}",
                           (item.rec_id, cutoff_ms, *excl)).fetchone()[0]
    user_excluded = 0
    if excl:
        user_excluded = c.execute(
            f"SELECT COUNT(*) {base} AND protected=0 AND from_addr IN ({','.join('?' * len(excl))})",
            (item.rec_id, *excl)).fetchone()[0]
    return ids, {"protected_excluded": protected, "protected_by_reason": by_reason,
                 "too_recent": too_recent, "user_excluded": user_excluded}


def _select_spec(c, item: PlanItem, spec: Spec, now_ms: int) -> tuple[list[str], dict]:
    """Custom (natural-language) recommendation: same deterministic filter as its preview.
    Protected and sent mail are excluded by query.where() itself."""
    if spec.mode != "messages":
        raise PlanError("a top-senders view can't be turned into a plan; save a message filter instead")
    excl = tuple(s.lower() for s in item.excluded_senders)
    ids = matching_ids(c, spec, excl, item.older_than_days, now_ms)
    wp, pp = where(spec, now_ms=now_ms, protected=1)
    by_reason = {r[0]: r[1] for r in c.execute(
        f"SELECT protect_reason, COUNT(*) FROM messages WHERE {wp} GROUP BY protect_reason", pp)}
    wa, pa = where(spec, now_ms=now_ms)                      # no exclusions, no extra age
    all_ = c.execute(f"SELECT COUNT(*) FROM messages WHERE {wa}", pa).fetchone()[0]
    wx, px = where(spec, now_ms=now_ms, extra_older_than_days=item.older_than_days)
    aged = c.execute(f"SELECT COUNT(*) FROM messages WHERE {wx}", px).fetchone()[0]
    return ids, {"protected_excluded": sum(by_reason.values()), "protected_by_reason": by_reason,
                 "too_recent": all_ - aged, "user_excluded": aged - len(ids)}


def create_plan(db: Database, items: list[PlanItem]) -> dict:
    if not items:
        raise PlanError("a plan needs at least one recommendation")
    seen = set()
    for it in items:
        if it.action not in ACTIONS:
            raise PlanError(f"unsupported action {it.action!r}")
        if it.rec_id in seen:
            raise PlanError(f"recommendation {it.rec_id} listed twice")
        seen.add(it.rec_id)
        if it.older_than_days < 0:
            raise PlanError("older_than_days must be >= 0")
    now_ms = int(time.time() * 1000)
    with db.tx() as c:
        recs = {r["id"]: r for r in c.execute("SELECT id, action, spec FROM recommendations")}
        for it in items:
            if it.rec_id not in recs:
                raise PlanError(f"unknown recommendation {it.rec_id}")
            if recs[it.rec_id]["action"] == "review":
                raise PlanError(f"{it.rec_id} is review-only and cannot be executed")
        rows: list[tuple[str, str, str]] = []
        per_item = []
        for it in items:
            spec = parse_spec(json.loads(recs[it.rec_id]["spec"])) if recs[it.rec_id]["spec"] else None
            older = max(it.older_than_days, (spec.older_than_days or 0) if spec else 0)
            cutoff = now_ms - older * 86_400_000
            ids, info = _select_spec(c, it, spec, now_ms) if spec else _select(c, it, cutoff)
            rows += [(mid, it.rec_id, it.action) for mid in ids]
            per_item.append({"rec_id": it.rec_id, "action": it.action, "older_than_days": it.older_than_days,
                             "cutoff_ms": cutoff, "excluded_senders": sorted(s.lower() for s in it.excluded_senders),
                             "count": len(ids), **info})
        if not rows:
            raise PlanError("nothing matches: every message is protected, excluded or too recent")
        inbox_before = c.execute("SELECT COUNT(*) FROM messages WHERE in_inbox=1 AND is_sent=0").fetchone()[0]
        digest = ids_hash([(m, a) for m, _, a in rows])
        summary = {
            "by_action": {a: sum(1 for r in rows if r[2] == a) for a in ACTIONS if any(r[2] == a for r in rows)},
            "items": per_item,
            "protected_excluded": sum(p["protected_excluded"] for p in per_item),
            "user_excluded": sum(p["user_excluded"] for p in per_item),
            "too_recent": sum(p["too_recent"] for p in per_item),
        }
        c.execute("UPDATE plans SET status='superseded' WHERE status='draft'")
        cur = c.execute(
            "INSERT INTO plans(status,created_at,spec,total,ids_hash,summary,inbox_before) VALUES ('draft',?,?,?,?,?,?)",
            (now_iso(), json.dumps(per_item), len(rows), digest, json.dumps(summary), inbox_before))
        pid = cur.lastrowid
        c.executemany("INSERT INTO plan_messages(plan_id,message_id,rec_id,action) VALUES (?,?,?,?)",
                      [(pid, m, r, a) for m, r, a in rows])
    return get_plan(db, pid)


def approve_plan(db: Database, plan_id: int, confirm_hash: str) -> dict:
    with db.tx() as c:
        p = c.execute("SELECT status, ids_hash FROM plans WHERE id=?", (plan_id,)).fetchone()
        if p is None:
            raise PlanError("unknown plan")
        if p["status"] != "draft":
            raise PlanError(f"plan is {p['status']}, only a draft can be approved")
        if confirm_hash != p["ids_hash"]:
            raise PlanError("confirmation does not match this plan's contents; review it again")
        c.execute("UPDATE plans SET status='approved', approved_at=? WHERE id=?", (now_iso(), plan_id))
    return get_plan(db, plan_id)


def verify_frozen(c, plan_id: int) -> bool:
    p = c.execute("SELECT ids_hash FROM plans WHERE id=?", (plan_id,)).fetchone()
    rows = [(r[0], r[1]) for r in c.execute("SELECT message_id, action FROM plan_messages WHERE plan_id=?", (plan_id,))]
    return p is not None and ids_hash(rows) == p["ids_hash"]


def get_plan(db: Database, plan_id: int) -> dict:
    with db.connect() as c:
        p = c.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if p is None:
            raise PlanError("unknown plan")
        counts = {r[0]: r[1] for r in c.execute(
            "SELECT status, COUNT(*) FROM plan_messages WHERE plan_id=? GROUP BY status", (plan_id,))}
        samples = [dict(r) for r in c.execute(
            "SELECT m.id, m.from_addr, m.from_name, m.subject, m.internal_date, pm.action, pm.rec_id "
            "FROM plan_messages pm JOIN messages m ON m.id=pm.message_id WHERE pm.plan_id=? "
            "ORDER BY m.internal_date DESC LIMIT 8", (plan_id,))]
    d = dict(p)
    d["spec"] = json.loads(d["spec"])
    d["summary"] = json.loads(d["summary"])
    d["message_status"] = counts
    d["samples"] = samples
    return d
