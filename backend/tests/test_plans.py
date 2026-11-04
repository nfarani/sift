import sqlite3

import pytest

from sift.executor import ExecutionError
from sift.plans import PlanError, PlanItem, approve_plan, create_plan, get_plan

from .conftest import approved_plan, plan_ids


def test_plan_lists_exact_ids_and_excludes_senders(db, scanned):
    full = create_plan(db, [PlanItem(rec_id="promotions")])
    with db.connect() as c:
        top = c.execute("SELECT from_addr FROM messages WHERE category='promotions' GROUP BY from_addr "
                        "ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
    excl = create_plan(db, [PlanItem(rec_id="promotions", excluded_senders=[top])])
    with db.connect() as c:
        n_top = c.execute("SELECT COUNT(*) FROM plan_messages pm JOIN messages m ON m.id=pm.message_id "
                          "WHERE pm.plan_id=? AND m.from_addr=?", (excl["id"], top)).fetchone()[0]
    assert n_top == 0
    assert excl["total"] < full["total"]
    assert excl["summary"]["user_excluded"] == full["total"] - excl["total"]
    assert get_plan(db, full["id"])["status"] == "superseded"  # a new draft supersedes the old one


def test_older_than_filter(db, scanned):
    all_ = create_plan(db, [PlanItem(rec_id="newsletters", older_than_days=0)])
    old = create_plan(db, [PlanItem(rec_id="newsletters", older_than_days=365)])
    assert old["total"] < all_["total"]
    assert old["summary"]["too_recent"] == all_["total"] - old["total"]


def test_approval_requires_matching_hash(db, scanned):
    p = create_plan(db, [PlanItem(rec_id="promotions")])
    with pytest.raises(PlanError):
        approve_plan(db, p["id"], "0" * 64)
    a = approve_plan(db, p["id"], p["ids_hash"])
    assert a["status"] == "approved"
    with pytest.raises(PlanError):
        approve_plan(db, p["id"], p["ids_hash"])  # cannot re-approve


def test_frozen_plan_rejects_new_rows(db, scanned):
    p = approved_plan(db)
    with db.connect() as c:
        extra = c.execute("SELECT id FROM messages WHERE category='newsletters' LIMIT 1").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="frozen"):
            c.execute("INSERT INTO plan_messages(plan_id,message_id,rec_id,action) VALUES (?,?,?,?)",
                      (p["id"], extra, "promotions", "archive"))


def test_frozen_plan_rejects_scope_changes(db, scanned):
    p = approved_plan(db)
    mid = plan_ids(db, p["id"])[0]
    with db.connect() as c:
        with pytest.raises(sqlite3.IntegrityError, match="frozen"):
            c.execute("UPDATE plan_messages SET action='trash' WHERE plan_id=? AND message_id=?", (p["id"], mid))
        with pytest.raises(sqlite3.IntegrityError, match="frozen"):
            c.execute("UPDATE plan_messages SET message_id='other' WHERE plan_id=? AND message_id=?", (p["id"], mid))


def test_executor_refuses_tampered_plan(db, scanned, executor):
    p = approved_plan(db)
    with db.connect() as c:  # bypass the triggers by dropping one: the hash check is the backstop
        c.execute("DROP TRIGGER plan_messages_no_delete")
        c.execute("DELETE FROM plan_messages WHERE plan_id=? AND message_id=?", (p["id"], plan_ids(db, p["id"])[0]))
    with pytest.raises(ExecutionError, match="changed after approval"):
        executor.create_run(p["id"])


def test_executor_refuses_unapproved_plan(db, scanned, executor):
    p = create_plan(db, [PlanItem(rec_id="promotions")])
    with pytest.raises(ExecutionError, match="approved"):
        executor.create_run(p["id"])


def test_review_only_recommendation_cannot_be_planned(db, scanned):
    with db.connect() as c:
        has = c.execute("SELECT 1 FROM recommendations WHERE id='uncertain'").fetchone()
    if has:
        with pytest.raises(PlanError, match="review-only"):
            create_plan(db, [PlanItem(rec_id="uncertain")])


def test_unknown_action_rejected(db, scanned):
    with pytest.raises(PlanError):
        create_plan(db, [PlanItem(rec_id="promotions", action="delete")])


def test_later_scan_does_not_change_approved_plan(db, scanned, scanner):
    p = approved_plan(db)
    before = sorted(plan_ids(db, p["id"]))
    sid = scanner.start()
    scanner.run(sid)
    assert sorted(plan_ids(db, p["id"])) == before


def test_approved_plan_fields_and_status_are_frozen(db, scanned):
    """Safety review S3."""
    p = approved_plan(db)
    with db.connect() as c:
        for stmt in ("UPDATE plans SET ids_hash='x' WHERE id=?", "UPDATE plans SET spec='[]' WHERE id=?",
                     "UPDATE plans SET total=1 WHERE id=?", "UPDATE plans SET status='draft' WHERE id=?"):
            with pytest.raises(sqlite3.IntegrityError, match="frozen"):
                c.execute(stmt, (p["id"],))


def test_frozen_plan_rows_cannot_be_deleted_or_reapproved(db, scanned):
    import sqlite3
    p = approved_plan(db)
    with db.connect() as c:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("DELETE FROM plan_messages WHERE plan_id=?", (p["id"],))
        c.execute("UPDATE plans SET status='cancelled' WHERE id=?", (p["id"],))
        for back in ("approved", "running", "partial"):
            with pytest.raises(sqlite3.IntegrityError):
                c.execute("UPDATE plans SET status=? WHERE id=?", (back, p["id"]))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE plans SET analysis_gen=999 WHERE id=?", (p["id"],))
