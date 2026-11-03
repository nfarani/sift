"""MockProvider: a Gmail-shaped provider backed by a local SQLite file of synthetic mail.

Behaves like Gmail where it matters for safety: paginated listing, metadata-only reads
that never touch UNREAD, label ids, batchModify capped at 1,000 ids, trash/untrash,
and no delete or send at all. Supports fault injection for tests (429/500 sequences).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import defaultdict
from pathlib import Path

from .base import MailProvider, MessageMeta, Page, PermanentError, TransientError
from .synthetic import USER_EMAIL, generate

SYSTEM_LABELS = {"INBOX", "SENT", "TRASH", "SPAM", "UNREAD", "STARRED", "IMPORTANT",
                 "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES",
                 "CATEGORY_FORUMS"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mbox (
    id TEXT PRIMARY KEY, thread_id TEXT, from_addr TEXT, from_name TEXT, to_addrs TEXT,
    subject TEXT, snippet TEXT, internal_date INTEGER, labels TEXT,
    list_id TEXT, list_unsubscribe TEXT, list_unsubscribe_post TEXT
);
CREATE INDEX IF NOT EXISTS ix_mbox_date ON mbox(internal_date DESC, id);
CREATE TABLE IF NOT EXISTS user_labels (id TEXT PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class MockProvider(MailProvider):
    name = "mock"
    synthetic = True

    def __init__(self, path: Path, size: int = 45_000, seed: int = 1234, latency_ms: int = 0):
        self.path = Path(path)
        self.latency = latency_ms / 1000
        self._lock = threading.Lock()
        # Fault injection: method name -> queue of HTTP statuses to raise on successive calls.
        self.faults: dict[str, list[int]] = defaultdict(list)
        self.calls: dict[str, int] = defaultdict(int)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            row = c.execute("SELECT value FROM meta WHERE key='generated'").fetchone()
        if not row:
            self.regenerate(size, seed)

    # -- plumbing -------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _enter(self, method: str) -> None:
        self.calls[method] += 1
        if self.latency:
            time.sleep(self.latency)
        q = self.faults.get(method)
        if q:
            status = q.pop(0)
            if status == 429 or status >= 500:
                raise TransientError(status, "simulated")
            raise PermanentError(f"HTTP {status} simulated")

    def regenerate(self, size: int, seed: int) -> None:
        rows = generate(size=size, seed=seed)
        with self._lock, self._conn() as c:
            c.execute("BEGIN")
            c.execute("DELETE FROM mbox")
            c.execute("DELETE FROM user_labels")
            c.executemany(
                "INSERT INTO mbox VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(m["id"], m["thread_id"], m["from_addr"], m["from_name"], json.dumps(m["to_addrs"]),
                  m["subject"], m["snippet"], m["internal_date"], json.dumps(m["labels"]), m["list_id"],
                  m["list_unsubscribe"], m["list_unsubscribe_post"]) for m in rows],
            )
            c.execute("INSERT OR REPLACE INTO meta VALUES ('generated', ?)", (json.dumps({"size": size, "seed": seed}),))
            c.execute("COMMIT")

    # -- read -----------------------------------------------------------------
    def account_email(self) -> str:
        return USER_EMAIL

    def profile(self) -> dict:
        with self._conn() as c:
            n = c.execute("SELECT COUNT(*) FROM mbox").fetchone()[0]
            t = c.execute("SELECT COUNT(DISTINCT thread_id) FROM mbox").fetchone()[0]
        return {"email": USER_EMAIL, "messages_total": n, "threads_total": t}

    def count_label(self, label: str) -> int:
        self._enter("labels")
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM mbox WHERE labels LIKE ? AND labels NOT LIKE '%\"TRASH\"%'",
                             (f'%"{label}"%',)).fetchone()[0]

    def list_message_ids(self, label: str, page_token: str | None = None, max_results: int = 500) -> Page:
        self._enter("list")
        offset = int(page_token) if page_token else 0
        like = f'%"{label}"%'
        # Like Gmail, listing excludes Trash/Spam unless that label is asked for.
        where = "labels LIKE ?" + ("" if label in ("TRASH", "SPAM") else " AND labels NOT LIKE '%\"TRASH\"%'")
        with self._conn() as c:
            ids = [r[0] for r in c.execute(
                f"SELECT id FROM mbox WHERE {where} ORDER BY internal_date DESC, id LIMIT ? OFFSET ?",
                (like, max_results, offset))]
            est = c.execute(f"SELECT COUNT(*) FROM mbox WHERE {where}", (like,)).fetchone()[0] if offset == 0 else None
        nxt = str(offset + len(ids)) if len(ids) == max_results else None
        return Page(ids=ids, next_page_token=nxt, result_size_estimate=est)

    def get_metadata(self, ids: list[str]) -> list[MessageMeta]:
        self._enter("get")
        if not ids:
            return []
        with self._conn() as c:
            q = ",".join("?" * len(ids))
            rows = c.execute(f"SELECT * FROM mbox WHERE id IN ({q})", ids).fetchall()
        by_id = {r["id"]: r for r in rows}
        out = []
        for i in ids:
            r = by_id.get(i)
            if r is None:
                continue
            out.append(MessageMeta(
                id=r["id"], thread_id=r["thread_id"], from_addr=r["from_addr"], from_name=r["from_name"] or "",
                to_addrs=json.loads(r["to_addrs"]), subject=r["subject"], snippet=r["snippet"],
                internal_date=r["internal_date"], labels=json.loads(r["labels"]), list_id=r["list_id"],
                list_unsubscribe=r["list_unsubscribe"], list_unsubscribe_post=r["list_unsubscribe_post"]))
        return out

    def threads_with_sent(self, thread_ids: list[str]) -> set[str]:
        self._enter("threads")
        if not thread_ids:
            return set()
        q = ",".join("?" * len(thread_ids))
        with self._conn() as c:
            return {r[0] for r in c.execute(
                f"SELECT DISTINCT thread_id FROM mbox WHERE thread_id IN ({q}) AND labels LIKE '%\"SENT\"%'",
                list(thread_ids))}

    # -- write (executor only) ------------------------------------------------
    def ensure_label(self, name: str) -> str:
        self._enter("labels")
        with self._lock, self._conn() as c:
            row = c.execute("SELECT id FROM user_labels WHERE name=?", (name,)).fetchone()
            if row:
                return row[0]
            n = c.execute("SELECT COUNT(*) FROM user_labels").fetchone()[0]
            lid = f"Label_{n + 1}"
            c.execute("INSERT INTO user_labels VALUES (?,?)", (lid, name))
            return lid

    def _valid_label(self, c: sqlite3.Connection, lid: str) -> bool:
        return lid in SYSTEM_LABELS or c.execute("SELECT 1 FROM user_labels WHERE id=?", (lid,)).fetchone() is not None

    def batch_modify(self, ids: list[str], add: list[str], remove: list[str]) -> None:
        self._enter("modify")
        if len(ids) > self.max_modify_batch:
            raise PermanentError(f"batchModify accepts at most {self.max_modify_batch} ids")
        if "TRASH" in add or "SPAM" in add:
            raise PermanentError("use trash() to move to Trash")
        with self._lock, self._conn() as c:
            for lid in add + remove:
                if not self._valid_label(c, lid):
                    raise PermanentError(f"unknown label {lid}")
            c.execute("BEGIN")
            for mid in ids:
                row = c.execute("SELECT labels FROM mbox WHERE id=?", (mid,)).fetchone()
                if row is None:
                    continue  # Gmail silently ignores unknown ids in batchModify
                labels = [l for l in json.loads(row[0]) if l not in remove]
                labels += [l for l in add if l not in labels]
                c.execute("UPDATE mbox SET labels=? WHERE id=?", (json.dumps(labels), mid))
            c.execute("COMMIT")

    def _set_trash(self, mid: str, on: bool) -> None:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT labels FROM mbox WHERE id=?", (mid,)).fetchone()
            if row is None:
                raise PermanentError("HTTP 404 not found")
            labels = [l for l in json.loads(row[0]) if l != "TRASH"]
            if on:
                labels.append("TRASH")
            c.execute("UPDATE mbox SET labels=? WHERE id=?", (json.dumps(labels), mid))

    def trash(self, message_id: str) -> None:
        self._enter("trash")
        self._set_trash(message_id, True)

    def untrash(self, message_id: str) -> None:
        self._enter("untrash")
        self._set_trash(message_id, False)

    # -- test helpers (not part of MailProvider) -------------------------------
    def _test_set_labels(self, mid: str, labels: list[str]) -> None:
        with self._conn() as c:
            c.execute("UPDATE mbox SET labels=? WHERE id=?", (json.dumps(labels), mid))

    def _test_remove(self, mid: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM mbox WHERE id=?", (mid,))

    def label_name(self, lid: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT name FROM user_labels WHERE id=?", (lid,)).fetchone()
        return row[0] if row else (lid if lid in SYSTEM_LABELS else None)
