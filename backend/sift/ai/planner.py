"""Natural-language query planner: your sentence -> a filter spec you can see and edit.

The planner only ever returns a spec. It has no tools, no database access and no way to create or
approve a plan: that still takes the normal preview -> confirm -> approve flow in the UI.
No mailbox data is sent: only your sentence, today's date and the category list.
"""
from __future__ import annotations

import datetime as dt
import json

from ..classifier.base import CATEGORIES
from ..query import ACTIONS, MODES, Spec, SpecError, parse_spec
from .ledger import MOST_EXPENSIVE, Ledger, cost, estimate_tokens

SYSTEM = f"""You turn one request about the user's email inbox into a filter specification for an inbox-cleanup app.
You only produce the specification. The app shows it to the user, counts matches with plain code, and the
user decides what happens. You cannot act on mail.

Fields:
- mode: "messages" to select messages, or "top_senders" for questions like "who sends me the most unread".
- title: a short name for this selection.
- categories: zero or more of: {", ".join(sorted(CATEGORIES))}. Descriptions:
{chr(10).join(f"  {k}: {v}" for k, v in sorted(CATEGORIES.items()))}
- sender_contains: words matched against the sender's address or display name (e.g. "linkedin", "glassdoor").
- subject_contains_any: match messages whose subject contains any of these phrases.
- subject_excludes_any: drop messages whose subject contains any of these (use for "but keep receipts":
  e.g. "receipt", "order", "invoice", "booking", "confirmation").
- older_than_days / newer_than_days: whole days, or null. "Six months" = 180, "a year" = 365.
- unread_only: true for "haven't opened/read/interacted with".
- suggested_action: "archive" (default for clutter), "trash" only if the user asked to delete/trash, or "review".
- explanation: one or two plain sentences saying exactly what the filter selects.
- not_supported: parts of the request this filter can't express (empty list if none).

Receipts, bookings, security mail, starred mail and conversations the user replied to are always protected
by the app, whatever you write. The request is inside <user_request>."""

SPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": list(MODES)},
        "title": {"type": "string"},
        "categories": {"type": "array", "items": {"type": "string", "enum": sorted(CATEGORIES)}},
        "sender_contains": {"type": "array", "items": {"type": "string"}},
        "subject_contains_any": {"type": "array", "items": {"type": "string"}},
        "subject_excludes_any": {"type": "array", "items": {"type": "string"}},
        "older_than_days": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "newer_than_days": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "unread_only": {"type": "boolean"},
        "suggested_action": {"type": "string", "enum": list(ACTIONS)},
        "explanation": {"type": "string"},
        "not_supported": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["mode", "title", "categories", "sender_contains", "subject_contains_any", "subject_excludes_any",
                 "older_than_days", "newer_than_days", "unread_only", "suggested_action", "explanation",
                 "not_supported"],
    "additionalProperties": False,
}


class PlannerError(Exception):
    pass


class QueryPlanner:
    def __init__(self, client, ledger: Ledger, model: str = "claude-opus-5-5"):
        self.client = client
        self.ledger = ledger
        self.model = model

    def plan(self, text: str, today: dt.date | None = None) -> Spec:
        text = (text or "").strip()
        if not 3 <= len(text) <= 500:
            raise PlannerError("describe what you want in 3-500 characters")
        today = today or dt.date.today()
        user = (f"Today is {today.isoformat()}.\n<user_request>\n"
                f"{text.replace('<', '‹').replace('>', '›')}\n</user_request>")
        # Worst case: every output token used, possibly served by a pricier fallback model.
        self.ledger.check_single(cost(MOST_EXPENSIVE, estimate_tokens(SYSTEM + user) + 400, 8000))
        kwargs = dict(model=self.model, max_tokens=8000, system=SYSTEM, messages=[{"role": "user", "content": user}],
                      output_config={"format": {"type": "json_schema", "schema": SPEC_SCHEMA}, "effort": "low"})
        if self.model.startswith("claude-opus-5"):
            resp = self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                                                    **kwargs)
        else:
            resp = self.client.messages.create(**kwargs)
        served = getattr(resp, "model", None) or self.model
        if resp.stop_reason in ("refusal", "max_tokens"):
            self.ledger.record(run_id=None, purpose="query_plan", model=served, usage=resp.usage, ok=False,
                               detail=f"stop_reason={resp.stop_reason}")
            raise PlannerError("Claude couldn't turn that into a filter; try rephrasing")
        raw = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
        try:
            spec = parse_spec(json.loads(raw))
        except (ValueError, SpecError) as e:
            self.ledger.record(run_id=None, purpose="query_plan", model=served, usage=resp.usage, ok=False,
                               detail=f"rejected: {e}"[:300])
            raise PlannerError(f"Claude's filter was rejected by validation ({e}); try rephrasing") from e
        self.ledger.record(run_id=None, purpose="query_plan", model=served, usage=resp.usage, ok=True)
        return spec
