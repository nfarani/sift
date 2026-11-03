"""Deterministic protection rules. They run before any classifier and cannot be overridden by it.

A protected message is never placed in a plan, and the executor re-checks these rules
on fresh metadata immediately before modifying anything.

Deliberately NOT used as decisive signals: Gmail's IMPORTANT label (noisy) and the
presence of a List-Unsubscribe header (transactional senders use it too).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("receipt", re.compile(r"\b(receipts?|invoices?|order\s+(confirmation|#|no\.?|number)|your\s+order|has\s+shipped|"
                           r"payment\s+receipt|refund|purchase\s+confirmation)\b", re.I)),
    ("travel", re.compile(r"\b(booking|reservation|itinerary|boarding\s+pass|e-?ticket|check-?in|flight\s+confirmation|"
                          r"confirmation\s+(number|#))\b", re.I)),
    ("security", re.compile(r"\b(password|verification\s+code|security\s+(alert|code)|2fa|two-factor|one-time\s+(code|passcode)|"
                            r"new\s+sign-?in|login\s+attempt|account\s+recovery)\b", re.I)),
    ("finance", re.compile(r"\b(bank|statement|payment\s+received|direct\s+debit|payslip|tax|your\s+bill|"
                           r"account\s+ending|transaction)\b", re.I)),
]
_SENDER_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("security", re.compile(r"^(security|account-?security|verify|2fa)@", re.I)),
    ("finance", re.compile(r"^(statements?|billing|invoices?|payments?)@", re.I)),
]


@dataclass
class ProtectionContext:
    user_email: str
    sent_threads: set[str] = field(default_factory=set)
    sent_recipients: set[str] = field(default_factory=set)
    protected_senders: set[str] = field(default_factory=set)
    protected_domains: set[str] = field(default_factory=set)
    protected_labels: set[str] = field(default_factory=set)


def protection_reason(*, labels: list[str], thread_id: str, from_addr: str, subject: str,
                      ctx: ProtectionContext) -> str | None:
    """Return why a message is protected, or None. Order matters only for the reason shown."""
    addr = from_addr.lower()
    domain = addr.rsplit("@", 1)[-1]
    if "STARRED" in labels:
        return "starred"
    if thread_id in ctx.sent_threads:
        return "thread contains your reply"
    if addr == ctx.user_email.lower():
        return "sent by you"
    if addr in ctx.sent_recipients:
        return "sender you have emailed"
    if addr in ctx.protected_senders:
        return "protected sender (your rule)"
    if domain in ctx.protected_domains:
        return "protected domain (your rule)"
    hit = next((l for l in labels if l in ctx.protected_labels), None)
    if hit:
        return f"protected label {hit} (your rule)"
    for name, pat in _SENDER_PATTERNS:
        if pat.search(addr):
            return f"{name} sender"
    for name, pat in _PATTERNS:
        if pat.search(subject or ""):
            return f"{name} pattern in subject"
    return None


def load_context(db, user_email: str) -> ProtectionContext:
    """Build the context from scanned SENT mail and the user's rules."""
    ctx = ProtectionContext(user_email=user_email)
    with db.connect() as c:
        for r in c.execute("SELECT thread_id, to_addrs FROM messages WHERE is_sent=1"):
            ctx.sent_threads.add(r["thread_id"])
            for a in (r["to_addrs"] or "").split(","):
                if a.strip():
                    ctx.sent_recipients.add(a.strip().lower())
        for r in c.execute("SELECT kind, value FROM protection_rules"):
            {"sender": ctx.protected_senders, "domain": ctx.protected_domains,
             "label": ctx.protected_labels}[r["kind"]].add(r["value"].lower() if r["kind"] != "label" else r["value"])
    return ctx
