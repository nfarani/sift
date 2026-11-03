"""Aggregator → protection rules → classifier → recommendations. All local, all deterministic
except for whatever the configured classifier does (rules only in Phase 1)."""
from __future__ import annotations

import json
from collections import Counter, defaultdict

from .classifier.base import CATEGORIES, DISPOSABLE, Classifier, SenderGroup
from .db import Database, now_iso
from .protection import load_context, protection_reason

# Default age threshold per recommendation: recent mail is left alone.
DEFAULT_OLDER_THAN = {"job_alerts": 14, "promotions": 30, "newsletters": 30, "social": 7,
                      "notifications": 30, "spam": 0, "advertising": 30}
TITLES = {
    "job_alerts": "Archive automated job alerts",
    "promotions": "Archive shopping and travel promotions",
    "newsletters": "Archive old newsletters",
    "social": "Archive social media notifications",
    "notifications": "Archive old automated notifications",
    "spam": "Archive likely spam",
    "advertising": "Archive advertising",
    "uncertain": "Review senders the rules could not classify",
}


def apply_protection(db: Database, user_email: str) -> int:
    ctx = load_context(db, user_email)
    updates = []
    with db.connect() as c:
        for r in c.execute("SELECT id, thread_id, from_addr, subject, labels FROM messages WHERE is_sent=0"):
            reason = protection_reason(labels=json.loads(r["labels"]), thread_id=r["thread_id"],
                                       from_addr=r["from_addr"], subject=r["subject"], ctx=ctx)
            updates.append((1 if reason else 0, reason, r["id"]))
    with db.tx() as c:
        c.executemany("UPDATE messages SET protected=?, protect_reason=? WHERE id=?", updates)
    return sum(u[0] for u in updates)


def aggregate(db: Database, user_email: str) -> list[SenderGroup]:
    ctx = load_context(db, user_email)
    stats: dict[str, dict] = {}
    cats: dict[str, Counter] = defaultdict(Counter)
    samples: dict[str, list[str]] = defaultdict(list)
    with db.connect() as c:
        rows = c.execute(
            "SELECT from_addr, from_name, from_domain, internal_date, labels, list_id, list_unsubscribe, "
            "one_click_unsub, in_inbox, is_unread, protected, subject FROM messages WHERE is_sent=0 ORDER BY id")
        for r in rows:
            s = stats.setdefault(r["from_addr"], dict(
                domain=r["from_domain"], name=r["from_name"], total=0, in_inbox=0, unread=0, protected=0,
                first=r["internal_date"], last=r["internal_date"], list_id=r["list_id"], has_unsub=0, one_click=0))
            s["total"] += 1
            s["in_inbox"] += r["in_inbox"]
            s["unread"] += r["is_unread"]
            s["protected"] += r["protected"]
            s["first"] = min(s["first"], r["internal_date"])
            s["last"] = max(s["last"], r["internal_date"])
            s["list_id"] = s["list_id"] or r["list_id"]
            s["has_unsub"] |= 1 if r["list_unsubscribe"] else 0
            s["one_click"] |= r["one_click_unsub"]
            for l in json.loads(r["labels"]):
                if l.startswith("CATEGORY_"):
                    cats[r["from_addr"]][l] += 1
            if len(samples[r["from_addr"]]) < 12:
                samples[r["from_addr"]].append(r["subject"])
    with db.tx() as c:
        c.execute("DELETE FROM sender_groups")
        c.executemany(
            "INSERT INTO sender_groups(sender,domain,display_name,total,in_inbox,unread,protected,first_date,last_date,"
            "list_id,has_unsub,one_click_unsub,gmail_categories) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(k, s["domain"], s["name"], s["total"], s["in_inbox"], s["unread"], s["protected"], s["first"], s["last"],
              s["list_id"], s["has_unsub"], s["one_click"], json.dumps(dict(cats[k]))) for k, s in stats.items()])
    return [SenderGroup(sender=k, domain=s["domain"], display_name=s["name"] or "", total=s["total"],
                        protected=s["protected"], list_id=s["list_id"], gmail_categories=dict(cats[k]),
                        sample_subjects=samples[k], user_emailed=k.lower() in ctx.sent_recipients)
            for k, s in stats.items()]


def classify(db: Database, classifier: Classifier, groups: list[SenderGroup]) -> int:
    result = classifier.classify(groups)
    with db.tx() as c:
        for sender, cl in result.items():
            if cl.category not in CATEGORIES:  # never trust an unknown label
                cl.category, cl.confidence = "uncertain", 0.0
            c.execute("UPDATE sender_groups SET category=?, confidence=?, reason=?, classifier=? WHERE sender=?",
                      (cl.category, cl.confidence, cl.reason, cl.classifier, sender))
            c.execute("UPDATE messages SET category=? WHERE from_addr=? AND is_sent=0", (cl.category, sender))
    return len({cl.category for cl in result.values()})


def build_recommendations(db: Database, classifier: Classifier, scan_id: int | None) -> None:
    with db.connect() as c:
        rows = c.execute(
            "SELECT category, SUM(total) n, SUM(total*confidence)/SUM(total) conf FROM sender_groups "
            "WHERE category IS NOT NULL GROUP BY category").fetchall()
    with db.tx() as c:
        c.execute("DELETE FROM recommendations")
        for r in rows:
            cat = r["category"]
            if cat not in DISPOSABLE and cat != "uncertain":
                continue
            action = "review" if cat == "uncertain" else "archive"
            reason = ("These senders didn't match any rule confidently. Nothing is suggested until you look."
                      if cat == "uncertain" else
                      f"{CATEGORIES[cat]}: bulk mail with no sign you've replied to it. Protected messages from the "
                      f"same senders (receipts, bookings, security, your threads) are excluded automatically.")
            c.execute("INSERT INTO recommendations VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (cat, cat, TITLES.get(cat, CATEGORIES[cat]), action, DEFAULT_OLDER_THAN.get(cat, 0), reason,
                       round(r["conf"] or 0, 2), classifier.name, scan_id, now_iso()))


def run_analysis(db: Database, classifier: Classifier, user_email: str, scan_id: int | None) -> dict:
    protected = apply_protection(db, user_email)          # 1. protection first
    groups = aggregate(db, user_email)                    # 2. sender/list aggregation
    n_cats = classify(db, classifier, groups)             # 3. classify what's left
    build_recommendations(db, classifier, scan_id)        # 4. recommendations
    return {"protected": protected, "senders": len(groups), "categories": n_cats}
