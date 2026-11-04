"""Aggregator → protection rules → classifier → recommendations. All local, all deterministic
except for whatever the configured classifier does (rules only in Phase 1)."""
from __future__ import annotations

import json
from collections import Counter, defaultdict

from .classifier.base import CATEGORIES, DISPOSABLE, KEEP, Classifier, SenderGroup
from .db import Database, now_iso
from .protection import RULES_VERSION, load_context, protection_reason

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
    db.set_setting("protection.rules_version", RULES_VERSION)
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


def _like_escape(text: str) -> str:
    return text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def classify(db: Database, classifier: Classifier, groups: list[SenderGroup]) -> int:
    """Rules first; validated Claude labels (cached in ai_labels) then override unsettled groups.
    Protection flags are untouched here: protected messages never enter a plan whatever the label."""
    result = classifier.classify(groups)
    with db.connect() as c:
        ai = {r["sender"]: r for r in c.execute("SELECT * FROM ai_labels")}
    with db.tx() as c:
        for sender, cl in result.items():
            if cl.category not in CATEGORIES:  # never trust an unknown label
                cl.category, cl.confidence = "uncertain", 0.0
            label, conf, reason, source, exceptions = cl.category, cl.confidence, cl.reason, cl.classifier, []
            a = ai.get(sender)
            if a is not None and cl.category != "personal" and a["category"] in CATEGORIES:
                label, conf, reason, source = a["category"], a["confidence"], a["reason"], f"claude:{a['model']}"
                exceptions = json.loads(a["exceptions"])
                if cl.category in KEEP and label in DISPOSABLE:
                    # Claude may not quietly downgrade something the rules wanted kept: you decide.
                    reason = (f"Rules said {CATEGORIES[cl.category].lower()}, Claude suggested "
                              f"{CATEGORIES[label].lower()} ({a['reason']}). Your call.")
                    label, conf = "uncertain", min(conf, 0.5)
            c.execute("UPDATE sender_groups SET category=?, confidence=?, reason=?, classifier=? WHERE sender=?",
                      (label, conf, reason, source, sender))
            c.execute("UPDATE messages SET category=? WHERE from_addr=? AND is_sent=0", (label, sender))
            # Per-message exceptions can only move mail towards keep categories (validated on the way in).
            for e in exceptions:
                if e.get("category") in KEEP and len(e.get("subject_contains", "").strip()) >= 4:
                    c.execute("UPDATE messages SET category=? WHERE from_addr=? AND is_sent=0 "
                              "AND lower(subject) LIKE ? ESCAPE '\\'",
                              (e["category"], sender, f"%{_like_escape(e['subject_contains'].strip())}%"))
    with db.connect() as c:
        return c.execute("SELECT COUNT(DISTINCT category) FROM messages WHERE is_sent=0").fetchone()[0]


def build_recommendations(db: Database, classifier: Classifier, scan_id: int | None) -> None:
    # Any rebuild (scan, rule change, Claude run) invalidates plans built from the previous analysis.
    db.set_setting("analysis.generation", int(db.get_setting("analysis.generation", 0) or 0) + 1)
    with db.connect() as c:
        rows = c.execute(
            "SELECT category, SUM(total) n, SUM(total*confidence)/SUM(total) conf, "
            "CASE WHEN SUM(classifier LIKE 'claude%') > 0 THEN 'rules+claude' ELSE 'rules' END clf "
            "FROM sender_groups WHERE category IS NOT NULL GROUP BY category").fetchall()
    with db.tx() as c:
        c.execute("DELETE FROM recommendations WHERE source IS NOT 'query'")  # saved NL queries survive rescans
        for r in rows:
            cat = r["category"]
            if cat not in DISPOSABLE and cat != "uncertain":
                continue
            action = "review" if cat == "uncertain" else "archive"
            reason = ("These senders didn't match any rule confidently. Nothing is suggested until you look."
                      if cat == "uncertain" else
                      f"{CATEGORIES[cat]}: bulk mail with no sign you've replied to it. Protected messages from the "
                      f"same senders (receipts, bookings, security, your threads) are excluded automatically.")
            c.execute("INSERT INTO recommendations(id,category,title,action,older_than_days,reason,confidence,"
                      "classifier,scan_id,created_at,source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (cat, cat, TITLES.get(cat, CATEGORIES[cat]), action, DEFAULT_OLDER_THAN.get(cat, 0), reason,
                       round(r["conf"] or 0, 2), r["clf"], scan_id, now_iso(), "rules" if r["clf"] == "rules" else "claude"))


def run_analysis(db: Database, classifier: Classifier, user_email: str, scan_id: int | None) -> dict:
    protected = apply_protection(db, user_email)          # 1. protection first
    groups = aggregate(db, user_email)                    # 2. sender/list aggregation
    n_cats = classify(db, classifier, groups)             # 3. classify what's left
    build_recommendations(db, classifier, scan_id)        # 4. recommendations
    return {"protected": protected, "senders": len(groups), "categories": n_cats}
