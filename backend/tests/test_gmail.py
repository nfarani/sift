"""GmailReader, OAuth and read-only mode, against a fake Gmail service (no network, no real account)."""
import inspect
import json
import logging
import time
from pathlib import Path

import httplib2
import pytest
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError

from sift.api import build_state, create_app
from sift.config import Settings
from sift.providers import gmail as gmail_mod
from sift.providers import gmail_auth
from sift.providers.base import MailReader, MailWriter, PermanentError, TransientError
from sift.providers.gmail import GmailReader, QuotaLimiter, _parse
from sift.providers.gmail_auth import READONLY_SCOPE, AuthError, TokenStore, check_scopes

FAKE_TOKEN = "ya29.FAKE-ACCESS-TOKEN-should-never-leak"
FAKE_REFRESH = "1//FAKE-REFRESH-TOKEN-should-never-leak"


def http_error(status, reason="err"):
    body = json.dumps({"error": {"code": status, "message": reason, "errors": [{"reason": reason}]}}).encode()
    return HttpError(httplib2.Response({"status": status, "reason": reason}), body, uri="https://example.invalid")


# --- fake Gmail service ---------------------------------------------------------------------------
class Req:
    def __init__(self, svc, kind, kwargs):
        self.svc, self.kind, self.kwargs = svc, kind, kwargs

    def execute(self, num_retries=0):
        self.svc.calls.append((self.kind, self.kwargs))
        return self.svc.handle(self.kind, self.kwargs)


class Batch:
    def __init__(self, svc, callback):
        self.svc, self.cb, self.items = svc, callback, []

    def add(self, req, request_id=None):
        self.items.append((request_id, req))

    def execute(self):
        self.svc.batch_sizes.append(len(self.items))
        for rid, req in self.items:
            try:
                self.cb(rid, req.execute(), None)
            except HttpError as e:
                self.cb(rid, None, e)


def msg(mid, frm="Shop <deals@shop.example>", subject="Sale", labels=("INBOX", "UNREAD"), to="me@example.com",
        cc=None, bcc=None, thread=None, extra=()):
    headers = [{"name": "From", "value": frm}, {"name": "To", "value": to}, {"name": "Subject", "value": subject}]
    if cc:
        headers.append({"name": "Cc", "value": cc})
    if bcc:
        headers.append({"name": "Bcc", "value": bcc})
    headers += [{"name": n, "value": v} for n, v in extra]
    return {"id": mid, "threadId": thread or mid, "labelIds": list(labels), "snippet": "x" * 300,
            "internalDate": "1700000000000", "payload": {"headers": headers}}


class FakeCreds:
    valid = True
    token = FAKE_TOKEN
    refresh_token = FAKE_REFRESH
    scopes = [READONLY_SCOPE]
    granted_scopes = [READONLY_SCOPE]

    def to_json(self):
        return json.dumps({"token": self.token, "refresh_token": self.refresh_token, "scopes": self.scopes})


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, s, u): return self.store.get((s, u))
    def set_password(self, s, u, p): self.store[(s, u)] = p
    def delete_password(self, s, u): self.store.pop((s, u), None)


class G:
    """Minimal fake of googleapiclient's Gmail resource tree."""

    def __init__(self, messages, threads=None, labels=None):
        self.msgs = messages
        self.thr = threads or {}
        self.lbls = labels or [{"id": "INBOX", "name": "INBOX"}, {"id": "Label_9", "name": "Work"}]
        self.calls, self.batch_sizes, self.fail = [], [], {}

    def users(self): return _Node(self, "")
    def new_batch_http_request(self, callback): return Batch(self, callback)

    def handle(self, kind, kw):
        key = kw.get("id")
        if self.fail.get(key):
            raise http_error(*self.fail[key].pop(0))
        if kind == "getProfile":
            return {"emailAddress": "Nick@Example.com", "messagesTotal": len(self.msgs), "threadsTotal": 3}
        if kind == "messages.get":
            assert kw["format"] == "metadata", "bodies must never be requested"
            if key not in self.msgs:
                raise http_error(404, "notFound")
            return self.msgs[key]
        if kind == "messages.list":
            ids = sorted(i for i, m in self.msgs.items() if set(kw["labelIds"]) <= set(m["labelIds"]))
            start = int(kw.get("pageToken") or 0)
            page = ids[start:start + kw["maxResults"]]
            nxt = str(start + len(page)) if start + len(page) < len(ids) else None
            return {"messages": [{"id": i} for i in page], "nextPageToken": nxt, "resultSizeEstimate": len(ids)}
        if kind == "threads.get":
            assert kw["format"] == "minimal"
            return self.thr[key]
        if kind == "labels.get":
            return {"id": key, "messagesTotal": sum(key in m["labelIds"] for m in self.msgs.values())}
        if kind == "labels.list":
            return {"labels": self.lbls}
        raise AssertionError(kind)


class _Node:
    def __init__(self, g, path):
        self.g, self.path = g, path

    def __getattr__(self, name):
        if name in ("messages", "threads", "labels"):
            return lambda: _Node(self.g, name)
        if name in ("get", "list", "getProfile"):
            kind = f"{self.path}.{name}" if self.path else name
            return lambda **kw: Req(self.g, kind, kw)
        raise AttributeError(name)


def reader(g, sleeps=None, limiter=None):
    return GmailReader(FakeCreds(), TokenStore(FakeKeyring()), limiter=limiter or QuotaLimiter(10**9),
                       service_factory=lambda c: g, sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


# --- parsing ------------------------------------------------------------------------------------------
def test_parse_headers_and_all_recipients():
    m = _parse(msg("a", frm='"Maya O." <Maya@Brightlane.example>', to="me@example.com, x@y.example",
                   cc="Boss <boss@work.example>", bcc="secret@z.example",
                   extra=[("List-Id", "<news.example>"), ("List-Unsubscribe", "<https://u.example/1>"),
                          ("List-Unsubscribe-Post", "List-Unsubscribe=One-Click")]))
    assert m.from_addr == "maya@brightlane.example" and m.from_name == "Maya O."
    assert set(m.to_addrs) == {"me@example.com", "x@y.example", "boss@work.example", "secret@z.example"}
    assert m.list_id == "<news.example>" and m.list_unsubscribe_post == "List-Unsubscribe=One-Click"
    assert len(m.snippet) == 200 and m.internal_date == 1_700_000_000_000 and "UNREAD" in m.labels


# --- reads --------------------------------------------------------------------------------------------
def test_metadata_only_batched_and_404_dropped():
    g = G({f"m{i:03}": msg(f"m{i:03}") for i in range(120)})
    r = reader(g)
    out = r.get_metadata([f"m{i:03}" for i in range(120)] + ["gone"])
    assert len(out) == 120
    assert max(g.batch_sizes) <= 50
    gets = [kw for kind, kw in g.calls if kind == "messages.get"]
    assert gets and all(kw["format"] == "metadata" for kw in gets)


def test_rate_limited_subrequests_retried_alone():
    g = G({"a": msg("a"), "b": msg("b"), "c": msg("c")})
    g.fail = {"b": [(429, "rateLimitExceeded")], "c": [(403, "userRateLimitExceeded")]}
    sleeps = []
    out = reader(g, sleeps).get_metadata(["a", "b", "c"])
    assert {m.id for m in out} == {"a", "b", "c"}
    assert [kw["id"] for k, kw in g.calls if k == "messages.get"].count("a") == 1  # successes not re-fetched
    assert sleeps == [0.5]


def test_persistent_rate_limit_raises_transient():
    g = G({"a": msg("a")})
    g.fail = {"a": [(429, "rateLimitExceeded")] * 10}
    with pytest.raises(TransientError):
        reader(g).get_metadata(["a"])


def test_permission_error_is_permanent():
    g = G({"a": msg("a")})
    g.fail = {"a": [(403, "insufficientPermissions")]}
    with pytest.raises(PermanentError):
        reader(g).get_metadata(["a"])


def test_list_count_profile_labels_threads():
    g = G({"a": msg("a", thread="t1"), "b": msg("b", labels=("SENT",), thread="t2")},
          threads={"t1": {"messages": [{"labelIds": ["INBOX"]}]}, "t2": {"messages": [{"labelIds": ["SENT"]}]}})
    r = reader(g)
    assert r.account_email() == "nick@example.com"
    page = r.list_message_ids("INBOX", None, 1)
    assert page.ids == ["a"] and page.next_page_token is None  # only "a" carries INBOX
    assert r.count_label("SENT") == 1
    assert r.label_names()["Label_9"] == "Work"
    assert r.threads_with_sent(["t1", "t2"]) == {"t2"}


def test_quota_limiter_holds_rate():
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    q = QuotaLimiter(units_per_minute=6000, clock=lambda: t[0], sleep=sleep)
    for _ in range(30):
        q.acquire(20 * 50)  # a 50-message metadata batch
    # 30,000 units at 100 units/s, minus the 1,000-unit burst allowance: ~290 s
    assert 285 <= t[0] <= 295


# --- the reader can't write ------------------------------------------------------------------------
def test_gmail_reader_has_no_write_methods():
    assert issubclass(GmailReader, MailReader) and not issubclass(GmailReader, MailWriter)
    for name in ("batch_modify", "trash", "untrash", "ensure_label", "modify", "delete", "send"):
        assert not hasattr(GmailReader, name)
    src = inspect.getsource(gmail_mod)
    for forbidden in ("batchModify", ".modify(", ".trash(", ".untrash(", ".delete(", ".send(", "drafts"):
        assert forbidden not in src


# --- scopes and tokens -------------------------------------------------------------------------------
def test_only_readonly_scope_requested(monkeypatch, tmp_path):
    assert gmail_auth.ALLOWED_SCOPES == {"https://www.googleapis.com/auth/gmail.readonly"}
    seen = {}

    class Flow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            seen["scopes"] = scopes
            return cls()

        def run_local_server(self, **kw):
            seen.update(kw)
            return FakeCreds()

    monkeypatch.setattr(gmail_auth, "InstalledAppFlow", Flow)
    secrets = tmp_path / "client_secret.json"
    secrets.write_text("{}")
    gmail_auth.run_consent_flow(secrets)
    assert seen["scopes"] == [READONLY_SCOPE]
    assert seen["host"] == "127.0.0.1" and seen["port"] == 0  # loopback, random port


@pytest.mark.parametrize("scopes", [
    ["https://www.googleapis.com/auth/gmail.modify"],
    [READONLY_SCOPE, "https://www.googleapis.com/auth/gmail.send"],
    [READONLY_SCOPE, "https://" + "mail.google.com/"],
])
def test_tokens_with_extra_scopes_refused(scopes):
    c = FakeCreds()
    c.granted_scopes = scopes
    with pytest.raises(AuthError):
        check_scopes(c)


def test_token_store_roundtrip_and_delete():
    kr = FakeKeyring()
    store = TokenStore(kr)
    store.save(FakeCreds())
    raw = kr.get_password("sift-gmail", "oauth-token")
    assert FAKE_REFRESH in raw  # stored in the keychain backend...
    store.delete()
    assert store.load() is None


def test_tokens_never_in_repo():
    root = Path(__file__).resolve().parents[2]
    hits = [p for p in root.rglob("*") if p.is_file() and ".venv" not in p.parts and "node_modules" not in p.parts
            and ".git" not in p.parts and p.suffix in (".json", ".pickle") and "token" in p.name.lower()]
    assert hits == []


# --- read-only Gmail mode through the API ------------------------------------------------------------
@pytest.fixture
def gmail_app(tmp_path, monkeypatch, retry):
    s = Settings()
    s.data_dir = tmp_path
    s.provider = "gmail"
    s.extra_allowed_hosts = ("testserver",)
    s.client_secrets = tmp_path / "client_secret.json"
    kr = FakeKeyring()
    store = TokenStore(kr)
    kr.set_password("sift-gmail", "oauth-token", FakeCreds().to_json())
    monkeypatch.setattr(TokenStore, "load", lambda self: FakeCreds() if self.backend.store else None)
    msgs = {f"i{i:04}": msg(f"i{i:04}", thread=f"t{i}", subject="50% off everything",
                            labels=("INBOX", "UNREAD", "CATEGORY_PROMOTIONS"),
                            extra=[("List-Id", "<deals.shop.example>")]) for i in range(700)}
    msgs.update({f"s{i:04}": msg(f"s{i:04}", frm="Me <me@example.com>", labels=("SENT",), to="pal@example.com",
                                 thread=f"t{i}") for i in range(20)})
    g = G(msgs)
    revoked = []
    monkeypatch.setattr("sift.api.revoke", lambda creds: revoked.append(True) or True)
    state = build_state(s, retry=retry, token_store=store,
                        reader_factory=lambda creds, st: reader(g))
    state.scanner.page_size = 100
    return TestClient(create_app(state), headers={"X-Sift": "1"}), state, g, kr, revoked


def wait_scan(c):
    for _ in range(400):
        d = c.get("/api/scan").json()
        if d["status"] != "running":
            return d
        time.sleep(0.02)
    raise AssertionError("scan timeout")


def test_gmail_mode_is_read_only(gmail_app):
    c, state, g, kr, _ = gmail_app
    st = c.get("/api/status").json()
    assert st["connection"]["state"] == "connected" and st["account"] == "nick@example.com"
    assert st["can_modify"] is False and st["synthetic"] is False and "read-only" in st["mode"]
    assert state.writer is None and state.executor.writer is None
    assert FAKE_TOKEN not in json.dumps(st) and FAKE_REFRESH not in json.dumps(st)
    c.post("/api/scan", json={"max_messages": 500})
    assert wait_scan(c)["status"] == "completed"
    recs = [r for r in c.get("/api/recommendations").json()["items"] if r["action"] != "review" and r["count"]]
    assert recs, "fixture should yield at least one actionable recommendation"
    plan = c.post("/api/plans", json={"items": [{"rec_id": recs[0]["id"]}]})
    assert plan.status_code == 200
    p = plan.json()
    c.post(f"/api/plans/{p['id']}/approve", json={"confirm_hash": p["ids_hash"]})
    r = c.post(f"/api/plans/{p['id']}/execute")
    assert r.status_code == 400 and "read-only" in r.json()["detail"]


def test_capped_scan_reads_at_most_500_per_phase(gmail_app):
    c, state, g, _, _ = gmail_app
    c.post("/api/scan", json={"max_messages": 500})
    d = wait_scan(c)
    assert d["status"] == "completed"
    with state.db.connect() as conn:
        inbox = conn.execute("SELECT COUNT(*) FROM messages WHERE is_sent=0").fetchone()[0]
        sent = conn.execute("SELECT COUNT(*) FROM messages WHERE is_sent=1").fetchone()[0]
    assert inbox == 500 and sent == 20
    assert d["total_estimate"] == d["messages_scanned"] == 520
    fetched = [kw["id"] for k, kw in g.calls if k == "messages.get"]
    assert len(fetched) == 520  # nothing beyond the cap was fetched


def test_disconnect_revokes_deletes_token_and_local_metadata(gmail_app):
    c, state, g, kr, revoked = gmail_app
    c.post("/api/scan", json={"max_messages": 50})
    wait_scan(c)
    r = c.post("/api/gmail/disconnect").json()
    assert r == {"revoked_at_google": True, "local_token_deleted": True, "local_metadata_deleted": True}
    assert revoked and kr.store == {}
    with state.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    st = c.get("/api/status").json()
    assert st["connection"]["state"] == "disconnected" and st["account"] is None
    assert c.post("/api/scan").status_code == 409


def test_no_tokens_in_logs(gmail_app, caplog):
    c, *_ = gmail_app
    with caplog.at_level(logging.DEBUG):
        c.get("/api/status")
        c.post("/api/scan", json={"max_messages": 10})
        wait_scan(c)
        c.post("/api/gmail/disconnect")
    assert FAKE_TOKEN not in caplog.text and FAKE_REFRESH not in caplog.text


def test_gmail_and_mock_use_separate_databases(tmp_path):
    a, b = Settings(), Settings()
    a.data_dir = b.data_dir = tmp_path
    a.provider, b.provider = "mock", "gmail"
    assert a.app_db != b.app_db


def test_label_name_rule_resolves_to_label_id(db, scanned):
    from sift.protection import load_context
    db.set_setting("labels.map", {"Label_9": "Work", "INBOX": "INBOX"})
    with db.connect() as c:
        c.execute("INSERT INTO protection_rules(kind,value,created_at) VALUES ('label','work','now')")
    ctx = load_context(db, "you@sift-demo.test")
    assert "Label_9" in ctx.protected_labels


def test_scan_reports_throughput_and_eta(db, provider, scanner):
    provider.latency = 0.02  # long enough (>1 s) for a throughput measurement
    sid = scanner.start()
    scanner.run(sid)
    with db.connect() as c:
        r = c.execute("SELECT rate_mps, eta_s FROM scans WHERE id=?", (sid,)).fetchone()
    assert r["rate_mps"] and r["rate_mps"] > 0



def test_default_limiter_admits_full_batches():
    """Review B1: a 50-message batch (1,000 units) must get through the default 5,400/min bucket."""
    t = [0.0]

    def sleep(x):
        t[0] += x

    q = QuotaLimiter(clock=lambda: t[0], sleep=sleep)  # production default
    for _ in range(10):
        q.acquire(20 * 50)
    assert t[0] < 120  # 10,000 units at 90 units/s, minus burst ≈ 101 s; crucially, it returns
    q.acquire(40 * 50)  # a threads.get batch (2,000 units) too


def test_production_reader_uses_default_limiter_end_to_end():
    g = G({f"m{i:03}": msg(f"m{i:03}") for i in range(50)})
    r = GmailReader(FakeCreds(), TokenStore(FakeKeyring()), service_factory=lambda c: g)
    t0 = time.monotonic()
    assert len(r.get_metadata(sorted(g.msgs))) == 50
    assert time.monotonic() - t0 < 5


def test_oauth_library_logs_silenced():
    """Review B2: the redirect handler's request line (with the auth code) must not reach the log."""
    from sift.api import configure_logging
    configure_logging()
    lg = logging.getLogger("google_auth_oauthlib.flow")
    assert not lg.isEnabledFor(logging.INFO)


def test_refused_token_is_revoked(monkeypatch, tmp_path):
    bad = FakeCreds()
    bad.granted_scopes = [READONLY_SCOPE, "https://www.googleapis.com/auth/gmail.modify"]

    class Flow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            return cls()

        def run_local_server(self, **kw):
            return bad

    revoked = []
    monkeypatch.setattr(gmail_auth, "InstalledAppFlow", Flow)
    monkeypatch.setattr(gmail_auth, "revoke", lambda c: revoked.append(c) or True)
    secrets = tmp_path / "cs.json"
    secrets.write_text("{}")
    with pytest.raises(AuthError):
        gmail_auth.run_consent_flow(secrets)
    assert revoked == [bad]


def test_expired_signin_flips_to_reconnect(gmail_app):
    c, state, g, _, _ = gmail_app
    from sift.providers.gmail_auth import ReauthRequired

    def expired(*a, **k):
        raise ReauthRequired("Google sign-in expired; reconnect Gmail")

    state.provider.count_label = expired
    c.post("/api/scan", json={"max_messages": 10})
    assert wait_scan(c)["status"] == "failed"
    for _ in range(100):  # the job thread flips the state right after recording the failure
        if c.get("/api/status").json()["connection"]["state"] == "reauth_required":
            return
        time.sleep(0.02)
    raise AssertionError("connection never flipped to reauth_required")


def test_refresh_error_mid_request_means_reconnect():
    from google.auth.exceptions import RefreshError
    from sift.providers.gmail import _classify
    from sift.providers.gmail_auth import ReauthRequired
    assert isinstance(_classify(RefreshError("invalid_grant")), ReauthRequired)


def test_limiter_never_exceeds_gmail_minute_quota():
    t = [0.0]

    def sleep(x):
        t[0] += x

    q = QuotaLimiter(clock=lambda: t[0], sleep=sleep)
    spent = []
    while t[0] < 60:
        q.acquire(20)
        spent.append((t[0], 20))
    in_first_minute = sum(u for ts, u in spent if ts <= 60)
    assert in_first_minute <= 6000


def test_no_scan_while_connecting(gmail_app):
    c, state, *_ = gmail_app
    state.connection = {"state": "connecting", "error": None}
    assert c.post("/api/scan").status_code == 409
