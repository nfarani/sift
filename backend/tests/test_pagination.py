from sift.providers.mock import MockProvider


def test_list_pages_cover_every_inbox_message_once(provider):
    seen, token, pages = [], None, 0
    while True:
        page = provider.list_message_ids("INBOX", token, max_results=137)
        seen += page.ids
        pages += 1
        token = page.next_page_token
        if token is None:
            break
    with provider._conn() as c:
        expected = {r[0] for r in c.execute("SELECT id FROM mbox WHERE labels LIKE '%\"INBOX\"%'")}
    assert len(seen) == len(set(seen)), "duplicate ids across pages"
    assert set(seen) == expected
    assert pages == len(expected) // 137 + 1  # a full last page yields one empty trailing page


def test_scanner_stores_inbox_and_sent(db, provider, scanned):
    with db.connect() as c:
        stored = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        sent = c.execute("SELECT COUNT(*) FROM messages WHERE is_sent=1").fetchone()[0]
        scan = c.execute("SELECT * FROM scans WHERE id=?", (scanned,)).fetchone()
    with provider._conn() as c:
        expected = c.execute("SELECT COUNT(*) FROM mbox WHERE labels LIKE '%\"INBOX\"%' OR labels LIKE '%\"SENT\"%'").fetchone()[0]
    assert stored == expected
    assert sent > 0
    assert scan["messages_scanned"] == stored
    assert scan["status"] == "completed"


def test_metadata_read_never_marks_read(provider):
    page = provider.list_message_ids("UNREAD", None, 100)
    provider.get_metadata(page.ids)
    after = provider.get_metadata(page.ids)
    assert all("UNREAD" in m.labels for m in after)


def test_metadata_omits_unknown_ids(provider):
    page = provider.list_message_ids("INBOX", None, 3)
    out = provider.get_metadata(page.ids + ["doesnotexist"])
    assert [m.id for m in out] == page.ids


def test_generator_is_deterministic(tmp_path):
    a = MockProvider(tmp_path / "a.sqlite", size=500, seed=3)
    b = MockProvider(tmp_path / "b.sqlite", size=500, seed=3)
    pa = a.list_message_ids("INBOX", None, 1000).ids
    pb = b.list_message_ids("INBOX", None, 1000).ids
    assert sorted(pa) == sorted(pb)


def test_full_size_mailbox_is_about_45k(tmp_path):
    p = MockProvider(tmp_path / "big.sqlite", size=45_000, seed=1234)
    assert 43_000 <= p.profile()["messages_total"] <= 47_000


def test_scan_estimate_matches_what_is_scanned(db, scanned):
    with db.connect() as c:
        r = c.execute("SELECT total_estimate, messages_scanned FROM scans WHERE id=?", (scanned,)).fetchone()
    assert r["total_estimate"] == r["messages_scanned"]
