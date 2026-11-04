"""RFC 8058 one-click unsubscribe: exact parsing, frozen batches with their own approval, SSRF-safe POST.

RFC 8058 (https://www.rfc-editor.org/rfc/rfc8058.html, checked 2026-11-04):
- 3.1: "The List-Unsubscribe header field MUST contain one HTTPS URI." ... "The List-Unsubscribe-Post header
  MUST contain the single key/value pair 'List-Unsubscribe=One-Click'."
- 3.2: the receiver performs "an HTTPS POST to the HTTPS URI in the List-Unsubscribe header", sending that
  key/value pair as the body, as multipart/form-data or application/x-www-form-urlencoded.
  "The POST request MUST NOT include cookies, HTTP authorization, or any other context information."
  "The mail sender MUST NOT return an HTTPS redirect" -> a 3xx is reported as not confirmed, never followed.
  Receivers "MUST NOT perform a POST on the HTTPS URI without user consent" -> separate approval per batch.
- 3.1/4 also require a DKIM signature covering both headers. Sift only reads metadata and cannot verify
  that, so one-click is offered to the user, who approves each batch.

List-Unsubscribe syntax (RFC 2369): a comma-separated list of URIs, each in angle brackets.

Safety:
- Nothing here runs without an approved batch whose hash the user echoed back. Batches are frozen: the
  exact URLs are stored at creation; any change means a new batch and a new approval. A batch runs once.
- mailto: unsubscribes are never sent (sending mail needs a scope Sift never requests). They are shown as
  "manual" with the address.
- POST only to https on port 443, to a DNS name (no IP literals, no localhost/.local/.internal/reserved
  names, no single-label hosts) whose every resolved address is public. The connection is pinned to the
  vetted address (TLS still verifies the certificate for the hostname), so DNS rebinding can't redirect it.
- One request per URL: no retries, no redirects, no cookies, short timeouts, response body not read.
- Every attempt is logged (append-only `unsubscribe_log`): sender, URL host only, status code, timestamp.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import unquote, urlsplit

from .db import Database, now_iso

ONE_CLICK_VALUE = "List-Unsubscribe=One-Click"
POST_BODY = ONE_CLICK_VALUE.encode("ascii")
CONTENT_TYPE = "application/x-www-form-urlencoded"
USER_AGENT = "Sift/0.1 (RFC 8058 one-click unsubscribe)"
TIMEOUT_S = 10.0
MAX_BATCH_SENDERS = 200
MAX_URL_LEN = 2048

_URI_RE = re.compile(r"<\s*([^<>]*?)\s*>")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST_RE = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})+$")
# Names that never belong to a public sender (RFC 6761/2606 reserved, mDNS, common private pseudo-TLDs).
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".intranet", ".lan", ".home", ".corp", ".home.arpa",
                     ".test", ".invalid", ".example", ".onion", ".arpa")
_BLOCKED_HOSTS = {"localhost", "localhost.localdomain"}


class UnsubscribeError(Exception):
    """Fixed, UI-safe messages only."""


# --- parsing -------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class UnsubOption:
    kind: str                     # one_click | manual | none
    url: str | None = None        # the https URL to POST to (one_click only)
    host: str | None = None       # host of the single acceptable https link, if any
    mailto: str | None = None     # address for a manual unsubscribe
    reason: str = ""


def parse_uris(list_unsubscribe: str | None) -> list[str]:
    """URIs in a List-Unsubscribe header, in order. Only bracketed URIs count (RFC 2369)."""
    if not list_unsubscribe:
        return []
    unfolded = re.sub(r"\r?\n[ \t]+", " ", list_unsubscribe)
    return [u.strip() for u in _URI_RE.findall(unfolded) if u.strip()]


def _mailto_address(uri: str) -> str | None:
    if not uri.lower().startswith("mailto:"):
        return None
    addr = unquote(uri[7:].split("?", 1)[0]).strip()
    return addr if re.fullmatch(r"[^@\s<>,;]+@[^@\s<>,;]+", addr) else None


def is_one_click_post(value: str | None) -> bool:
    """Exactly the single key/value pair RFC 8058 requires (surrounding whitespace from header folding
    tolerated). Anything else, including extra pairs or different case, is not one-click."""
    return value is not None and value.strip() == ONE_CLICK_VALUE


def classify(list_unsubscribe: str | None, list_unsubscribe_post: str | None) -> UnsubOption:
    uris = parse_uris(list_unsubscribe)
    https = [u for u in uris if u.lower().startswith("https://")]
    mailto = next((a for a in map(_mailto_address, uris) if a), None)
    host = None
    if len(https) == 1:
        try:
            host = check_url(https[0])
        except UnsubscribeError:
            host = None
    post_ok = is_one_click_post(list_unsubscribe_post)
    if post_ok and host:
        return UnsubOption("one_click", url=https[0], host=host, mailto=mailto, reason="RFC 8058 one-click")
    if mailto or https:
        why = ("no 'List-Unsubscribe-Post: List-Unsubscribe=One-Click' header" if not post_ok else
               "one-click needs exactly one acceptable https link")
        return UnsubOption("manual", host=host, mailto=mailto, reason=why)
    return UnsubOption("none", reason="no usable List-Unsubscribe link")


# --- SSRF checks ---------------------------------------------------------------------------------------
def check_url(url: str) -> str:
    """Validate a one-click URL without touching the network. Returns the lower-cased host."""
    if len(url) > MAX_URL_LEN or any(ch in url for ch in "\r\n\t \x00\\"):
        raise UnsubscribeError("link is malformed")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UnsubscribeError("link is malformed") from None
    if parts.scheme.lower() != "https":
        raise UnsubscribeError("only https links are used")
    if "@" in parts.netloc:
        raise UnsubscribeError("links with credentials are refused")
    if port not in (None, 443):
        raise UnsubscribeError("only the standard https port is allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsubscribeError("link has no host")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise UnsubscribeError("links to IP addresses are refused")
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES) or "." not in host:
        raise UnsubscribeError("links to local or private hosts are refused")
    if not _HOST_RE.match(host) or host.rsplit(".", 1)[-1].isdigit():
        raise UnsubscribeError("link host is not a valid public domain name")
    return host


def _public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or
                                a.is_reserved or a.is_unspecified)


Resolver = Callable[[str], list[str]]


def system_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    return list(dict.fromkeys(i[4][0] for i in infos))


def vetted_address(host: str, resolver: Resolver) -> str:
    """Resolve and require every address to be public (one private answer refuses the host)."""
    try:
        addrs = resolver(host)
    except Exception:
        raise UnsubscribeError("could not resolve the link's host") from None
    if not addrs:
        raise UnsubscribeError("could not resolve the link's host")
    if not all(_public(a) for a in addrs):
        raise UnsubscribeError("link resolves to a private or local address; refused")
    return addrs[0]


# --- the POST ------------------------------------------------------------------------------------------
@dataclass
class PostResult:
    outcome: str                  # confirmed | not_confirmed | failed | refused | simulated
    status_code: int | None
    detail: str


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a pre-vetted IP, with SNI and certificate checks for the original hostname."""

    def __init__(self, host: str, ip: str, timeout: float, context: ssl.SSLContext):
        super().__init__(host, 443, timeout=timeout, context=context)
        self._ip = ip
        self._ctx = context

    def connect(self):
        sock = socket.create_connection((self._ip, 443), timeout=self.timeout)
        self.sock = self._ctx.wrap_socket(sock, server_hostname=self.host)


def _https_post(host: str, ip: str, target: str, timeout: float) -> int:
    """One POST. http.client never follows redirects and keeps no cookies."""
    ctx = ssl.create_default_context()  # verifies certificate chain and hostname
    conn = _PinnedHTTPSConnection(host, ip, timeout, ctx)
    try:
        conn.request("POST", target, body=POST_BODY, headers={
            "Host": host, "Content-Type": CONTENT_TYPE, "Content-Length": str(len(POST_BODY)),
            "User-Agent": USER_AGENT, "Accept": "*/*", "Connection": "close"})
        return conn.getresponse().status  # status line + headers only; the body is never read
    finally:
        conn.close()


Transport = Callable[[str, str, str, float], int]


class OneClickPoster:
    """Performs exactly one RFC 8058 POST. `resolver` and `transport` are injectable for tests."""
    simulated = False

    def __init__(self, resolver: Resolver = system_resolver, transport: Transport = _https_post,
                 timeout: float = TIMEOUT_S):
        self.resolver, self.transport, self.timeout = resolver, transport, timeout

    def post(self, url: str) -> PostResult:
        try:
            host = check_url(url)
            ip = vetted_address(host, self.resolver)
        except UnsubscribeError as e:
            return PostResult("refused", None, str(e))
        parts = urlsplit(url)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        try:
            code = self.transport(host, ip, target, self.timeout)
        except ssl.SSLError:
            return PostResult("failed", None, "TLS certificate or handshake failed")
        except (socket.timeout, TimeoutError):
            return PostResult("failed", None, "timed out")
        except Exception as e:
            return PostResult("failed", None, f"network error ({type(e).__name__})")
        if 200 <= code < 300:
            return PostResult("confirmed", code, "sender accepted the request")
        if 300 <= code < 400:
            return PostResult("not_confirmed", code, "sender answered with a redirect; not followed (RFC 8058)")
        return PostResult("failed", code, f"sender answered HTTP {code}")


class SimulatedPoster:
    """For the synthetic mailbox: never touches the network, and says so."""
    simulated = True

    def post(self, url: str) -> PostResult:
        try:
            check_url(url)
        except UnsubscribeError as e:
            return PostResult("refused", None, str(e))
        return PostResult("simulated", None, "synthetic mailbox: nothing was sent")


# --- batches -------------------------------------------------------------------------------------------
def _hash(items: list[dict]) -> str:
    return hashlib.sha256(json.dumps(items, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _batch_row(c, batch_id: int) -> dict:
    r = c.execute("SELECT * FROM unsubscribe_batches WHERE id=?", (batch_id,)).fetchone()
    if r is None:
        raise UnsubscribeError("unknown unsubscribe batch")
    d = dict(r)
    d["items"] = json.loads(d["items"])
    return d


def public_batch(d: dict) -> dict:
    """What the UI sees: hosts and addresses, never the full one-click URL (it carries per-recipient tokens)."""
    items = [{k: v for k, v in it.items() if k != "url"} for it in d["items"]]
    return {**d, "items": items,
            "counts": {k: sum(1 for it in d["items"] if it["method"] == k) for k in ("one_click", "manual", "none")}}


def get_batch(db: Database, batch_id: int) -> dict:
    with db.connect() as c:
        d = _batch_row(c, batch_id)
        d["log"] = [dict(r) for r in c.execute(
            "SELECT ts, sender, method, url_host, status_code, outcome, detail FROM unsubscribe_log "
            "WHERE batch_id=? ORDER BY id", (batch_id,))]
    return public_batch(d)


def create_batch(db: Database, reader, senders: Iterable[str], user_email: str) -> dict:
    """Freeze a list of sender groups into an unsubscribe batch (status draft).
    Headers come from a fresh metadata read (read-only) of each sender's newest scanned message, parsed
    exactly; the stored scan flag is not trusted for this."""
    from .protection import load_context
    wanted = list(dict.fromkeys(s.strip().lower() for s in senders if s and s.strip()))
    if not wanted:
        raise UnsubscribeError("choose at least one sender")
    if len(wanted) > MAX_BATCH_SENDERS:
        raise UnsubscribeError(f"at most {MAX_BATCH_SENDERS} senders per batch")
    ctx = load_context(db, user_email)
    newest: dict[str, str] = {}
    has_protected: set[str] = set()
    with db.connect() as c:
        for s in wanted:
            g = c.execute("SELECT protected FROM sender_groups WHERE sender=?", (s,)).fetchone()
            if not g:
                raise UnsubscribeError(f"unknown sender group: {s[:120]}")
            if g[0]:
                has_protected.add(s)
            r = c.execute("SELECT id FROM messages WHERE from_addr=? AND is_sent=0 "
                          "ORDER BY internal_date DESC, id DESC LIMIT 1", (s,)).fetchone()
            if r:
                newest[s] = r[0]
    metas = {m.id: m for m in (reader.get_metadata(list(newest.values())) if newest else [])}
    items = []
    for s in wanted:
        domain = s.rsplit("@", 1)[-1]
        base = {"sender": s, "message_id": newest.get(s), "url": None, "host": None, "mailto": None}
        if s in ctx.sent_recipients or s == user_email.lower():
            items.append({**base, "method": "none", "reason": "sender you have emailed: protected"})
            continue
        if s in ctx.protected_senders or domain in ctx.protected_domains:
            items.append({**base, "method": "none", "reason": "protected by your rule"})
            continue
        if s in has_protected:  # starred, a thread with your reply, receipts/security/finance mail
            items.append({**base, "method": "none", "reason": "has protected mail: unsubscribe manually if you mean it"})
            continue
        m = metas.get(newest.get(s))
        if m is None or m.from_addr.lower() != s:
            items.append({**base, "method": "none", "reason": "no current message from this sender"})
            continue
        opt = classify(m.list_unsubscribe, m.list_unsubscribe_post)
        items.append({**base, "method": opt.kind, "url": opt.url, "host": opt.host, "mailto": opt.mailto,
                      "reason": opt.reason})
    h = _hash(items)
    with db.connect() as c:
        bid = c.execute("INSERT INTO unsubscribe_batches(status,created_at,items,items_hash) VALUES "
                        "('draft',?,?,?)", (now_iso(), json.dumps(items, sort_keys=True), h)).lastrowid
    return get_batch(db, bid)


def approve_batch(db: Database, batch_id: int, confirm_hash: str) -> dict:
    with db.tx() as c:
        d = _batch_row(c, batch_id)
        if d["status"] != "draft":
            raise UnsubscribeError(f"batch is {d['status']}; only a new batch can be approved")
        if _hash(d["items"]) != d["items_hash"]:
            raise UnsubscribeError("batch contents changed after creation; create a new batch")
        if confirm_hash != d["items_hash"]:
            raise UnsubscribeError("confirmation does not match this batch; review it again")
        if not any(it["method"] == "one_click" for it in d["items"]):
            raise UnsubscribeError("nothing to send: no sender in this batch supports one-click")
        c.execute("UPDATE unsubscribe_batches SET status='approved', approved_at=? WHERE id=?",
                  (now_iso(), batch_id))
    return get_batch(db, batch_id)


def cancel_batch(db: Database, batch_id: int) -> dict:
    with db.tx() as c:
        d = _batch_row(c, batch_id)
        if d["status"] not in ("draft", "approved"):
            raise UnsubscribeError(f"batch is {d['status']}")
        c.execute("UPDATE unsubscribe_batches SET status='cancelled', finished_at=? WHERE id=?", (now_iso(), batch_id))
    return get_batch(db, batch_id)


def start_batch(db: Database, batch_id: int, cleanup_enabled_at: str | None) -> None:
    """Move approved -> running, atomically. A batch runs at most once.
    `cleanup_enabled_at`: when cleanup was last enabled (None = never: refuse; "" = no grant applies, i.e.
    the synthetic mailbox). A batch approved before that time is refused."""
    with db.tx() as c:
        d = _batch_row(c, batch_id)
        if d["status"] != "approved":
            raise UnsubscribeError(f"batch is {d['status']}; it must be approved (once) before it runs")
        if _hash(d["items"]) != d["items_hash"]:
            raise UnsubscribeError("batch contents changed after approval; refusing to run")
        # Same rule as the executor: approved strictly after cleanup was (re-)enabled.
        if cleanup_enabled_at is None or (cleanup_enabled_at and (d["approved_at"] or "") <= cleanup_enabled_at):
            raise UnsubscribeError("this batch was approved before cleanup was (re-)enabled; create a new batch")
        if c.execute("SELECT 1 FROM unsubscribe_batches WHERE status='running'").fetchone():
            raise UnsubscribeError("another unsubscribe batch is running")
        c.execute("UPDATE unsubscribe_batches SET status='running', started_at=? WHERE id=?", (now_iso(), batch_id))


def _log(db: Database, batch_id: int, sender: str, method: str, host: str | None, code: int | None,
         outcome: str, detail: str) -> None:
    with db.connect() as c:
        c.execute("INSERT INTO unsubscribe_log(ts,batch_id,sender,method,url_host,status_code,outcome,detail) "
                  "VALUES (?,?,?,?,?,?,?,?)", (now_iso(), batch_id, sender, method, host, code, outcome, detail[:200]))


def run_batch(db: Database, batch_id: int, poster) -> str:
    """Execute a batch already moved to running by start_batch. One POST per one-click sender, logged
    write-ahead ('attempted') and with its result. Manual items are logged as shown, never sent."""
    with db.connect() as c:
        d = _batch_row(c, batch_id)
    if d["status"] != "running":
        raise UnsubscribeError("batch is not running")
    status = "completed"
    try:
        for it in d["items"]:
            if it["method"] == "manual":
                via = f" via {it['mailto']}" if it.get("mailto") else ""
                _log(db, batch_id, it["sender"], "manual", it.get("host"), None, "manual",
                     f"not sent: unsubscribe yourself{via}")
                continue
            if it["method"] != "one_click" or not it.get("url"):
                continue
            _log(db, batch_id, it["sender"], "one_click", it.get("host"), None, "attempted", "single POST")
            r = poster.post(it["url"])
            _log(db, batch_id, it["sender"], "one_click", it.get("host"), r.status_code, r.outcome, r.detail)
    except Exception as e:
        status = "failed"
        with db.connect() as c:
            c.execute("UPDATE unsubscribe_batches SET error=? WHERE id=?", (f"stopped ({type(e).__name__})", batch_id))
    with db.connect() as c:
        c.execute("UPDATE unsubscribe_batches SET status=?, finished_at=? WHERE id=?", (status, now_iso(), batch_id))
    return status


def discard_batches(db: Database) -> None:
    """Disconnect: batches hold one-click URLs (per-recipient tokens), so they're deleted unless one is
    running. The append-only log (hosts only) is kept."""
    with db.connect() as c:
        c.execute("DELETE FROM unsubscribe_batches WHERE status != 'running'")
