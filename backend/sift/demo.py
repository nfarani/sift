"""Demo mode: mask personal data in every API response, for screen recordings.

Masking happens here, in the backend, so real addresses, names and subjects never reach the browser
while demo mode is on. Masked addresses are reversible on the way back in (request bodies only), so
excluding or protecting a sender still works during a recording. Nothing else changes: plans, the
executor and the audit log always work on real ids.

  - email addresses      n***a1b2@mail.example.com   (first letter, short hash, domain kept)
  - display names         Sender #4821                (stable per name)
  - subjects / snippets   hidden
  - your own name parts   masked wherever they appear (derived from your address)
  - digit runs of 6+      ####                        (order numbers, codes, phone numbers)
"""
from __future__ import annotations

import hashlib
import json
import re
import threading

EMAIL = re.compile(r"[A-Za-z0-9._%+\-=]+@([A-Za-z0-9\-]+\.)+[A-Za-z]{2,}")
DIGITS = re.compile(r"\d{6,}")  # order numbers, codes, phones; leaves dates and run labels readable
HIDDEN = "[hidden in demo mode]"
HASH_LEN = 8
MASKED = re.compile(r"^.?\*\*\*[0-9a-f]{4,%d}@\S+$" % HASH_LEN)  # also the older 4-char masks from open tabs
# Filter terms you typed yourself (Ask): shown and saved as typed.
USER_TERMS = {"sender_contains", "subject_contains_any", "subject_excludes_any", "text", "query"}


class UnknownMasked(ValueError):
    """A masked address came back that this backend didn't hand out (e.g. after a restart)."""
# JSON keys whose values are message content: replaced entirely.
CONTENT_KEYS = {"subject", "snippet", "subjects", "snippets", "sample_subjects", "subject_contains"}
# JSON keys whose values are display names.
NAME_KEYS = {"display_name", "from_name"}


def _passthrough(key: str | None) -> bool:
    """Identifiers, hashes, timestamps and statuses are never altered (the UI sends ids back)."""
    if not key:
        return False
    k = key.lower()
    if k == "list_id":  # List-Ids can carry addresses and account numbers: masked like free text
        return False
    return k in USER_TERMS or k == "id" or k.endswith(("_id", "_ids", "hash", "_at", "status", "state", "kind", "action"))


def _h(s: str, n: int = 4) -> str:
    return hashlib.sha256(s.lower().encode()).hexdigest()[:n]


class Masker:
    def __init__(self):
        self._lock = threading.Lock()
        self._reverse: dict[str, str] = {}      # masked address -> real address
        self._ambiguous: set[str] = set()       # masks shared by two real addresses: never unmasked
        self._names: dict[str, str] = {}        # real display name (lower) -> pseudonym
        self._own_tokens: set[str] = set()

    # -- vocabulary ---------------------------------------------------------
    def learn(self, own_email: str | None, display_names: list[str]) -> None:
        """Teach the masker the names to look for inside free text (Claude reasons, titles)."""
        with self._lock:
            if own_email:
                local = own_email.split("@", 1)[0]
                self._own_tokens = {t for t in re.split(r"[^A-Za-z]+", local) if len(t) >= 3}
            for n in display_names:
                n = (n or "").strip()
                if len(n) >= 3 and " " in n:  # multi-word names (people) inside free text; brands stay readable
                    self._names.setdefault(n.lower(), f"Sender #{int(_h(n, 6), 16) % 9000 + 1000}")

    def address(self, addr: str) -> str:
        local, _, domain = addr.partition("@")
        masked = f"{local[:1].lower()}***{_h(addr, HASH_LEN)}@{domain.lower()}"
        with self._lock:
            prev = self._reverse.get(masked)
            if prev is not None and prev.lower() != addr.lower():
                self._ambiguous.add(masked)
            self._reverse[masked] = addr
        return masked

    def name(self, n: str) -> str:
        n = (n or "").strip()
        if not n:
            return n
        return f"Sender #{int(_h(n, 6), 16) % 9000 + 1000}"

    def text(self, s: str) -> str:
        # Mask addresses first, and keep the digit rule away from them: a mask's hash can contain 6+ digits.
        parts, last = [], 0
        for m in EMAIL.finditer(s):
            parts.append(DIGITS.sub("####", s[last:m.start()]))
            parts.append(self.address(m.group(0)))
            last = m.end()
        parts.append(DIGITS.sub("####", s[last:]))
        s = "".join(parts)
        with self._lock:
            names = sorted(self._names.items(), key=lambda kv: -len(kv[0]))
            own = set(self._own_tokens)
        for real, pseudo in names:
            if real in s.lower():
                s = re.sub(re.escape(real), pseudo, s, flags=re.I)
        for t in own:
            s = re.sub(rf"\b{re.escape(t)}\b", "You", s, flags=re.I)
        return s

    # -- JSON walk ----------------------------------------------------------
    def mask(self, obj, key: str | None = None):
        if isinstance(obj, dict):
            return {k: self.mask(v, k) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.mask(v, key) for v in obj]
        if isinstance(obj, str):
            if _passthrough(key):
                return obj
            if key in CONTENT_KEYS:
                return HIDDEN if obj else obj
            if key in NAME_KEYS:
                return self.name(obj)
            return self.text(obj)
        return obj

    def unmask(self, obj):
        """Request bodies: turn masked addresses back into the real ones we handed out."""
        if isinstance(obj, dict):
            return {k: self.unmask(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.unmask(v) for v in obj]
        if isinstance(obj, str):
            with self._lock:
                real = None if obj in self._ambiguous else self._reverse.get(obj)
            if real is None and MASKED.match(obj):
                raise UnknownMasked(obj)  # never act on a masked address we can't map back
            return real or obj
        return obj

    def learn_addresses(self, addrs) -> None:
        """Masks are deterministic, so the reverse map can be rebuilt from known senders after a restart."""
        for a in addrs:
            if a:
                self.address(a)


def _has_masked(obj) -> bool:
    if isinstance(obj, dict):
        return any(_has_masked(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_masked(v) for v in obj)
    return isinstance(obj, str) and bool(MASKED.match(obj))


class DemoMiddleware:
    """Pure ASGI middleware for /api/. While `enabled()`: masks JSON responses and unmasks JSON request bodies.
    Always (demo on or off): refuses a request carrying a masked address it can't map back, so a stale demo-mode
    page can never save a masked string as a sender exclusion or rule."""

    def __init__(self, app, masker: Masker, enabled, refresh):
        self.app, self.masker, self.enabled, self.refresh = app, masker, enabled, refresh

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        on = self.enabled()
        if on:
            self.refresh()

        async def receive_unmasked():
            msg = await receive()
            if msg["type"] == "http.request":
                body = msg.get("body", b"")
                while msg.get("more_body"):
                    msg = await receive()
                    body += msg.get("body", b"")
                if body:
                    try:
                        data = json.loads(body)
                    except ValueError:
                        data = None
                    if data is not None:
                        try:
                            if on:
                                body = json.dumps(self.masker.unmask(data)).encode()
                            elif _has_masked(data):
                                raise UnknownMasked("demo mode is off")
                        except UnknownMasked:
                            body = b'{"__sift_unknown_masked__": true}'  # rejected below; nothing is acted on
                return {"type": "http.request", "body": body, "more_body": False}
            return msg

        start: dict = {}
        chunks: list[bytes] = []

        async def send_masked(msg):
            if msg["type"] == "http.response.start":
                start.update(msg)
                return
            if msg["type"] == "http.response.body":
                chunks.append(msg.get("body", b""))
                if msg.get("more_body"):
                    return
                body = b"".join(chunks)
                headers = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
                ctype = dict(start.get("headers", [])).get(b"content-type", b"")
                if on and body and ctype.startswith(b"application/json"):
                    try:
                        body = json.dumps(self.masker.mask(json.loads(body))).encode()
                    except ValueError:
                        body = json.dumps({"detail": HIDDEN}).encode()  # never pass unmasked text through
                headers.append((b"content-length", str(len(body)).encode()))
                await send({**start, "headers": headers})
                await send({"type": "http.response.body", "body": body})
                return
            await send(msg)

        # Peek once so an unmappable masked address is refused before the route runs.
        first = await receive_unmasked()
        if first.get("body") == b'{"__sift_unknown_masked__": true}':
            body = json.dumps({"detail": "this request contains a masked demo-mode address that can't be mapped back "
                                         "(demo mode was switched or the backend restarted); reload the page and "
                                         "try again"}).encode()
            await send({"type": "http.response.start", "status": 409,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        sent = {"done": False}

        async def replay():
            if not sent["done"]:
                sent["done"] = True
                return first
            return await receive()

        if first.get("type") == "http.request":  # the body may have changed length
            n = str(len(first.get("body", b""))).encode()
            scope = {**scope, "headers": [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"content-length"]
                     + [(b"content-length", n)]}
        await self.app(scope, replay, send_masked if on else send)
