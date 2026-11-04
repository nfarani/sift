"""Background job runner: one scan or run at a time, state lives in SQLite (never in memory only)."""
from __future__ import annotations

import logging
import threading
import traceback
from typing import Callable

from .db import Database, now_iso

log = logging.getLogger("sift.jobs")


class JobRunner:
    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def submit(self, name: str, fn: Callable[[], object], on_crash: Callable[[str], None]) -> None:
        with self._lock:
            if self.busy():
                raise RuntimeError("another job is running")

            def target():
                try:
                    fn()
                except Exception as e:  # recorded in DB so the UI shows it; never swallowed silently
                    log.error("job %s crashed: %s", name, traceback.format_exc())
                    on_crash(f"{type(e).__name__}: {e}")

            self._thread = threading.Thread(target=target, name=f"sift-{name}", daemon=True)
            self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)


def recover_interrupted(db: Database) -> None:
    """On startup, anything still marked running was interrupted by a restart."""
    with db.tx() as c:
        c.execute("UPDATE scans SET status='interrupted', updated_at=? WHERE status='running'", (now_iso(),))
        c.execute("UPDATE runs SET status='failed', error=COALESCE(error,'interrupted by restart'), finished_at=? "
                  "WHERE status='running'", (now_iso(),))
        c.execute("UPDATE plans SET status='partial' WHERE status='running'")
        c.execute("UPDATE ai_runs SET status='failed', error=COALESCE(error,'interrupted by restart'), finished_at=? "
                  "WHERE status='running'", (now_iso(),))
