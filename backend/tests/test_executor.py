import pytest

from sift.executor import ExecutionError
from sift.providers.base import TransientError

from .conftest import approved_plan, labels_of, plan_ids


def run(executor, plan_id):
    rid = executor.create_run(plan_id)
    return rid, executor.run(rid)


def run_row(db, rid):
    with db.connect() as c:
        return dict(c.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone())


def test_archive_run_confirms_and_labels(db, provider, scanned, executor, unlocked):
    p = approved_plan(db)
    rid, status = run(executor, p["id"])
    r = run_row(db, rid)
    assert status == "completed"
    assert r["attempted"] == r["confirmed"] == p["total"]
    labels = labels_of(provider, plan_ids(db, p["id"]))
    label_id = r["label_id"]
    assert all("INBOX" not in l and label_id in l for l in labels.values())
    assert provider.label_name(label_id).startswith("Sift/") and provider.label_name(label_id).endswith(f"run-{rid}")


def test_archive_does_not_touch_unread(db, provider, scanned, executor, unlocked):
    p = approved_plan(db)
    ids = plan_ids(db, p["id"])
    unread_before = {m for m, l in labels_of(provider, ids).items() if "UNREAD" in l}
    run(executor, p["id"])
    unread_after = {m for m, l in labels_of(provider, ids).items() if "UNREAD" in l}
    assert unread_before == unread_after


def test_first_run_cap_enforced_in_backend(db, provider, scanned, executor):
    p = approved_plan(db)
    assert p["total"] > 50
    rid, status = run(executor, p["id"])
    r = run_row(db, rid)
    assert r["cap"] == 50 and r["total"] == 50 and r["confirmed"] == 50
    moved = [m for m, l in labels_of(provider, plan_ids(db, p["id"])).items() if "INBOX" not in l]
    assert len(moved) == 50


def test_unlock_requires_successful_restore(db, provider, scanned, executor):
    with pytest.raises(ExecutionError):
        executor.unlock_full_runs()
    p = approved_plan(db)
    rid, _ = run(executor, p["id"])
    with pytest.raises(ExecutionError):
        executor.unlock_full_runs()
    rest = executor.create_restore(rid)
    assert executor.restore(rest) == "completed"
    executor.unlock_full_runs()
    assert db.get_setting("cleanup.full_runs_unlocked") is True


def test_revalidation_removes_newly_protected_and_missing(db, provider, scanned, executor, unlocked):
    p = approved_plan(db)
    ids = plan_ids(db, p["id"])
    starred, gone = ids[0], ids[1]
    lab = labels_of(provider, [starred])[starred]
    provider._test_set_labels(starred, lab + ["STARRED"])
    provider._test_remove(gone)
    rid, status = run(executor, p["id"])
    r = run_row(db, rid)
    assert r["skipped"] == 2
    assert r["confirmed"] == p["total"] - 2
    assert "INBOX" in labels_of(provider, [starred])[starred]
    with db.connect() as c:
        reasons = dict(c.execute("SELECT message_id, reason FROM plan_messages WHERE plan_id=? AND status='skipped'",
                                 (p["id"],)).fetchall())
    assert reasons[starred].startswith("newly protected") and reasons[gone] == "no longer exists"


def test_revalidation_never_adds(db, provider, scanned, executor, unlocked):
    p = approved_plan(db)
    frozen = set(plan_ids(db, p["id"]))
    # A brand-new promo arrives after approval, matching the same filter.
    with provider._conn() as c:
        c.execute("INSERT INTO mbox SELECT 'newpromo1', 'newpromo1', from_addr, from_name, to_addrs, subject, snippet, "
                  "internal_date, labels, list_id, list_unsubscribe, list_unsubscribe_post FROM mbox WHERE id=?",
                  (next(iter(frozen)),))
    modified = []
    real = provider.batch_modify

    def spy(ids, add, remove):
        modified.extend(ids)
        return real(ids, add, remove)

    provider.batch_modify = spy
    run(executor, p["id"])
    assert set(modified) <= frozen
    assert "newpromo1" not in modified
    assert "INBOX" in labels_of(provider, ["newpromo1"])["newpromo1"]


def test_cancel_mid_run_leaves_rest_pending(db, provider, scanned, executor, unlocked):
    p = approved_plan(db)
    assert p["total"] > 2 * executor.chunk
    rid = executor.create_run(p["id"])
    real = provider.batch_modify

    def cancel_after_first(ids, add, remove):
        real(ids, add, remove)
        executor.request_cancel(rid)

    provider.batch_modify = cancel_after_first
    assert executor.run(rid) == "cancelled"
    r = run_row(db, rid)
    assert r["confirmed"] == executor.chunk
    with db.connect() as c:
        pending = c.execute("SELECT COUNT(*) FROM plan_messages WHERE plan_id=? AND status='pending'", (p["id"],)).fetchone()[0]
        pstatus = c.execute("SELECT status FROM plans WHERE id=?", (p["id"],)).fetchone()[0]
    assert pending == p["total"] - executor.chunk
    assert pstatus == "cancelled"
    # resuming finishes the remainder
    provider.batch_modify = real
    rid2, status = run(executor, p["id"])
    assert status == "completed" and run_row(db, rid2)["confirmed"] == pending


def test_retry_on_429_and_500_then_success(db, provider, scanned, executor, unlocked, sleeps):
    p = approved_plan(db)
    provider.faults["modify"] = [429, 500, 503]
    rid, status = run(executor, p["id"])
    assert status == "completed"
    assert run_row(db, rid)["confirmed"] == p["total"]
    assert sleeps[:3] == [0.5, 1.0, 2.0]  # exponential backoff


def test_persistent_failure_marks_failed_then_rerun_recovers(db, provider, scanned, executor, unlocked, retry):
    p = approved_plan(db)
    provider.faults["modify"] = [500] * retry.max_attempts  # first chunk exhausts retries
    rid, status = run(executor, p["id"])
    r = run_row(db, rid)
    assert status == "partial"
    assert r["failed"] == executor.chunk and r["confirmed"] == p["total"] - executor.chunk
    with db.connect() as c:
        audit_failed = c.execute("SELECT COUNT(*) FROM audit_log WHERE run_id=? AND status='failed'", (rid,)).fetchone()[0]
    assert audit_failed == executor.chunk
    rid2, status2 = run(executor, p["id"])
    assert status2 == "completed" and run_row(db, rid2)["confirmed"] == executor.chunk


def test_permanent_error_not_retried(db, provider, scanned, executor, unlocked, sleeps):
    p = approved_plan(db)
    provider.faults["modify"] = [400]
    rid, status = run(executor, p["id"])
    assert status == "partial"
    assert sleeps == []


def test_idempotent_rerun_makes_no_calls(db, provider, scanned, executor, unlocked):
    p = approved_plan(db)
    run(executor, p["id"])
    before = provider.calls["modify"]
    rid2, status = run(executor, p["id"])
    assert status == "completed"
    assert provider.calls["modify"] == before
    assert run_row(db, rid2)["attempted"] == 0


def test_already_applied_change_is_not_reapplied(db, provider, scanned, executor, unlocked):
    """Crash after the API call but before the DB write: the re-run notices and makes no call."""
    p = approved_plan(db)
    ids = plan_ids(db, p["id"])
    provider.batch_modify(ids[:10], add=[], remove=["INBOX"])  # applied out-of-band
    rid, _ = run(executor, p["id"])
    assert run_row(db, rid)["already_done"] == 10
    assert run_row(db, rid)["confirmed"] == p["total"] - 10


def test_restore_archive(db, provider, scanned, executor, unlocked):
    p = approved_plan(db)
    ids = plan_ids(db, p["id"])
    before = labels_of(provider, ids)
    rid, _ = run(executor, p["id"])
    label_id = run_row(db, rid)["label_id"]
    rest = executor.create_restore(rid)
    assert executor.restore(rest) == "completed"
    r = run_row(db, rest)
    assert r["confirmed"] == p["total"] and r["failed"] == 0
    after = labels_of(provider, ids)
    for mid in ids:
        assert "INBOX" in after[mid] and label_id not in after[mid]
        assert set(after[mid]) == set(before[mid])
    # restoring again is a no-op
    rest2 = executor.create_restore(rid)
    executor.restore(rest2)
    assert run_row(db, rest2)["attempted"] == 0


def test_trash_and_restore(db, provider, scanned, executor, unlocked):
    p = approved_plan(db, rec="spam", action="trash")
    ids = plan_ids(db, p["id"])
    rid, status = run(executor, p["id"])
    assert status == "completed"
    assert all("TRASH" in l for l in labels_of(provider, ids).values())
    assert provider.calls["trash"] == len(ids)
    rest = executor.create_restore(rid)
    assert executor.restore(rest) == "completed"
    after = labels_of(provider, ids)
    assert all("TRASH" not in l and "INBOX" in l for l in after.values())


def test_restore_reports_messages_that_no_longer_exist(db, provider, scanned, executor, unlocked):
    p = approved_plan(db, rec="spam", action="trash")
    ids = plan_ids(db, p["id"])
    rid, _ = run(executor, p["id"])
    provider._test_remove(ids[0])  # e.g. Trash auto-emptied after 30 days
    rest = executor.create_restore(rid)
    assert executor.restore(rest) == "partial"
    r = run_row(db, rest)
    assert r["failed"] == 1 and r["confirmed"] == len(ids) - 1


def test_audit_log_complete_and_append_only(db, provider, scanned, executor, unlocked):
    import sqlite3
    import json
    p = approved_plan(db)
    ids = plan_ids(db, p["id"])
    provider._test_remove(ids[0])
    rid, _ = run(executor, p["id"])
    with db.connect() as c:
        rows = c.execute("SELECT * FROM audit_log WHERE run_id=?", (rid,)).fetchall()
        assert {r["message_id"] for r in rows} == set(ids)  # every plan message has an entry
        for r in rows:
            assert r["ts"] and r["plan_id"] == p["id"] and r["action"] == "archive"
            if r["status"] == "confirmed":
                b, a = json.loads(r["before_labels"]), json.loads(r["after_labels"])
                assert "INBOX" in b and "INBOX" not in a
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            c.execute("UPDATE audit_log SET status='x' WHERE run_id=?", (rid,))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            c.execute("DELETE FROM audit_log WHERE run_id=?", (rid,))
    rest = executor.create_restore(rid)
    executor.restore(rest)
    with db.connect() as c:
        n = c.execute("SELECT COUNT(*) FROM audit_log WHERE run_id=? AND action='restore_archive' AND status='confirmed'",
                      (rest,)).fetchone()[0]
    assert n == len(ids) - 1


def test_batch_size_within_gmail_limit(db, provider, scanned, retry, unlocked):
    from sift.executor import Executor
    ex = Executor(db, provider, retry=retry, chunk=5000)
    assert ex.chunk <= 1000
    sizes = []
    real = provider.batch_modify
    provider.batch_modify = lambda ids, add, remove: (sizes.append(len(ids)), real(ids, add, remove))[1]
    p = approved_plan(db)
    rid = ex.create_run(p["id"])
    ex.run(rid)
    assert sizes and max(sizes) <= 1000


def test_only_one_run_at_a_time(db, scanned, executor, unlocked):
    p = approved_plan(db)
    executor.create_run(p["id"])
    p2 = approved_plan(db, rec="newsletters")
    with pytest.raises(ExecutionError, match="in progress"):
        executor.create_run(p2["id"])
