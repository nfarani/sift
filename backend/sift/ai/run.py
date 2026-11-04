"""AI classification runs: pick what the rules didn't settle, estimate locally, run within the cap."""
from __future__ import annotations

import json

from ..analysis import aggregate, build_recommendations, classify
from ..ai.ledger import cost, estimate_tokens
from ..classifier.claude import SYSTEM_PROMPT, ClaudeClassifier, GroupInput, render_data
from ..classifier.rules import RuleOnlyClassifier
from ..db import Database, now_iso


def candidates(db: Database) -> list[GroupInput]:
    """Sender groups sent to Claude, biggest first: everything the rules didn't firmly settle, including
    groups the rules labelled but may have misfiled.

    Never sent: senders you've corresponded with ("personal"), finance/security senders whose mail is mostly
    protected anyway, senders with nothing left to act on, and senders already labelled by Claude (cached).
    Minimal samples only, and only from unprotected messages: 6 subjects + 3 snippets of <=160 chars.
    """
    with db.connect() as c:
        rows = c.execute(
            "SELECT g.* FROM sender_groups g WHERE g.category IS NOT 'personal' "
            "AND NOT (g.category IN ('financial','security') AND g.protected * 2 >= g.total) "
            "AND g.in_inbox > g.protected "
            "AND g.sender NOT IN (SELECT sender FROM ai_labels) "
            "ORDER BY g.total DESC, g.sender").fetchall()
        sent_to = {a.strip() for r in c.execute("SELECT to_addrs FROM messages WHERE is_sent=1")
                   for a in (r[0] or "").split(",") if a.strip()}
        out = []
        for g in rows:
            subj = [r[0] for r in c.execute(
                "SELECT subject FROM messages WHERE from_addr=? AND is_sent=0 AND protected=0 GROUP BY subject "
                "ORDER BY MAX(internal_date) DESC LIMIT 6", (g["sender"],))]
            snip = [r[0] for r in c.execute(
                "SELECT snippet FROM messages WHERE from_addr=? AND is_sent=0 AND protected=0 AND snippet != '' "
                "ORDER BY internal_date DESC LIMIT 3", (g["sender"],))]
            out.append(GroupInput(
                sender=g["sender"], display_name=g["display_name"] or "", list_id=g["list_id"], total=g["total"],
                unread=g["unread"], gmail_tabs=json.loads(g["gmail_categories"] or "{}"),
                has_unsubscribe=bool(g["has_unsub"]), you_corresponded=g["sender"] in sent_to,
                subjects=subj, snippets=snip))
    return out


def estimate(db: Database, clf: ClaudeClassifier, cap_usd: float) -> dict:
    groups = candidates(db)
    full = clf.estimate(groups)
    with db.connect() as c:
        inbox = c.execute("SELECT COUNT(*) FROM messages WHERE is_sent=0 AND in_inbox=1").fetchone()[0]
    # How far the cap reaches if we go biggest-first: marginal cost per group, including its share of
    # the per-request system prompt and the assumed share of second-opinion (review) calls.
    share = full["review_share_assumed"]
    sys_tokens = estimate_tokens(SYSTEM_PROMPT) + 400
    covered_groups = covered_msgs = 0
    spent = 0.0
    for g in groups:
        data = estimate_tokens(render_data([g]))
        bulk = cost(clf.bulk_model, data + sys_tokens / clf.bulk_batch, 90 + 50 / clf.bulk_batch)
        review = cost(clf.review_model, data + sys_tokens / clf.review_batch, 110 + 1500 / clf.review_batch)
        spent += bulk + share * review
        if spent > cap_usd:
            break
        covered_groups += 1
        covered_msgs += g.total
    return {**full, "messages_in_candidates": sum(g.total for g in groups), "inbox": inbox,
            "cap_usd": cap_usd, "within_cap": {"groups": covered_groups, "messages": covered_msgs},
            "sends": "sender address and name, list id, counts, Gmail tabs, up to 6 subjects and 3 snippets "
                     "(160 chars) per sender; no bodies, no attachments"}


def run(db: Database, clf: ClaudeClassifier, run_id: int, user_email: str) -> str:
    groups = candidates(db)
    with db.connect() as c:
        c.execute("UPDATE ai_runs SET groups_total=?, current_op='classifying' WHERE id=?", (len(groups), run_id))

    def progress(purpose, i, n, done):
        with db.connect() as c:
            c.execute("UPDATE ai_runs SET current_op=?, groups_done=?, cost_usd=? WHERE id=?",
                      (f"{'first pass' if purpose == 'classify_bulk' else 'second opinion'}: {i}/{n}", done,
                       clf.ledger.spent(run_id), run_id))

    def cancelled():
        with db.connect() as c:
            return bool(c.execute("SELECT cancel_requested FROM ai_runs WHERE id=?", (run_id,)).fetchone()[0])

    def save(batch):  # persist each accepted batch at once, so paid-for labels survive a later failure
        with db.tx() as c:
            for lab in batch:
                c.execute("INSERT OR REPLACE INTO ai_labels VALUES (?,?,?,?,?,?,?)",
                          (lab.sender, lab.category, lab.confidence, lab.reason, json.dumps(lab.exceptions),
                           lab.model, now_iso()))

    labels, stats = clf.classify(groups, run_id, progress, cancelled, on_batch=save)
    # Re-apply rules + Claude labels, then rebuild recommendations.
    rules = RuleOnlyClassifier()
    classify(db, rules, aggregate(db, user_email))
    build_recommendations(db, rules, None)
    status = ("cancelled" if stats["cancelled"] else "stopped_at_cap" if stats["stopped_at_cap"] else "completed")
    with db.connect() as c:
        c.execute("UPDATE ai_runs SET status=?, finished_at=?, groups_done=?, groups_failed=?, groups_reviewed=?, "
                  "cost_usd=?, current_op='finished', error=? WHERE id=?",
                  (status, now_iso(), len(labels), stats["failed"], stats["reviewed"], clf.ledger.spent(run_id),
                   "; ".join(stats["errors"][:3]) or None, run_id))
    return status
