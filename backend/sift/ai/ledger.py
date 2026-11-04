"""Token accounting and the hard spend cap. Every Anthropic call is recorded here, accepted or not."""
from __future__ import annotations

from ..db import Database, now_iso

# USD per million tokens. Checked 2026-11-04 against the claude-api skill's model table.
# Unknown models are refused rather than guessed, so the cap can never be computed against $0.
PRICES = {
    "claude-haiku-4-5":  {"in": 1.00, "out": 5.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-sonnet-5-5": {"in": 2.00, "out": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-opus-5-5":   {"in": 4.00, "out": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    # Possible server-side refusal-fallback targets for Opus 5.5 requests.
    "claude-opus-4-8":   {"in": 5.00, "out": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-5":     {"in": 5.00, "out": 25.00, "cache_read": 0.50, "cache_write": 6.25},
}
MOST_EXPENSIVE = max(PRICES, key=lambda m: PRICES[m]["out"])


class SpendCapReached(Exception):
    pass


def price(model: str) -> dict:
    if model not in PRICES:
        raise ValueError(f"no price on file for model {model!r}; add it to sift/ai/ledger.py before using it")
    return PRICES[model]


def cost(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    p = price(model)
    return (input_tokens * p["in"] + output_tokens * p["out"] + cache_read * p["cache_read"]
            + cache_write * p["cache_write"]) / 1_000_000


def estimate_tokens(text: str) -> int:
    """Conservative local estimate (no API call): ~3.5 characters per token, rounded up 15%."""
    return int(len(text) / 3.5 * 1.15) + 1


class Ledger:
    def __init__(self, db: Database, cap_usd: float):
        self.db = db
        self.cap_usd = cap_usd

    def spent(self, run_id: int | None = None) -> float:
        with self.db.connect() as c:
            if run_id is None:
                return c.execute("SELECT COALESCE(SUM(cost_usd),0) FROM ai_usage").fetchone()[0]
            return c.execute("SELECT COALESCE(SUM(cost_usd),0) FROM ai_usage WHERE run_id=?", (run_id,)).fetchone()[0]

    def check(self, run_id: int | None, next_cost_estimate: float) -> None:
        """Refuse a call that could push this run past the cap."""
        if self.spent(run_id) + next_cost_estimate > self.cap_usd:
            raise SpendCapReached(f"spend cap ${self.cap_usd:.2f} would be exceeded")

    def check_single(self, next_cost_estimate: float) -> None:
        """For one-off calls outside a run (query planning): the call alone must fit under the cap."""
        if next_cost_estimate > self.cap_usd:
            raise SpendCapReached(f"this request alone could exceed the ${self.cap_usd:.2f} cap")

    def record(self, *, run_id, purpose, model, usage, ok: bool, detail: str | None = None) -> float:
        it = getattr(usage, "input_tokens", 0) or 0
        ot = getattr(usage, "output_tokens", 0) or 0
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        # A fallback may have served the request; price by the serving model, or the dearest known one.
        served = model if model in PRICES else MOST_EXPENSIVE
        usd = cost(served, it, ot, cr, cw)
        with self.db.connect() as c:
            c.execute("INSERT INTO ai_usage(ts,run_id,purpose,model,input_tokens,output_tokens,cache_read_tokens,"
                      "cache_write_tokens,cost_usd,ok,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (now_iso(), run_id, purpose, model, it, ot, cr, cw, usd, int(ok), detail))
        return usd

    def totals(self) -> dict:
        with self.db.connect() as c:
            rows = c.execute("SELECT purpose, model, COUNT(*) calls, SUM(input_tokens) i, SUM(output_tokens) o, "
                             "SUM(cost_usd) usd, SUM(ok=0) rejected FROM ai_usage GROUP BY purpose, model").fetchall()
        return {"by": [dict(r) for r in rows], "total_usd": round(sum(r["usd"] or 0 for r in rows), 4)}
