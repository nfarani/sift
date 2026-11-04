"""Deterministic filter specs: what a natural-language request becomes, and what a custom
recommendation is built from. Plain SQL over the local scan; no AI here.

Protected messages and your own sent mail are always excluded from matches. That is not a spec field,
so nothing (including a model-written spec) can turn it off.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .classifier.base import CATEGORIES

MODES = ("messages", "top_senders")
ACTIONS = ("archive", "trash", "review")


class SpecError(ValueError):
    pass


@dataclass
class Spec:
    mode: str = "messages"
    title: str = ""
    categories: list[str] = field(default_factory=list)
    sender_contains: list[str] = field(default_factory=list)
    subject_contains_any: list[str] = field(default_factory=list)
    subject_excludes_any: list[str] = field(default_factory=list)
    older_than_days: int | None = None
    newer_than_days: int | None = None
    unread_only: bool = False
    suggested_action: str = "review"
    explanation: str = ""
    not_supported: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _strs(v, name, lo=2, hi=60, n=10) -> list[str]:
    if not isinstance(v, list) or len(v) > n:
        raise SpecError(f"{name} must be a list of at most {n} items")
    out = []
    for x in v:
        if not isinstance(x, str) or not lo <= len(x.strip()) <= hi:
            raise SpecError(f"{name} items must be {lo}-{hi} characters")
        out.append(x.strip())
    return out


def _days(v, name):
    if v is None:
        return None
    if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 36500:
        raise SpecError(f"{name} must be a whole number of days (0-36500) or null")
    return v


def parse_spec(d: dict) -> Spec:
    """Strict validation of a spec from the UI or from the planner model. Unknown keys are rejected."""
    if not isinstance(d, dict):
        raise SpecError("spec must be an object")
    allowed = set(Spec().__dict__)
    extra = set(d) - allowed
    if extra:
        raise SpecError(f"unknown fields: {sorted(extra)}")
    s = Spec()
    s.mode = d.get("mode", "messages")
    if s.mode not in MODES:
        raise SpecError("mode must be messages or top_senders")
    s.title = str(d.get("title", ""))[:120]
    s.categories = _strs(d.get("categories", []), "categories", 3, 20, 15)
    if any(c not in CATEGORIES for c in s.categories):
        raise SpecError("unknown category")
    s.sender_contains = [x.lower() for x in _strs(d.get("sender_contains", []), "sender_contains")]
    s.subject_contains_any = _strs(d.get("subject_contains_any", []), "subject_contains_any")
    s.subject_excludes_any = _strs(d.get("subject_excludes_any", []), "subject_excludes_any")
    s.older_than_days = _days(d.get("older_than_days"), "older_than_days")
    s.newer_than_days = _days(d.get("newer_than_days"), "newer_than_days")
    if not isinstance(d.get("unread_only", False), bool):
        raise SpecError("unread_only must be true or false")
    s.unread_only = d.get("unread_only", False)
    s.suggested_action = d.get("suggested_action", "review")
    if s.suggested_action not in ACTIONS:
        raise SpecError("suggested_action must be archive, trash or review")
    s.explanation = str(d.get("explanation", ""))[:500]
    s.not_supported = [str(x)[:200] for x in (d.get("not_supported") or [])][:10]
    if not (s.categories or s.sender_contains or s.subject_contains_any or s.older_than_days is not None
            or s.newer_than_days is not None or s.unread_only or s.mode == "top_senders"):
        raise SpecError("the filter is empty; it would match your whole inbox")
    return s


def _like(text: str) -> str:
    t = text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{t}%"


def where(spec: Spec, now_ms: int | None = None, protected: int | None = 0,
          excluded_senders: tuple[str, ...] = (), extra_older_than_days: int = 0) -> tuple[str, list]:
    """SQL WHERE over `messages` (alias-free). protected=0 for actionable matches, 1 to count exclusions."""
    now_ms = now_ms or int(time.time() * 1000)
    clauses = ["is_sent=0", "in_inbox=1"]
    params: list = []
    if protected is not None:
        clauses.append("protected=?")
        params.append(protected)
    if spec.categories:
        clauses.append(f"category IN ({','.join('?' * len(spec.categories))})")
        params += spec.categories
    if spec.sender_contains:
        clauses.append("(" + " OR ".join(["lower(from_addr) LIKE ? ESCAPE '\\' OR lower(COALESCE(from_name,'')) "
                                          "LIKE ? ESCAPE '\\'"] * len(spec.sender_contains)) + ")")
        for x in spec.sender_contains:
            params += [_like(x), _like(x)]
    if spec.subject_contains_any:
        clauses.append("(" + " OR ".join(["lower(subject) LIKE ? ESCAPE '\\'"] * len(spec.subject_contains_any)) + ")")
        params += [_like(x) for x in spec.subject_contains_any]
    for x in spec.subject_excludes_any:
        clauses.append("lower(subject) NOT LIKE ? ESCAPE '\\'")
        params.append(_like(x))
    older = max(spec.older_than_days or 0, extra_older_than_days or 0)
    if older:
        clauses.append("internal_date < ?")
        params.append(now_ms - older * 86_400_000)
    if spec.newer_than_days is not None:
        clauses.append("internal_date >= ?")
        params.append(now_ms - spec.newer_than_days * 86_400_000)
    if spec.unread_only:
        clauses.append("is_unread=1")
    if excluded_senders:
        clauses.append(f"from_addr NOT IN ({','.join('?' * len(excluded_senders))})")
        params += [s.lower() for s in excluded_senders]
    return " AND ".join(clauses), params


def preview(db, spec: Spec, excluded_senders: tuple[str, ...] = (), extra_older_than_days: int = 0) -> dict:
    w, p = where(spec, excluded_senders=excluded_senders, extra_older_than_days=extra_older_than_days)
    wp, pp = where(spec, protected=1, excluded_senders=excluded_senders, extra_older_than_days=extra_older_than_days)
    with db.connect() as c:
        count = c.execute(f"SELECT COUNT(*) FROM messages WHERE {w}", p).fetchone()[0]
        prot = c.execute(f"SELECT COUNT(*) FROM messages WHERE {wp}", pp).fetchone()[0]
        samples = [dict(r) for r in c.execute(
            f"SELECT id, from_addr, from_name, subject, internal_date, category FROM messages WHERE {w} "
            "ORDER BY internal_date DESC LIMIT 8", p)]
        senders = [dict(r) for r in c.execute(
            f"SELECT from_addr sender, MAX(from_name) display_name, COUNT(*) n, SUM(is_unread) unread, "
            f"MIN(internal_date) first, MAX(internal_date) last FROM messages WHERE {w} "
            "GROUP BY from_addr ORDER BY n DESC LIMIT 25", p)]
        dr = c.execute(f"SELECT MIN(internal_date), MAX(internal_date) FROM messages WHERE {w}", p).fetchone()
    return {"count": count, "protected_excluded": prot, "samples": samples, "senders": senders,
            "date_range": [dr[0], dr[1]]}


def matching_ids(c, spec: Spec, excluded_senders: tuple[str, ...], extra_older_than_days: int,
                 now_ms: int) -> list[str]:
    w, p = where(spec, now_ms=now_ms, excluded_senders=excluded_senders, extra_older_than_days=extra_older_than_days)
    return [r[0] for r in c.execute(f"SELECT id FROM messages WHERE {w} ORDER BY internal_date", p)]
