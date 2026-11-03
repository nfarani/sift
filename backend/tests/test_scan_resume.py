from sift.providers.base import TransientError


def test_cancel_then_resume_completes_without_duplicates(db, provider, scanner):
    sid = scanner.start()
    calls = {"n": 0}
    real = provider.get_metadata

    def cancel_after_some(ids):
        calls["n"] += 1
        if calls["n"] == 7:
            scanner.request_cancel(sid)
        return real(ids)

    provider.get_metadata = cancel_after_some
    assert scanner.run(sid) == "cancelled"
    with db.connect() as c:
        partial = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        scan = c.execute("SELECT status, checkpoint FROM scans WHERE id=?", (sid,)).fetchone()
    assert 0 < partial
    assert scan["status"] == "cancelled"
    assert scanner.resumable()["id"] == sid

    provider.get_metadata = real
    scanner.prepare_resume(sid)
    assert scanner.run(sid) == "completed"
    with db.connect() as c:
        total = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        distinct = c.execute("SELECT COUNT(DISTINCT id) FROM messages").fetchone()[0]
        scanned = c.execute("SELECT messages_scanned FROM scans WHERE id=?", (sid,)).fetchone()[0]
    with provider._conn() as c:
        expected = c.execute("SELECT COUNT(*) FROM mbox WHERE labels LIKE '%\"INBOX\"%' OR labels LIKE '%\"SENT\"%'").fetchone()[0]
    assert total == distinct == expected == scanned
    assert total > partial


def test_failed_scan_keeps_checkpoint_and_resumes(db, provider, scanner, retry):
    sid = scanner.start()
    # Enough consecutive 500s to exhaust retries on some later call.
    real = provider.get_metadata
    state = {"n": 0}

    def flaky(ids):
        state["n"] += 1
        if 10 <= state["n"] < 10 + retry.max_attempts:
            raise TransientError(500, "boom")
        return real(ids)

    provider.get_metadata = flaky
    assert scanner.run(sid) == "failed"
    with db.connect() as c:
        r = c.execute("SELECT errors, error_log FROM scans WHERE id=?", (sid,)).fetchone()
    assert r["errors"] >= retry.max_attempts  # retries + give-up are all visible
    scanner.prepare_resume(sid)
    assert scanner.run(sid) == "completed"


def test_interrupted_scan_marked_resumable_on_startup(db, scanner):
    from sift.jobs import recover_interrupted
    sid = scanner.start()  # left "running" as if the process died
    recover_interrupted(db)
    assert scanner.resumable()["id"] == sid
