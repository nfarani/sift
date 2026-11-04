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
from sift.providers.gmail_writer import GmailWriter
from sift.providers.gmail_auth import READONLY_SCOPE, AuthError, TokenStore, check_scopes

FAKE_TOKEN = "ya29.FAKE-ACCESS-TOKEN-should-never-leak"
FAKE_REFRESH = "1//FAKE-REFRESH-TOKEN-should-never-leak"


def http_error(status, reason="err"):
    body = json.dumps({"error": {"code": status, "message": reason, "errors": [{"reason": reason}]}}).encode()
    return HttpError(httplib2.Response({"status": status, "reason": reason}), body, uri="https://example.invalid")


@pytest.fixture(autouse=True)
def no_real_revoke(monkeypatch):
    """Tests must never reach oauth2.googleapis.com. Any revoke that isn't faked by the test is recorded here."""
    calls = []

    class NoNetwork:
        def __call__(self, *a, **k):
            raise AssertionError("a test tried to refresh a token over the network")

    monkeypatch.setattr(gmail_auth, "Request", NoNetwork)
    monkeypatch.setattr(gmail_auth, "revoke", lambda creds, opener=None: calls.append(creds) or True)
    monkeypatch.setattr("sift.api.revoke", lambda creds: calls.append(creds) or True)
    return calls


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
        return creds_json(self.scopes, self.token, self.refresh_token)


MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
WRITER_SCOPES = [MODIFY_SCOPE, READONLY_SCOPE]
FAKE_WRITER_TOKEN = "ya29.FAKE-WRITER-ACCESS-should-never-leak"
FAKE_WRITER_REFRESH = "1//FAKE-WRITER-REFRESH-should-never-leak"


def creds_json(scopes=(READONLY_SCOPE,), token=FAKE_TOKEN, refresh=FAKE_REFRESH):
    """Authorized-user JSON that google.oauth2.credentials can really load (no expiry => valid)."""
    return json.dumps({"token": token, "refresh_token": refresh, "client_id": "fake.apps.example",
                       "client_secret": "fake-secret", "token_uri": "https://oauth2.example.invalid/token",
                       "expiry": "2099-01-01T00:00:00Z", "scopes": list(scopes)})


class WriterCreds(FakeCreds):
    token = FAKE_WRITER_TOKEN
    refresh_token = FAKE_WRITER_REFRESH
    scopes = WRITER_SCOPES
    granted_scopes = WRITER_SCOPES


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
            if key in self.thr:
                return self.thr[key]
            return {"id": key, "messages": [{"labelIds": m["labelIds"]} for m in self.msgs.values()
                                            if m["threadId"] == key]}
        if kind == "labels.get":
            return {"id": key, "messagesTotal": sum(key in m["labelIds"] for m in self.msgs.values())}
        if kind == "labels.list":
            return {"labels": self.lbls}
        if kind == "labels.create":
            if any(l["name"].casefold() == kw["body"]["name"].casefold() for l in self.lbls):
                raise http_error(409, "duplicate")
            lbl = {"id": f"Label_{100 + len(self.lbls)}", "name": kw["body"]["name"]}
            self.lbls.append(lbl)
            return lbl
        if kind == "messages.batchModify":
            body = kw["body"]
            assert len(body["ids"]) <= 1000
            for mid in body["ids"]:
                m = self.msgs.get(mid)
                if m is None:
                    continue
                labels = [l for l in m["labelIds"] if l not in body["removeLabelIds"]]
                m["labelIds"] = labels + [l for l in body["addLabelIds"] if l not in labels]
            return {}
        if kind in ("messages.trash", "messages.untrash"):
            if key not in self.msgs:
                raise http_error(404, "notFound")
            m = self.msgs[key]
            m["labelIds"] = [l for l in m["labelIds"] if l != "TRASH"] + (["TRASH"] if kind == "messages.trash" else [])
            return {"id": key, "labelIds": m["labelIds"]}
        raise AssertionError(kind)


class _Node:
    def __init__(self, g, path):
        self.g, self.path = g, path

    def __getattr__(self, name):
        if name in ("messages", "threads", "labels"):
            return lambda: _Node(self.g, name)
        if name in ("get", "list", "getProfile", "create", "batchModify", "trash", "untrash"):
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
    kr.set_password("sift-gmail", "oauth-token", creds_json())
    msgs = {f"i{i:04}": msg(f"i{i:04}", thread=f"t{i}", subject="50% off everything",
                            labels=("INBOX", "UNREAD", "CATEGORY_PROMOTIONS"),
                            extra=[("List-Id", "<deals.shop.example>")]) for i in range(700)}
    msgs.update({f"s{i:04}": msg(f"s{i:04}", frm="Me <me@example.com>", labels=("SENT",), to="pal@example.com",
                                 thread=f"t{i}") for i in range(20)})
    g = G(msgs)
    revoked = []
    monkeypatch.setattr("sift.api.revoke", lambda creds: revoked.append(True) or True)
    state = build_state(s, retry=retry, token_store=store,
                        reader_factory=lambda creds, st: reader(g), writer_factory=fake_writer_factory(g))
    state.scanner.page_size = 100
    return TestClient(create_app(state), headers={"X-Sift": "1"}), state, g, kr, revoked


def fake_writer_factory(g, made=None):
    def make(creds, store, limiter, on_reauth):
        w = GmailWriter(creds, store, limiter=limiter or QuotaLimiter(10**9), service_factory=lambda c: g,
                        on_reauth=on_reauth)
        if made is not None:
            made.append(w)
        return w
    return make


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
    assert r == {"revoked_at_google": True, "local_token_deleted": True, "local_metadata_deleted": True,
                 "cleanup_revoked_at_google": False, "cleanup_token_deleted": True, "problems": []}
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


# =====================================================================================================
# Phase 4: GmailWriter, separate writer token, Enable/Disable cleanup, corrupt keychain entries
# =====================================================================================================
from google.auth.exceptions import RefreshError

from sift.providers import gmail_writer as writer_mod
from sift.providers.gmail_auth import (WRITER, CleanupReauthRequired, CorruptToken, ReauthRequired,
                                       WRITER_KEYRING_SERVICE, fresh)


class CountingLimiter:
    def __init__(self):
        self.units = []

    def acquire(self, units):
        self.units.append(units)


def writer(g, limiter=None, on_reauth=None, creds=None):
    return GmailWriter(creds or WriterCreds(), TokenStore(FakeKeyring(), WRITER_KEYRING_SERVICE),
                       limiter=limiter or QuotaLimiter(10**9), service_factory=lambda c: g, on_reauth=on_reauth)


def calls(g, kind):
    return [kw for k, kw in g.calls if k == kind]


# --- GmailWriter ----------------------------------------------------------------------------------
def test_writer_interface_and_no_destructive_methods():
    assert issubclass(GmailWriter, MailWriter) and not issubclass(GmailWriter, MailReader)
    assert GmailWriter.max_modify_batch == 1000
    for name in ("delete", "batch_delete", "send", "empty_trash", "draft", "get_metadata", "list_message_ids"):
        assert not hasattr(GmailWriter, name)
    src = inspect.getsource(writer_mod)
    for forbidden in (".delete(", "batchDelete", ".send(", "drafts", "emptyTrash", "format=\"full\"", "raw"):
        assert forbidden not in src


def test_ensure_label_finds_existing_case_insensitively():
    g = G({}, labels=[{"id": "INBOX", "name": "INBOX"}, {"id": "Label_7", "name": "Sift/2026-11-04-run-12"}])
    w = writer(g)
    assert w.ensure_label("sift/2026-11-04-RUN-12") == "Label_7"
    assert calls(g, "labels.create") == []


def test_ensure_label_creates_once_then_cached():
    g = G({})
    w = writer(g)
    lid = w.ensure_label("Sift/2026-11-04-run-12")
    assert w.ensure_label("Sift/2026-11-04-run-12") == lid
    created = calls(g, "labels.create")
    assert len(created) == 1
    assert created[0]["body"] == {"name": "Sift/2026-11-04-run-12", "labelListVisibility": "labelShow",
                                  "messageListVisibility": "show"}
    # A fresh writer (e.g. after restart) finds it instead of creating a duplicate.
    assert writer(g).ensure_label("Sift/2026-11-04-run-12") == lid
    assert len(calls(g, "labels.create")) == 1


def test_ensure_label_409_race_resolves_to_existing():
    g = G({})
    w = writer(g)
    real = g.handle

    def racing(kind, kw):
        if kind == "labels.create":  # someone else created it between our list and our create
            g.lbls.append({"id": "Label_55", "name": kw["body"]["name"]})
        return real(kind, kw)

    g.handle = racing
    assert w.ensure_label("Sift/x") == "Label_55"


def test_ensure_label_rejects_empty_name():
    with pytest.raises(PermanentError):
        writer(G({})).ensure_label("  ")


def test_batch_modify_body_limit_and_guards():
    g = G({"a": msg("a"), "b": msg("b")})
    w = writer(g)
    w.batch_modify(["a", "b", "a"], add=["Label_9"], remove=["INBOX"])
    body = calls(g, "messages.batchModify")[0]["body"]
    assert body == {"ids": ["a", "b"], "addLabelIds": ["Label_9"], "removeLabelIds": ["INBOX"]}
    assert "INBOX" not in g.msgs["a"]["labelIds"] and "Label_9" in g.msgs["a"]["labelIds"]
    with pytest.raises(PermanentError):
        w.batch_modify([f"m{i}" for i in range(1001)], add=["Label_9"], remove=[])
    for bad in (dict(add=["TRASH"], remove=[]), dict(add=[], remove=["TRASH"]), dict(add=["SPAM"], remove=[]),
                dict(add=[], remove=[])):
        with pytest.raises(PermanentError):
            w.batch_modify(["a"], **bad)
    w.batch_modify([], add=["Label_9"], remove=[])  # nothing to do: no call
    assert len(calls(g, "messages.batchModify")) == 1


def test_trash_untrash():
    g = G({"a": msg("a")})
    w = writer(g)
    w.trash("a")
    assert "TRASH" in g.msgs["a"]["labelIds"]
    w.untrash("a")
    assert "TRASH" not in g.msgs["a"]["labelIds"]
    with pytest.raises(PermanentError):
        w.trash("missing")


def test_writer_quota_costs_match_google_table():
    lim = CountingLimiter()
    g = G({"a": msg("a")})
    w = writer(g, limiter=lim)
    w.ensure_label("Sift/run")       # labels.list 1 + labels.create 5
    w.batch_modify(["a"], add=["INBOX"], remove=[])   # 50
    w.trash("a")                     # 20
    w.untrash("a")                   # 5
    assert lim.units == [1, 5, 50, 20, 5]


@pytest.mark.parametrize("status,reason", [(429, "rateLimitExceeded"), (500, "backendError"), (503, "x"),
                                           (403, "userRateLimitExceeded")])
def test_writer_transient_errors_for_retry_policy(status, reason):
    g = G({"a": msg("a")})
    g.fail = {"a": [(status, reason)]}
    with pytest.raises(TransientError):
        writer(g).trash("a")


def test_writer_backs_off_and_succeeds_through_retry_policy(sleeps, retry):
    g = G({"a": msg("a")})
    g.fail = {"a": [(429, "rateLimitExceeded"), (503, "x")]}
    retry.call(lambda: writer(g).trash("a"))
    assert sleeps == [0.5, 1.0] and "TRASH" in g.msgs["a"]["labelIds"]


def test_writer_permission_error_is_permanent():
    g = G({"a": msg("a")})
    g.fail = {"a": [(403, "insufficientPermissions")]}
    with pytest.raises(PermanentError) as e:
        writer(g).trash("a")
    assert not isinstance(e.value, TransientError)


def test_writer_dead_refresh_token_means_reconnect_cleanup():
    hit = []

    class Expired(WriterCreds):
        valid = False

        def refresh(self, request):
            raise RefreshError("invalid_grant: Token has been expired or revoked.")

    w = writer(G({"a": msg("a")}), on_reauth=lambda: hit.append(1), creds=Expired())
    with pytest.raises(CleanupReauthRequired) as e:
        w.trash("a")
    assert "reconnect cleanup" in str(e.value) and hit == [1]
    assert FAKE_WRITER_REFRESH not in str(e.value)


def test_writer_refresh_error_mid_request_means_reconnect_cleanup():
    class Svc(G):
        def handle(self, kind, kw):
            raise RefreshError("invalid_grant")

    hit = []
    with pytest.raises(CleanupReauthRequired):
        writer(Svc({}), on_reauth=lambda: hit.append(1)).trash("a")
    assert hit == [1]


# --- scopes and the refresh re-check ----------------------------------------------------------------
@pytest.mark.parametrize("scopes", [
    [READONLY_SCOPE],                                            # no modify
    [MODIFY_SCOPE, "https://www.googleapis.com/auth/gmail.send"],
    [MODIFY_SCOPE, "https://" + "mail.google.com/"],
    [MODIFY_SCOPE, "https://www.googleapis.com/auth/gmail.compose"],
])
def test_writer_tokens_with_wrong_scopes_refused(scopes):
    c = WriterCreds()
    c.granted_scopes = scopes
    with pytest.raises(AuthError):
        check_scopes(c, WRITER)


def test_writer_scopes_accepted():
    check_scopes(WriterCreds(), WRITER)
    c = WriterCreds()
    c.granted_scopes = [MODIFY_SCOPE]
    check_scopes(c, WRITER)


def test_enable_cleanup_consent_requests_modify_and_loopback(monkeypatch, tmp_path):
    seen = {}

    class Flow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            seen["scopes"] = scopes
            return cls()

        def run_local_server(self, **kw):
            seen.update(kw)
            return WriterCreds() if MODIFY_SCOPE in seen["scopes"] else FakeCreds()

    monkeypatch.setattr(gmail_auth, "InstalledAppFlow", Flow)
    secrets = tmp_path / "cs.json"
    secrets.write_text("{}")
    gmail_auth.run_consent_flow(secrets, role=WRITER)
    assert seen["scopes"] == [MODIFY_SCOPE, READONLY_SCOPE]
    assert seen["host"] == "127.0.0.1" and seen["port"] == 0
    # The reader consent is unchanged: read-only only.
    gmail_auth.run_consent_flow(secrets)
    assert seen["scopes"] == [READONLY_SCOPE]


@pytest.mark.parametrize("granted", [[MODIFY_SCOPE, "https://" + "mail.google.com/"],
                                     [MODIFY_SCOPE, "https://www.googleapis.com/auth/gmail.send"],
                                     [READONLY_SCOPE]])
def test_writer_consent_with_bad_scopes_refused_and_revoked(monkeypatch, tmp_path, granted):
    bad = WriterCreds()
    bad.granted_scopes = granted

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
        gmail_auth.run_consent_flow(secrets, role=WRITER)
    assert revoked == [bad]


class Refreshing(FakeCreds):
    """valid=False until refresh(); refresh() reports the given granted scopes, like google-auth does."""

    def __init__(self, scopes, granted_after):
        self.valid = False
        self.scopes = scopes
        self.granted_scopes = scopes
        self._after = granted_after

    def refresh(self, request):
        self.valid = True
        self.granted_scopes = self._after


def test_reader_refresh_that_gains_modify_is_refused_and_not_saved(monkeypatch):
    kr = FakeKeyring()
    store = TokenStore(kr)
    kr.set_password("sift-gmail", "oauth-token", "old")
    revoked = []
    monkeypatch.setattr(gmail_auth, "revoke", lambda c: revoked.append(c) or True)
    import threading
    with pytest.raises(ReauthRequired) as e:
        fresh(Refreshing([READONLY_SCOPE], [READONLY_SCOPE, MODIFY_SCOPE]), store, threading.Lock())
    assert not isinstance(e.value, CleanupReauthRequired)
    assert kr.store == {} and revoked == []  # dropped locally; modify isn't "dangerous", so no revoke


def test_writer_refresh_with_forbidden_scope_is_revoked(monkeypatch):
    kr = FakeKeyring()
    store = TokenStore(kr, WRITER_KEYRING_SERVICE)
    revoked = []
    monkeypatch.setattr(gmail_auth, "revoke", lambda c: revoked.append(c) or True)
    import threading
    c = Refreshing(WRITER_SCOPES, WRITER_SCOPES + ["https://" + "mail.google.com/"])
    with pytest.raises(CleanupReauthRequired):
        fresh(c, store, threading.Lock(), role=WRITER)
    assert revoked == [c]


def test_good_refresh_is_saved():
    import threading
    kr = FakeKeyring()
    store = TokenStore(kr, WRITER_KEYRING_SERVICE)
    fresh(Refreshing(WRITER_SCOPES, WRITER_SCOPES), store, threading.Lock(), role=WRITER)
    assert (WRITER_KEYRING_SERVICE, "oauth-token") in kr.store


# --- token storage ------------------------------------------------------------------------------------
def test_reader_and_writer_tokens_are_separate_keychain_entries():
    kr = FakeKeyring()
    r = TokenStore(kr)
    w = r.for_writer()
    kr.set_password("sift-gmail", "oauth-token", creds_json())
    kr.set_password(WRITER_KEYRING_SERVICE, "oauth-token", creds_json(WRITER_SCOPES, FAKE_WRITER_TOKEN,
                                                                       FAKE_WRITER_REFRESH))
    assert r.load().scopes == [READONLY_SCOPE]
    assert set(w.load().scopes) == set(WRITER_SCOPES)
    w.delete()
    assert w.load() is None and r.load() is not None


@pytest.mark.parametrize("raw", ["{not json", '{"token": "x"}', "[]"])
def test_corrupt_entry_raises_then_is_discarded(raw):
    kr = FakeKeyring()
    store = TokenStore(kr)
    kr.set_password("sift-gmail", "oauth-token", raw)
    with pytest.raises(CorruptToken) as e:
        store.load()
    assert raw not in str(e.value)
    creds, problem = store.load_or_discard()
    assert creds is None and "removed" in problem and kr.store == {}


class BrokenKeyring(FakeKeyring):
    def get_password(self, s, u):
        raise RuntimeError("keychain locked")

    def delete_password(self, s, u):
        raise RuntimeError("keychain locked")


def test_unreadable_and_undeletable_entry_is_reported_not_raised():
    creds, problem = TokenStore(BrokenKeyring()).load_or_discard()
    assert creds is None and "Keychain Access" in problem


# --- API: Enable / Disable cleanup ---------------------------------------------------------------------
@pytest.fixture
def consent(monkeypatch, gmail_app):
    """Fake the browser consent: returns whatever creds the test sets."""
    box = {"creds": WriterCreds(), "roles": []}

    def flow(path, timeout_s=300, role="reader"):
        box["roles"].append(role)
        return box["creds"]

    monkeypatch.setattr("sift.api.run_consent_flow", flow)
    gmail_app[0].app.state.sift.settings.client_secrets.write_text("{}")
    return box


def wait_cleanup(c, done=("enabled", "error", "disabled", "reauth_required")):
    for _ in range(200):
        st = c.get("/api/status").json()
        if st["cleanup"]["state"] in done:
            return st
        time.sleep(0.02)
    raise AssertionError("cleanup flow timeout")


def enable(c):
    r = c.post("/api/cleanup/enable")
    assert r.status_code == 200, r.text
    return wait_cleanup(c)


def backdate_enable(state):
    """Plans/batches must be approved strictly after cleanup was enabled (1 s resolution). Tests that
    approve within the same second move the enable time back instead of sleeping."""
    state.db.set_setting("cleanup.enabled_at", "2000-01-01T00:00:00+00:00")


def test_enable_cleanup_stores_separate_writer_token(gmail_app, consent):
    c, state, g, kr, _ = gmail_app
    st = c.get("/api/status").json()
    assert st["can_modify"] is False and st["cleanup"]["state"] == "disabled"
    st = enable(c)
    assert consent["roles"] == [WRITER]
    assert st["can_modify"] is True and st["cleanup"]["state"] == "enabled" and "gmail.modify" in st["mode"]
    assert isinstance(state.writer, GmailWriter) and state.executor.writer is state.writer
    # Reader keeps its read-only token; the writer has its own entry.
    assert json.loads(kr.store[("sift-gmail", "oauth-token")])["scopes"] == [READONLY_SCOPE]
    assert set(json.loads(kr.store[(WRITER_KEYRING_SERVICE, "oauth-token")])["scopes"]) == set(WRITER_SCOPES)
    assert state.db.get_setting("cleanup.enabled_at")
    blob = json.dumps(st)
    for secret in (FAKE_TOKEN, FAKE_REFRESH, FAKE_WRITER_TOKEN, FAKE_WRITER_REFRESH):
        assert secret not in blob
    # Writer shares the reader's quota bucket.
    assert state.writer.limiter is state.provider.limiter


def test_enable_cleanup_needs_reader_and_gmail_mode(gmail_app, consent, tmp_path, provider, retry):
    c, state, *_ = gmail_app
    state.attach(None, None)
    state.connection = {"state": "disconnected", "error": None}
    assert c.post("/api/cleanup/enable").status_code == 409
    s = Settings()
    s.data_dir = tmp_path / "m"
    s.extra_allowed_hosts = ("testserver",)
    mock = TestClient(create_app(build_state(s, provider=provider, retry=retry)), headers={"X-Sift": "1"})
    assert mock.post("/api/cleanup/enable").status_code == 404
    assert mock.get("/api/status").json()["cleanup"]["state"] == "enabled"  # synthetic: can modify synthetic data


def test_enable_cleanup_for_other_account_refused(gmail_app, consent, monkeypatch):
    c, state, g, kr, _ = gmail_app
    c.get("/api/status")  # caches nick@example.com
    other = G({}, labels=[])
    other_handle = other.handle
    other.handle = lambda kind, kw: ({"emailAddress": "someone@else.example"} if kind == "getProfile"
                                     else other_handle(kind, kw))
    state.writer_factory = fake_writer_factory(other)
    revoked = []
    monkeypatch.setattr("sift.api.revoke", lambda creds: revoked.append(creds) or True)
    st = enable(c)
    assert st["cleanup"]["state"] == "error" and "different Google account" in st["cleanup"]["error"]
    assert st["can_modify"] is False and state.writer is None
    assert (WRITER_KEYRING_SERVICE, "oauth-token") not in kr.store and revoked == [consent["creds"]]


def test_enable_cleanup_bad_scopes_reported(gmail_app, monkeypatch):
    c, state, *_ = gmail_app
    state.settings.client_secrets.write_text("{}")

    def refused(path, timeout_s=300, role="reader"):
        raise AuthError("refusing token with unexpected scopes: ['x']")

    monkeypatch.setattr("sift.api.run_consent_flow", refused)
    st = enable(c)
    assert st["cleanup"]["state"] == "error" and "unexpected scopes" in st["cleanup"]["error"]
    assert st["can_modify"] is False


def test_disable_cleanup_deletes_only_writer(gmail_app, consent):
    c, state, g, kr, revoked = gmail_app
    enable(c)
    r = c.post("/api/cleanup/disable").json()
    assert r["revoked_at_google"] is True and r["local_writer_token_deleted"] is True
    assert r["status"]["can_modify"] is False and r["status"]["cleanup"]["state"] == "disabled"
    assert (WRITER_KEYRING_SERVICE, "oauth-token") not in kr.store
    assert ("sift-gmail", "oauth-token") in kr.store
    assert state.writer is None and state.provider is not None and state.executor.writer is None
    assert c.get("/api/status").json()["connection"]["state"] == "connected"


def test_startup_attaches_writer_only_with_valid_token(tmp_path, retry):
    def boot(kr):
        s = Settings()
        s.data_dir, s.provider, s.extra_allowed_hosts = tmp_path, "gmail", ("testserver",)
        g = G({})
        return build_state(s, retry=retry, token_store=TokenStore(kr), reader_factory=lambda cr, st: reader(g),
                           writer_factory=fake_writer_factory(g))

    kr = FakeKeyring()
    kr.set_password("sift-gmail", "oauth-token", creds_json())
    st = boot(kr)
    assert st.writer is None and st.cleanup["state"] == "disabled"

    kr.set_password(WRITER_KEYRING_SERVICE, "oauth-token", creds_json(WRITER_SCOPES))
    st = boot(kr)
    assert isinstance(st.writer, GmailWriter) and st.cleanup["state"] == "enabled"
    assert st.db.get_setting("cleanup.enabled_at")

    # Writer token without modify: refused and deleted, reader unaffected.
    kr.set_password(WRITER_KEYRING_SERVICE, "oauth-token", creds_json([READONLY_SCOPE]))
    st = boot(kr)
    assert st.writer is None and st.cleanup["state"] == "error" and st.connection["state"] == "connected"
    assert (WRITER_KEYRING_SERVICE, "oauth-token") not in kr.store

    # Writer token alone (no reader): never attached.
    kr2 = FakeKeyring()
    kr2.set_password(WRITER_KEYRING_SERVICE, "oauth-token", creds_json(WRITER_SCOPES))
    st = boot(kr2)
    assert st.writer is None and st.provider is None


def test_startup_survives_corrupt_entries(tmp_path, retry):
    kr = FakeKeyring()
    kr.set_password("sift-gmail", "oauth-token", "{garbage")
    kr.set_password(WRITER_KEYRING_SERVICE, "oauth-token", "also garbage")
    s = Settings()
    s.data_dir, s.provider, s.extra_allowed_hosts = tmp_path, "gmail", ("testserver",)
    state = build_state(s, retry=retry, token_store=TokenStore(kr), reader_factory=lambda cr, st: reader(G({})))
    assert state.provider is None and state.connection["state"] == "disconnected"
    assert "unreadable" in state.connection["error"] and kr.store == {}
    assert state.cleanup["state"] == "error"
    c = TestClient(create_app(state), headers={"X-Sift": "1"})
    st = c.get("/api/status").json()
    assert st["can_modify"] is False and "garbage" not in json.dumps(st)


def test_disconnect_survives_corrupt_entries(gmail_app, consent):
    c, state, g, kr, revoked = gmail_app
    enable(c)
    kr.set_password("sift-gmail", "oauth-token", "{broken")
    kr.set_password(WRITER_KEYRING_SERVICE, "oauth-token", "{broken")
    r = c.post("/api/gmail/disconnect")
    assert r.status_code == 200
    d = r.json()
    assert d["local_token_deleted"] and d["cleanup_token_deleted"] and len(d["problems"]) == 2
    assert kr.store == {} and state.writer is None and state.provider is None


def test_disconnect_also_removes_writer(gmail_app, consent, monkeypatch):
    c, state, g, kr, _ = gmail_app
    enable(c)
    revoked = []
    monkeypatch.setattr("sift.api.revoke", lambda creds: revoked.append(creds) or True)
    d = c.post("/api/gmail/disconnect").json()
    assert d["revoked_at_google"] and d["cleanup_revoked_at_google"] and len(revoked) == 2
    assert kr.store == {} and c.get("/api/status").json()["can_modify"] is False


def test_reconnect_same_account_keeps_cleanup_other_account_drops_it(gmail_app, consent, monkeypatch):
    c, state, g, kr, _ = gmail_app
    enable(c)
    # Reconnect the reader (same account): writer re-attached after re-checking its account.
    consent["creds"] = FakeCreds()
    c.post("/api/gmail/connect")
    for _ in range(200):
        if c.get("/api/status").json()["connection"]["state"] == "connected":
            break
        time.sleep(0.02)
    time.sleep(0.05)
    st = c.get("/api/status").json()
    assert st["can_modify"] is True and (WRITER_KEYRING_SERVICE, "oauth-token") in kr.store
    # Reconnect as a different account: cleanup token revoked and deleted.
    other = G({})
    oh = other.handle
    other.handle = lambda kind, kw: {"emailAddress": "b@other.example"} if kind == "getProfile" else oh(kind, kw)
    state.reader_factory = lambda creds, st: reader(other)
    c.post("/api/gmail/connect")
    for _ in range(200):
        st = c.get("/api/status").json()
        if st["connection"]["state"] == "connected" and st["account"] == "b@other.example":
            break
        time.sleep(0.02)
    st = c.get("/api/status").json()
    assert st["account"] == "b@other.example" and st["can_modify"] is False
    assert (WRITER_KEYRING_SERVICE, "oauth-token") not in kr.store


# --- end to end: execute and restore through GmailWriter against the fake Gmail ---------------------
def wait_run(c, run_id):
    for _ in range(500):
        d = c.get(f"/api/runs/{run_id}").json()
        if d["status"] != "running":
            return d
        time.sleep(0.02)
    raise AssertionError("run timeout")


def test_capped_run_archives_and_labels_via_gmail_writer(gmail_app, consent):
    c, state, g, kr, _ = gmail_app
    c.post("/api/scan")  # full scan: the executor refuses capped test scans
    assert wait_scan(c)["status"] == "completed"
    enable(c)
    backdate_enable(state)
    recs = [r for r in c.get("/api/recommendations").json()["items"] if r["action"] != "review" and r["count"]]
    p = c.post("/api/plans", json={"items": [{"rec_id": recs[0]["id"]}]}).json()
    c.post(f"/api/plans/{p['id']}/approve", json={"confirm_hash": p["ids_hash"]})
    r = c.post(f"/api/plans/{p['id']}/execute")
    assert r.status_code == 200, r.text
    run = wait_run(c, r.json()["id"])
    assert run["status"] in ("completed", "partial") and run["confirmed"] == 50  # first-run cap
    label = next(l for l in g.lbls if l["name"] == run["label_name"])
    archived = [m for m in g.msgs.values() if label["id"] in m["labelIds"]]
    assert len(archived) == 50 and all("INBOX" not in m["labelIds"] for m in archived)
    mods = calls(g, "messages.batchModify")
    assert mods and all(len(kw["body"]["ids"]) <= 500 for kw in mods)
    assert calls(g, "labels.create") and len(calls(g, "labels.create")) == 1
    # Restore puts them back in the inbox.
    rr = c.post(f"/api/runs/{run['id']}/restore")
    assert rr.status_code == 200, rr.text
    wait_run(c, rr.json()["id"])
    assert all("INBOX" in m["labelIds"] for m in archived)


def test_writer_expiry_mid_run_surfaces_reconnect_cleanup(gmail_app, consent):
    c, state, g, kr, _ = gmail_app
    c.post("/api/scan")
    wait_scan(c)
    enable(c)
    backdate_enable(state)

    class Expired(WriterCreds):  # the writer's refresh token died (Testing mode: 7 days); the reader's is fine
        valid = False

        def refresh(self, request):
            raise RefreshError("invalid_grant: Token has been expired or revoked.")

    state.writer._creds = Expired()
    recs = [r for r in c.get("/api/recommendations").json()["items"] if r["action"] != "review" and r["count"]]
    p = c.post("/api/plans", json={"items": [{"rec_id": recs[0]["id"]}]}).json()
    c.post(f"/api/plans/{p['id']}/approve", json={"confirm_hash": p["ids_hash"]})
    r = c.post(f"/api/plans/{p['id']}/execute")  # refused up front: the writer's account check needs a live token
    assert r.status_code == 400 and "reconnect cleanup" in r.json()["detail"]
    st = c.get("/api/status").json()
    assert st["cleanup"]["state"] == "reauth_required" and "reconnect cleanup" in st["cleanup"]["error"]
    assert st["can_modify"] is False and st["connection"]["state"] == "connected"
    assert (WRITER_KEYRING_SERVICE, "oauth-token") not in kr.store
    assert c.post(f"/api/plans/{p['id']}/execute").status_code == 400  # no writer any more
