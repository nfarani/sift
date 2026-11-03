"""Metadata-only scanner with SQLite checkpoints, cancel and resume.

Phases: `sent` (to learn who you correspond with), `inbox`, then `analyse`.
The checkpoint (phase + next page token) is written only after a page's metadata is
stored, so a crash or cancel never skips messages; upserts make re-reads harmless.
"""
from __future__ import annotations

import json
import time

from .analysis import run_analysis
from .classifier.base import Classifier
from .db import Database, now_iso
from .providers.base import MailReader, MessageMeta, ProviderError
from .retry import RetryPolicy

PAGE_SIZE = 500
META_BATCH = 50  # Google recommends <=50 calls per HTTP batch for Gmail
PHASES = ["sent", "inbox"]
LABEL_FOR = {"sent": "SENT", "inbox": "INBOX"}


class ScanCancelled(Exception):
    pass


def _row(m: MessageMeta, user_email: str, scan_id: int) -> tuple:
    labels = m.labels
    return (m.id, m.thread_id, m.from_addr.lower(), m.from_name, m.from_domain, ",".join(a.lower() for a in m.to_addrs),
            m.subject, m.snippet[:200], m.internal_date, json.dumps(labels), m.list_id, m.list_unsubscribe,
            1 if (m.list_unsubscribe_post and "one-click" in m.list_unsubscribe_post.lower()
                  and "https://" in (m.list_unsubscribe or "")) else 0,
            1 if ("SENT" in labels or m.from_addr.lower() == user_email.lower()) else 0,
            1 if "INBOX" in labels else 0, 1 if "UNREAD" in labels else 0, scan_id)


UPSERT = """
INSERT INTO messages(id,thread_id,from_addr,from_name,from_domain,to_addrs,subject,snippet,internal_date,labels,
                     list_id,list_unsubscribe,one_click_unsub,is_sent,in_inbox,is_unread,scan_id)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(id) DO UPDATE SET labels=excluded.labels, in_inbox=excluded.in_inbox, is_unread=excluded.is_unread,
                              scan_id=excluded.scan_id
"""


class Scanner:
    def __init__(self, db: Database, provider: MailReader, classifier: Classifier,
                 retry: RetryPolicy | None = None, page_size: int = PAGE_SIZE, meta_batch: int = META_BATCH):
        self.db = db
        self.provider = provider
        self.classifier = classifier
        self.retry = retry or RetryPolicy()
        self.page_size = page_size
        self.meta_batch = meta_batch

    # -- scan records ---------------------------------------------------------
    def start(self, max_per_phase: int | None = None) -> int:
        """Begin a fresh scan. Local message cache is rebuilt from the provider.
        `max_per_phase` caps how many Sent and how many Inbox messages are read (test scans)."""
        with self.db.tx() as c:
            c.execute("DELETE FROM messages")
            c.execute("DELETE FROM sender_groups")
            c.execute("DELETE FROM recommendations")
            cur = c.execute(
                "INSERT INTO scans(status,phase,started_at,updated_at,checkpoint,max_per_phase) "
                "VALUES ('running','sent',?,?,?,?)",
                (now_iso(), now_iso(), json.dumps({"phase": "sent", "page_token": None}), max_per_phase))
            return cur.lastrowid

    def resumable(self) -> dict | None:
        with self.db.connect() as c:
            r = c.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        return dict(r) if r and r["status"] in ("cancelled", "failed", "interrupted") else None

    def prepare_resume(self, scan_id: int) -> None:
        with self.db.connect() as c:
            c.execute("UPDATE scans SET status='running', cancel_requested=0, updated_at=? WHERE id=?",
                      (now_iso(), scan_id))

    def request_cancel(self, scan_id: int) -> None:
        with self.db.connect() as c:
            c.execute("UPDATE scans SET cancel_requested=1 WHERE id=? AND status='running'", (scan_id,))

    def _cancelled(self, scan_id: int) -> bool:
        with self.db.connect() as c:
            return bool(c.execute("SELECT cancel_requested FROM scans WHERE id=?", (scan_id,)).fetchone()[0])

    def _update(self, scan_id: int, **fields) -> None:
        fields["updated_at"] = now_iso()
        cols = ",".join(f"{k}=?" for k in fields)
        with self.db.connect() as c:
            c.execute(f"UPDATE scans SET {cols} WHERE id=?", (*fields.values(), scan_id))

    def _error(self, scan_id: int, msg: str) -> None:
        with self.db.tx() as c:
            r = c.execute("SELECT errors, error_log FROM scans WHERE id=?", (scan_id,)).fetchone()
            log = json.loads(r["error_log"])[-49:] + [{"at": now_iso(), "error": msg}]
            c.execute("UPDATE scans SET errors=?, error_log=? WHERE id=?", (r["errors"] + 1, json.dumps(log), scan_id))

    # -- the work -------------------------------------------------------------
    def run(self, scan_id: int) -> str:
        """Run (or resume) a scan to completion, cancellation or failure. Returns final status."""
        t0 = time.monotonic()
        with self.db.connect() as c:
            row = c.execute("SELECT checkpoint, elapsed_s, max_per_phase FROM scans WHERE id=?", (scan_id,)).fetchone()
        cp = json.loads(row["checkpoint"])
        cap = row["max_per_phase"]
        base_elapsed = row["elapsed_s"]
        user = self.provider.account_email()

        with self.db.connect() as c:  # counters survive resume
            seen_ids = {r[0] for r in c.execute("SELECT id FROM messages")}
            senders = {r[0] for r in c.execute("SELECT DISTINCT from_addr FROM messages WHERE is_sent=0")}
            per_phase = {"sent": c.execute("SELECT COUNT(*) FROM messages WHERE is_sent=1").fetchone()[0],
                         "inbox": c.execute("SELECT COUNT(*) FROM messages WHERE is_sent=0").fetchone()[0]}
        start_count = len(seen_ids)
        est_total = None

        def elapsed() -> float:
            return round(base_elapsed + time.monotonic() - t0, 1)

        def throughput() -> tuple[float | None, float | None]:
            """Messages/second measured in this session, and the estimated seconds remaining."""
            secs = time.monotonic() - t0
            done = len(seen_ids) - start_count
            if secs < 1 or done <= 0:
                return None, None
            rate = done / secs
            left = max(0, (est_total or 0) - len(seen_ids))
            return round(rate, 2), round(left / rate, 0)

        def note_retry(attempt: int, e: Exception) -> None:
            self._error(scan_id, f"retry {attempt}: {e}")

        try:
            if cp.get("phase") in PHASES:
                # Label names let protection rules like "Work" resolve to Gmail's label ids.
                self.db.set_setting("labels.map", self.retry.call(self.provider.label_names, note_retry))
                counts = [self.retry.call(lambda l=l: self.provider.count_label(l), note_retry)
                          for l in LABEL_FOR.values()]
                est_total = sum(min(n, cap) if cap else n for n in counts)
                self._update(scan_id, total_estimate=est_total)
                for phase in PHASES[PHASES.index(cp["phase"]):]:
                    token = cp.get("page_token") if phase == cp["phase"] else None
                    self._update(scan_id, phase=phase)
                    while True:
                        if self._cancelled(scan_id):
                            raise ScanCancelled()
                        if cap is not None and per_phase[phase] >= cap:
                            break
                        page = self.retry.call(lambda: self.provider.list_message_ids(
                            LABEL_FOR[phase], token, self.page_size), note_retry)
                        if cap is not None:
                            page.ids = page.ids[:cap - per_phase[phase]]
                        for i in range(0, len(page.ids), self.meta_batch):
                            if self._cancelled(scan_id):
                                raise ScanCancelled()
                            chunk = page.ids[i:i + self.meta_batch]
                            metas = self.retry.call(lambda: self.provider.get_metadata(chunk), note_retry)
                            with self.db.tx() as c:
                                c.executemany(UPSERT, [_row(m, user, scan_id) for m in metas])
                                for m in metas:
                                    if m.id not in seen_ids:
                                        seen_ids.add(m.id)
                                        per_phase[phase] += 1
                                        if "SENT" not in m.labels:
                                            senders.add(m.from_addr.lower())
                                rate, eta = throughput()
                                c.execute("UPDATE scans SET messages_scanned=?, senders_discovered=?, elapsed_s=?, "
                                          "rate_mps=?, eta_s=?, updated_at=? WHERE id=?",
                                          (len(seen_ids), len(senders), elapsed(), rate, eta, now_iso(), scan_id))
                        token = page.next_page_token
                        # checkpoint only after the whole page is stored
                        self._update(scan_id, checkpoint=json.dumps({"phase": phase, "page_token": token}))
                        if token is None:
                            break
                cp = {"phase": "analyse"}
                self._update(scan_id, checkpoint=json.dumps(cp))
            self._update(scan_id, phase="analyse", elapsed_s=elapsed(), eta_s=None)
            res = run_analysis(self.db, self.classifier, user, scan_id)
            self._update(scan_id, status="completed", phase="done", finished_at=now_iso(), elapsed_s=elapsed(),
                         senders_discovered=res["senders"], categories_detected=res["categories"],
                         checkpoint=json.dumps({"phase": "done"}))
            return "completed"
        except ScanCancelled:
            self._update(scan_id, status="cancelled", elapsed_s=elapsed())
            return "cancelled"
        except ProviderError as e:
            self._error(scan_id, f"gave up: {e}")
            self._update(scan_id, status="failed", elapsed_s=elapsed())
            return "failed"
        except Exception as e:  # unexpected: record it, keep the checkpoint so resume is possible
            self._error(scan_id, f"internal error: {type(e).__name__}: {e}")
            self._update(scan_id, status="failed", elapsed_s=elapsed())
            raise
