"""RFC 8058 one-click unsubscribe: parsing, SSRF checks, the single POST, frozen batches with their own
approval, and the append-only log. No network: resolvers and transports are fakes."""
import json
import socket
import sqlite3
import ssl
import time

import pytest
from fastapi.testclient import TestClient

from sift import unsubscribe as U
from sift.api import build_state, create_app
from sift.config import Settings


# --- parsing --------------------------------------------------------------------------------------------
GOOD_LU = "<https://news.shop-mail.com/u/abc?t=1>, <mailto:unsub@shop-mail.com?subject=unsubscribe>"


def test_one_click_requires_exact_post_value_and_https():
    o = U.classify(GOOD_LU, "List-Unsubscribe=One-Click")
    assert o.kind == "one_click" and o.url == "https://news.shop-mail.com/u/abc?t=1"
    assert o.host == "news.shop-mail.com" and o.mailto == "unsub@shop-mail.com"
    assert U.classify(GOOD_LU, "  List-Unsubscribe=One-Click \r\n").kind == "one_click"  # folding whitespace


@pytest.mark.parametrize("post", [None, "", "List-Unsubscribe=one-click", "list-unsubscribe=One-Click",
                                  "One-Click", "List-Unsubscribe=One-Click; foo=bar",
                                  "List-Unsubscribe=One-Click&x=1", "List-Unsubscribe = One-Click",
                                  "List-Unsubscribe=One-Click, List-Unsubscribe=One-Click"])
def test_anything_but_the_exact_pair_is_not_one_click(post):
    o = U.classify(GOOD_LU, post)
    assert o.kind == "manual" and o.url is None and o.mailto == "unsub@shop-mail.com"


@pytest.mark.parametrize("lu", [
    "<http://news.shop-mail.com/u/abc>",                                  # not https
    "<https://a.shop-mail.com/1>, <https://b.shop-mail.com/2>",          # RFC: exactly one https URI
    "<https://10.0.0.5/u>", "<https://[::1]/u>", "<https://localhost/u>", "<https://printer.local/u>",
    "<https://shop-mail.com:8443/u>", "<https://user:pw@shop-mail.com/u>", "<https://intranet/u>",
    "<https://svc.internal/u>", "<https://shop.example/u>",
])
def test_unacceptable_links_never_one_click(lu):
    o = U.classify(lu, "List-Unsubscribe=One-Click")
    assert o.kind != "one_click" and o.url is None


def test_mailto_only_is_manual_with_address_and_unbracketed_ignored():
    o = U.classify("<mailto:leave-123@lists.shop-mail.com>", "List-Unsubscribe=One-Click")
    assert o.kind == "manual" and o.mailto == "leave-123@lists.shop-mail.com" and o.url is None
    assert U.classify("https://shop-mail.com/u", "List-Unsubscribe=One-Click").kind == "none"
    assert U.classify(None, None).kind == "none"


def test_folded_header_parses():
    lu = "<https://news.shop-mail.com/u/abc>,\r\n <mailto:u@shop-mail.com>"
    assert U.parse_uris(lu) == ["https://news.shop-mail.com/u/abc", "mailto:u@shop-mail.com"]


# --- SSRF checks ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("url", ["http://shop-mail.com/u", "https://127.0.0.1/u", "https://[fe80::1]/u",
                                 "https://2130706433/u", "https://0x7f.1/u", "https://localhost./u",
                                 "https://a.localhost/u", "https://shop-mail.com:80/u", "https://x@shop-mail.com/u",
                                 "https://shop-mail.com\\@evil.com/u", "https://shop-mail.com/u\r\nX: y",
                                 "ftp://shop-mail.com/u", "https://" + "a" * 2100 + ".com/"])
def test_check_url_refuses(url):
    with pytest.raises(U.UnsubscribeError):
        U.check_url(url)


def test_check_url_accepts_public_name():
    assert U.check_url("https://Mail.Shop-Mail.com:443/u?x=1") == "mail.shop-mail.com"


@pytest.mark.parametrize("addrs", [["10.1.2.3"], ["127.0.0.1"], ["169.254.169.254"], ["192.168.1.1"], ["::1"],
                                   ["fc00::1"], ["::ffff:10.0.0.1"], ["93.184.216.34", "10.0.0.1"], [], ["0.0.0.0"],
                                   ["100.64.0.1"]])
def test_private_resolution_refused(addrs):
    with pytest.raises(U.UnsubscribeError):
        U.vetted_address("shop-mail.com", lambda h: addrs)


def test_public_resolution_accepted():
    assert U.vetted_address("shop-mail.com", lambda h: ["93.184.216.34", "2606:2800:220:1::1"]) == "93.184.216.34"


# --- the POST ---------------------------------------------------------------------------------------------
class FakeTransport:
    def __init__(self, result):
        self.result, self.calls = result, []

    def __call__(self, host, ip, target, timeout):
        self.calls.append((host, ip, target, timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


PUBLIC = staticmethod(lambda h: ["93.184.216.34"])


@pytest.mark.parametrize("code,outcome", [(200, "confirmed"), (202, "confirmed"), (204, "confirmed"),
                                          (301, "not_confirmed"), (302, "not_confirmed"), (307, "not_confirmed"),
                                          (400, "failed"), (404, "failed"), (500, "failed")])
def test_single_post_outcomes(code, outcome):
    t = FakeTransport(code)
    r = U.OneClickPoster(resolver=lambda h: ["93.184.216.34"], transport=t).post(
        "https://news.shop-mail.com/u/abc?t=1#frag")
    assert r.outcome == outcome and r.status_code == code
    assert t.calls == [("news.shop-mail.com", "93.184.216.34", "/u/abc?t=1", U.TIMEOUT_S)]  # exactly one


@pytest.mark.parametrize("exc,detail", [(socket.timeout(), "timed out"), (ssl.SSLError(), "TLS"),
                                        (ConnectionRefusedError(), "network error")])
def test_network_failures_reported_not_raised(exc, detail):
    t = FakeTransport(exc)
    r = U.OneClickPoster(resolver=lambda h: ["93.184.216.34"], transport=t).post("https://shop-mail.com/u")
    assert r.outcome == "failed" and detail in r.detail and len(t.calls) == 1


def test_refused_before_any_connection():
    t = FakeTransport(200)
    r = U.OneClickPoster(resolver=lambda h: ["10.0.0.7"], transport=t).post("https://shop-mail.com/u")
    assert r.outcome == "refused" and t.calls == []
    r = U.OneClickPoster(resolver=lambda h: ["93.184.216.34"], transport=t).post("http://shop-mail.com/u")
    assert r.outcome == "refused" and t.calls == []


def test_wire_request_is_rfc8058_and_pinned(monkeypatch):
    """The real transport, with the socket faked: POST, exact body and content type, no cookies or auth,
    connects to the vetted IP, TLS checks the original hostname."""
    seen = {}

    class Sock:
        def close(self):
            pass

    class Ctx:
        check_hostname = True
        verify_mode = ssl.CERT_REQUIRED

        def wrap_socket(self, sock, server_hostname=None):
            seen["sni"] = server_hostname
            return sock

    monkeypatch.setattr(U.socket, "create_connection", lambda addr, timeout=None: seen.update(addr=addr) or Sock())
    monkeypatch.setattr(U.ssl, "create_default_context", lambda: Ctx())

    class Resp:
        status = 302

    def fake_request(self, method, target, body=None, headers=None):
        self.connect()
        seen.update(method=method, target=target, body=body, headers=headers)

    monkeypatch.setattr(U._PinnedHTTPSConnection, "request", fake_request)
    monkeypatch.setattr(U._PinnedHTTPSConnection, "getresponse", lambda self: Resp())
    code = U._https_post("news.shop-mail.com", "93.184.216.34", "/u/abc?t=1", 5.0)
    assert code == 302
    assert seen["addr"] == ("93.184.216.34", 443) and seen["sni"] == "news.shop-mail.com"
    assert seen["method"] == "POST" and seen["target"] == "/u/abc?t=1"
    assert seen["body"] == b"List-Unsubscribe=One-Click"
    h = {k.lower(): v for k, v in seen["headers"].items()}
    assert h["content-type"] == "application/x-www-form-urlencoded" and h["host"] == "news.shop-mail.com"
    assert "cookie" not in h and "authorization" not in h


def test_default_context_verifies_certificates():
    ctx = ssl.create_default_context()
    assert ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED


def test_simulated_poster_never_connects(monkeypatch):
    monkeypatch.setattr(U.socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    r = U.SimulatedPoster().post("https://hirewave.com/u/1")
    assert r.outcome == "simulated" and "nothing was sent" in r.detail


# --- batches through the API (synthetic mailbox) ----------------------------------------------------------
class TrapPoster:
    """Fails the test if the synthetic mailbox ever reaches a real poster."""
    simulated = False

    def post(self, url):
        raise AssertionError("real poster used with synthetic data")


@pytest.fixture
def client(tmp_path, provider, retry):
    s = Settings()
    s.data_dir = tmp_path
    s.extra_allowed_hosts = ("testserver",)
    state = build_state(s, provider=provider, retry=retry, unsub_poster=TrapPoster())
    state.scanner.page_size, state.scanner.meta_batch = 200, 50
    c = TestClient(create_app(state), headers={"X-Sift": "1"})
    c.post("/api/scan")
    for _ in range(600):
        if c.get("/api/scan").json()["status"] != "running":
            break
        time.sleep(0.05)
    return c, state


def senders_by_kind(state):
    out = {"one_click": [], "manual": [], "none": []}
    with state.db.connect() as c:
        for r in c.execute("SELECT sender FROM sender_groups WHERE protected=0 ORDER BY total DESC"):
            m = c.execute("SELECT list_unsubscribe FROM messages WHERE from_addr=? ORDER BY internal_date DESC, "
                          "id DESC LIMIT 1", (r[0],)).fetchone()
            meta = state.provider.get_metadata([c.execute(
                "SELECT id FROM messages WHERE from_addr=? AND is_sent=0 ORDER BY internal_date DESC, id DESC LIMIT 1",
                (r[0],)).fetchone()[0]])[0]
            out[U.classify(meta.list_unsubscribe, meta.list_unsubscribe_post).kind].append(r[0])
    return out


def wait_batch(c, bid):
    for _ in range(300):
        d = c.get(f"/api/unsubscribe/batches/{bid}").json()
        if d["status"] not in ("running", "approved"):
            return d
        time.sleep(0.02)
    raise AssertionError("batch timeout")


def test_batch_lifecycle_synthetic(client):
    c, state = client
    kinds = senders_by_kind(state)
    assert kinds["one_click"] and kinds["manual"]
    chosen = kinds["one_click"][:3] + kinds["manual"][:1]
    r = c.post("/api/unsubscribe/batches", json={"senders": chosen})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["status"] == "draft" and b["counts"]["one_click"] == 3 and b["counts"]["manual"] == 1
    assert all("url" not in it for it in b["items"])            # tokens in URLs never reach the UI
    manual = next(it for it in b["items"] if it["method"] == "manual")
    assert manual["mailto"] or manual["host"]
    # Nothing has been sent, and nothing runs before approval.
    assert c.get("/api/unsubscribe/log").json() == []
    assert c.post(f"/api/unsubscribe/batches/{b['id']}/execute").status_code == 400
    assert c.post(f"/api/unsubscribe/batches/{b['id']}/approve", json={"confirm_hash": "nope"}).status_code == 400
    a = c.post(f"/api/unsubscribe/batches/{b['id']}/approve", json={"confirm_hash": b["items_hash"]}).json()
    assert a["status"] == "approved"
    # Approval is once only.
    assert c.post(f"/api/unsubscribe/batches/{b['id']}/approve", json={"confirm_hash": b["items_hash"]}).status_code == 400
    assert c.post(f"/api/unsubscribe/batches/{b['id']}/execute").status_code == 200
    d = wait_batch(c, b["id"])
    assert d["status"] == "completed"
    log = c.get("/api/unsubscribe/log").json()
    oc = [e for e in log if e["method"] == "one_click"]
    assert sorted(e["outcome"] for e in oc) == ["attempted"] * 3 + ["simulated"] * 3
    assert all(e["url_host"] and "/" not in e["url_host"] for e in oc)  # host only, never the URL
    assert any(e["method"] == "manual" and e["outcome"] == "manual" for e in log)
    # A batch runs once.
    assert c.post(f"/api/unsubscribe/batches/{b['id']}/execute").status_code == 400


def test_batch_rejects_unknown_and_protected_senders(client):
    c, state = client
    assert c.post("/api/unsubscribe/batches", json={"senders": ["nobody@nowhere.com"]}).status_code == 400
    assert c.post("/api/unsubscribe/batches", json={"senders": []}).status_code == 422
    kinds = senders_by_kind(state)
    target = kinds["one_click"][0]
    with state.db.connect() as conn:
        conn.execute("INSERT INTO protection_rules(kind,value,created_at) VALUES ('sender',?,'now')", (target,))
    b = c.post("/api/unsubscribe/batches", json={"senders": [target]}).json()
    assert b["items"][0]["method"] == "none" and "protected" in b["items"][0]["reason"]
    assert c.post(f"/api/unsubscribe/batches/{b['id']}/approve",
                  json={"confirm_hash": b["items_hash"]}).status_code == 400  # nothing to send


def test_batch_is_frozen_and_log_append_only(client):
    c, state = client
    kinds = senders_by_kind(state)
    b = c.post("/api/unsubscribe/batches", json={"senders": kinds["one_click"][:1]}).json()
    with state.db.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE unsubscribe_batches SET items='[]' WHERE id=?", (b["id"],))
        conn.execute("INSERT INTO unsubscribe_log(ts,sender,method,outcome) VALUES ('t','s','one_click','x')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE unsubscribe_log SET outcome='confirmed'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM unsubscribe_log")
    c.post(f"/api/unsubscribe/batches/{b['id']}/cancel")
    with state.db.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE unsubscribe_batches SET status='approved' WHERE id=?", (b["id"],))


def test_unsubscribe_requires_cleanup_enabled(client):
    c, state = client
    kinds = senders_by_kind(state)
    b = c.post("/api/unsubscribe/batches", json={"senders": kinds["one_click"][:1]}).json()
    state.detach_writer()
    assert c.post("/api/unsubscribe/batches", json={"senders": kinds["one_click"][:1]}).status_code == 409
    assert c.post(f"/api/unsubscribe/batches/{b['id']}/approve",
                  json={"confirm_hash": b["items_hash"]}).status_code == 409
    assert c.post(f"/api/unsubscribe/batches/{b['id']}/execute").status_code == 409


# --- Gmail mode: real poster path (faked), re-approval after re-enable -------------------------------------
from tests.test_gmail import (G, FakeKeyring, consent, creds_json, enable, gmail_app, msg,  # noqa: F401,E402
                              no_real_revoke, reader, wait_scan)


class RecordingPoster:
    simulated = False

    def __init__(self):
        self.urls = []

    def post(self, url):
        self.urls.append(url)
        return U.PostResult("confirmed", 200, "sender accepted the request")


@pytest.fixture
def gmail_unsub(gmail_app, consent):
    c, state, g, kr, _ = gmail_app
    for i in range(5):
        g.msgs[f"n{i}"] = msg(f"n{i}", frm=f"News <news{i}@letters-mail.com>", subject="Weekly digest",
                              labels=("INBOX", "CATEGORY_PROMOTIONS"), thread=f"tn{i}",
                              extra=[("List-Unsubscribe", f"<https://letters-mail.com/u/{i}>, <mailto:u@letters-mail.com>"),
                                     ("List-Unsubscribe-Post", "List-Unsubscribe=One-Click" if i < 3 else "x")])
    poster = RecordingPoster()
    state.unsub_poster = poster
    c.post("/api/scan")
    assert wait_scan(c)["status"] == "completed"
    return c, state, g, poster


def test_gmail_unsubscribe_needs_cleanup_and_fresh_approval(gmail_unsub):
    c, state, g, poster = gmail_unsub
    senders = [f"news{i}@letters-mail.com" for i in range(5)]
    assert c.post("/api/unsubscribe/batches", json={"senders": senders}).status_code == 409  # read-only
    enable(c)
    b = c.post("/api/unsubscribe/batches", json={"senders": senders}).json()
    assert b["counts"] == {"one_click": 3, "manual": 2, "none": 0}
    # Fresh headers were read (metadata only) for the batch.
    assert all(kw["format"] == "metadata" for k, kw in g.calls if k == "messages.get")
    c.post(f"/api/unsubscribe/batches/{b['id']}/approve", json={"confirm_hash": b["items_hash"]})
    # Approved in the same second cleanup was enabled: refused (must be strictly after).
    state.db.set_setting("cleanup.enabled_at", "2999-01-01T00:00:00+00:00")
    r = c.post(f"/api/unsubscribe/batches/{b['id']}/execute")
    assert r.status_code == 400 and "re-)enabled" in r.json()["detail"]
    state.db.set_setting("cleanup.enabled_at", "2000-01-01T00:00:00+00:00")
    assert c.post(f"/api/unsubscribe/batches/{b['id']}/execute").status_code == 200
    d = wait_batch(c, b["id"])
    assert d["status"] == "completed"
    assert poster.urls == [f"https://letters-mail.com/u/{i}" for i in range(3)]  # one POST each, manual never
    log = c.get("/api/unsubscribe/log").json()
    assert sum(1 for e in log if e["outcome"] == "confirmed" and e["status_code"] == 200) == 3
    assert not any("letters-mail.com/u" in json.dumps(e) for e in log)  # host only


def test_disconnect_discards_batches_keeps_log(gmail_unsub):
    c, state, g, poster = gmail_unsub
    enable(c)
    b = c.post("/api/unsubscribe/batches", json={"senders": ["news0@letters-mail.com"]}).json()
    with state.db.connect() as conn:
        conn.execute("INSERT INTO unsubscribe_log(ts,sender,method,outcome) VALUES ('t','s','one_click','x')")
    c.post("/api/gmail/disconnect")
    with state.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM unsubscribe_batches").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM unsubscribe_log").fetchone()[0] == 1
    assert c.get(f"/api/unsubscribe/batches/{b['id']}").status_code == 404
