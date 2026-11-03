"""SQLite storage. One connection per call site; WAL so the UI can read while jobs write."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scans (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    status             TEXT NOT NULL,           -- running | cancelled | completed | failed
    phase              TEXT NOT NULL,           -- sent | inbox | analyse | done
    started_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    finished_at        TEXT,
    elapsed_s          REAL NOT NULL DEFAULT 0, -- accumulated across resumes
    messages_scanned   INTEGER NOT NULL DEFAULT 0,
    senders_discovered INTEGER NOT NULL DEFAULT 0,
    categories_detected INTEGER NOT NULL DEFAULT 0,
    total_estimate     INTEGER,
    errors             INTEGER NOT NULL DEFAULT 0,
    error_log          TEXT NOT NULL DEFAULT '[]',
    checkpoint         TEXT NOT NULL DEFAULT '{}',
    cancel_requested   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id            TEXT PRIMARY KEY,
    thread_id     TEXT NOT NULL,
    from_addr     TEXT NOT NULL,
    from_name     TEXT,
    from_domain   TEXT NOT NULL,
    to_addrs      TEXT NOT NULL DEFAULT '',
    subject       TEXT NOT NULL DEFAULT '',
    snippet       TEXT NOT NULL DEFAULT '',
    internal_date INTEGER NOT NULL,
    labels        TEXT NOT NULL DEFAULT '[]',
    list_id       TEXT,
    list_unsubscribe TEXT,
    one_click_unsub INTEGER NOT NULL DEFAULT 0,
    is_sent       INTEGER NOT NULL DEFAULT 0,
    in_inbox      INTEGER NOT NULL DEFAULT 0,
    is_unread     INTEGER NOT NULL DEFAULT 0,
    protected     INTEGER NOT NULL DEFAULT 0,
    protect_reason TEXT,
    category      TEXT,
    scan_id       INTEGER
);
CREATE INDEX IF NOT EXISTS ix_msg_from ON messages(from_addr);
CREATE INDEX IF NOT EXISTS ix_msg_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS ix_msg_cat ON messages(category, in_inbox, protected);

CREATE TABLE IF NOT EXISTS sender_groups (
    sender        TEXT PRIMARY KEY,
    domain        TEXT NOT NULL,
    display_name  TEXT,
    total         INTEGER NOT NULL,
    in_inbox      INTEGER NOT NULL,
    unread        INTEGER NOT NULL,
    protected     INTEGER NOT NULL,
    first_date    INTEGER,
    last_date     INTEGER,
    list_id       TEXT,
    has_unsub     INTEGER NOT NULL DEFAULT 0,
    one_click_unsub INTEGER NOT NULL DEFAULT 0,
    gmail_categories TEXT NOT NULL DEFAULT '{}',
    category      TEXT,
    confidence    REAL,
    reason        TEXT,
    classifier    TEXT
);

CREATE TABLE IF NOT EXISTS protection_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL CHECK (kind IN ('sender','domain','label')),
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(kind, value)
);

CREATE TABLE IF NOT EXISTS recommendations (
    id              TEXT PRIMARY KEY,         -- category key, stable across rescans
    category        TEXT NOT NULL,
    title           TEXT NOT NULL,
    action          TEXT NOT NULL,            -- archive | trash | review
    older_than_days INTEGER NOT NULL DEFAULT 0,
    reason          TEXT NOT NULL,
    confidence      REAL NOT NULL,
    classifier      TEXT NOT NULL,
    scan_id         INTEGER,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    status        TEXT NOT NULL,              -- draft | approved | running | completed | partial | cancelled | superseded
    created_at    TEXT NOT NULL,
    approved_at   TEXT,
    spec          TEXT NOT NULL,              -- JSON: the filter each item was built from
    total         INTEGER NOT NULL,
    ids_hash      TEXT NOT NULL,
    summary       TEXT NOT NULL DEFAULT '{}', -- JSON: per-action/per-rec counts, protected exclusions
    inbox_before  INTEGER
);

CREATE TABLE IF NOT EXISTS plan_messages (
    plan_id    INTEGER NOT NULL REFERENCES plans(id),
    message_id TEXT NOT NULL,
    rec_id     TEXT NOT NULL,
    action     TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending', -- pending | done | skipped | failed
    reason     TEXT,
    run_id     INTEGER,
    PRIMARY KEY (plan_id, message_id)
);

-- Plans are frozen once they leave draft: no new rows, and no row may change its id/action/plan.
CREATE TRIGGER IF NOT EXISTS plan_messages_frozen_insert
BEFORE INSERT ON plan_messages
WHEN (SELECT status FROM plans WHERE id = NEW.plan_id) IS NOT 'draft'
BEGIN SELECT RAISE(ABORT, 'plan is frozen: cannot add messages'); END;

CREATE TRIGGER IF NOT EXISTS plan_messages_frozen_update
BEFORE UPDATE OF plan_id, message_id, action, rec_id ON plan_messages
BEGIN SELECT RAISE(ABORT, 'plan is frozen: cannot change message scope'); END;

CREATE TABLE IF NOT EXISTS runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL,           -- execute | restore
    plan_id          INTEGER,
    source_run_id    INTEGER,                 -- for restore: the run being undone
    status           TEXT NOT NULL,           -- running | completed | partial | cancelled | failed
    label_name       TEXT,
    label_id         TEXT,
    cap              INTEGER,
    total            INTEGER NOT NULL DEFAULT 0,
    attempted        INTEGER NOT NULL DEFAULT 0,
    confirmed        INTEGER NOT NULL DEFAULT 0,
    failed           INTEGER NOT NULL DEFAULT 0,
    skipped          INTEGER NOT NULL DEFAULT 0,
    already_done     INTEGER NOT NULL DEFAULT 0,
    current_op       TEXT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    error            TEXT
);

-- Append-only audit log of every state-changing provider call.
CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    run_id        INTEGER,
    plan_id       INTEGER,
    message_id    TEXT,
    action        TEXT NOT NULL,
    before_labels TEXT,
    after_labels  TEXT,
    status        TEXT NOT NULL,              -- confirmed | failed | skipped | unverified
    detail        TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_run ON audit_log(run_id);

CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript(SCHEMA)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # settings -----------------------------------------------------------
    def get_setting(self, key: str, default=None):
        with self.connect() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value) -> None:
        with self.connect() as c:
            c.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def wipe_analysis(self) -> None:
        """Clear scanned data and plans (used when the mock mailbox is regenerated). Audit log is kept."""
        with self.tx() as c:
            for t in ("plan_messages", "plans", "recommendations", "sender_groups", "messages", "scans"):
                c.execute(f"DELETE FROM {t}")
            c.execute("DELETE FROM settings WHERE key LIKE 'cleanup.%'")
