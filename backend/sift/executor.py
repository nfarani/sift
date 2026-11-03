"""Executor: the ONLY module that calls provider write methods.

It accepts a plan id (or, for restore, a run id) and nothing else. Before touching any
message it re-fetches fresh metadata and re-checks protection rules and the plan's filter;
revalidation can only drop ids from the work list, never add. Every state-changing call is
written to the append-only audit log with before/after labels, and every claimed success is
confirmed by re-reading the message afterwards ("attempted" vs "confirmed").
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .db import Database, now_iso
from .plans import verify_frozen
from .protection import load_context, protection_reason
from .providers.base import MailProvider, MessageMeta, ProviderError
from .retry import RetryPolicy

CHUNK = 500  # <= Gmail's 1,000-id batchModify limit
# A cancelled plan is not runnable again: cancelling withdraws the approval. Build and approve a new plan.
RUNNABLE_PLAN = ("approved", "partial", "completed")


class ExecutionError(Exception):
    pass


def unlocked(db: Database) -> bool:
    return bool(db.get_setting("cleanup.full_runs_unlocked", False))


class Executor:
    def __init__(self, db: Database, provider: MailProvider, first_run_cap: int = 50,
                 retry: RetryPolicy | None = None, chunk: int = CHUNK):
        self.db = db
        self.provider = provider
        self.first_run_cap = first_run_cap
        self.retry = retry or RetryPolicy()
        self.chunk = min(chunk, provider.max_modify_batch)

    # -- run records ----------------------------------------------------------
    def create_run(self, plan_id: int) -> int:
        with self.db.tx() as c:
            p = c.execute("SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone()
            if p is None:
                raise ExecutionError("unknown plan")
            if p["status"] not in RUNNABLE_PLAN:
                raise ExecutionError(f"plan is {p['status']}; it must be approved before it can run")
            if not verify_frozen(c, plan_id):
                raise ExecutionError("plan contents changed after approval; refusing to run")
            if c.execute("SELECT 1 FROM runs WHERE status='running'").fetchone():
                raise ExecutionError("another run is in progress")
            last = c.execute("SELECT status FROM scans ORDER BY id DESC LIMIT 1").fetchone()
            if last is None or last["status"] != "completed":
                # protection context (your sent mail, rules) comes from the last scan; it must be complete
                raise ExecutionError("the latest scan is not complete; finish or resume it before running a plan")
            cap = None if unlocked(self.db) else self.first_run_cap
            cur = c.execute("INSERT INTO runs(kind,plan_id,status,cap,started_at,current_op) "
                            "VALUES ('execute',?,'running',?,?,'starting')", (plan_id, cap, now_iso()))
            run_id = cur.lastrowid
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            c.execute("UPDATE runs SET label_name=? WHERE id=?", (f"Sift/{date}-run-{run_id}", run_id))
            c.execute("UPDATE plans SET status='running' WHERE id=?", (plan_id,))
        return run_id

    def create_restore(self, source_run_id: int) -> int:
        with self.db.tx() as c:
            src = c.execute("SELECT * FROM runs WHERE id=?", (source_run_id,)).fetchone()
            if src is None or src["kind"] != "execute":
                raise ExecutionError("only execution runs can be restored")
            if src["status"] == "running":
                raise ExecutionError("run is still in progress")
            if c.execute("SELECT 1 FROM runs WHERE status='running'").fetchone():
                raise ExecutionError("another run is in progress")
            cur = c.execute("INSERT INTO runs(kind,plan_id,source_run_id,status,label_name,label_id,started_at,current_op) "
                            "VALUES ('restore',?,?,'running',?,?,?,'starting')",
                            (src["plan_id"], source_run_id, src["label_name"], src["label_id"], now_iso()))
            return cur.lastrowid

    def request_cancel(self, run_id: int) -> None:
        with self.db.connect() as c:
            c.execute("UPDATE runs SET cancel_requested=1 WHERE id=? AND status='running'", (run_id,))

    def _cancelled(self, run_id: int) -> bool:
        with self.db.connect() as c:
            return bool(c.execute("SELECT cancel_requested FROM runs WHERE id=?", (run_id,)).fetchone()[0])

    def _bump(self, run_id: int, **delta) -> None:
        sets = ",".join(f"{k}={k}+?" for k in delta)
        with self.db.connect() as c:
            c.execute(f"UPDATE runs SET {sets} WHERE id=?", (*delta.values(), run_id))

    def _set(self, run_id: int, **fields) -> None:
        cols = ",".join(f"{k}=?" for k in fields)
        with self.db.connect() as c:
            c.execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    def _audit(self, c, run_id, plan_id, mid, action, before, after, status, detail=None) -> None:
        c.execute("INSERT INTO audit_log(ts,run_id,plan_id,message_id,action,before_labels,after_labels,status,detail) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (now_iso(), run_id, plan_id, mid, action, json.dumps(before) if before is not None else None,
                   json.dumps(after) if after is not None else None, status, detail))

    def _fetch(self, ids: list[str]) -> dict[str, MessageMeta]:
        return {m.id: m for m in self.retry.call(lambda: self.provider.get_metadata(ids))}

    def _sync_local(self, c, metas: dict[str, MessageMeta]) -> None:
        c.executemany("UPDATE messages SET labels=?, in_inbox=?, is_unread=? WHERE id=?",
                      [(json.dumps(m.labels), int("INBOX" in m.labels), int("UNREAD" in m.labels), m.id)
                       for m in metas.values()])

    # -- execute --------------------------------------------------------------
    def run(self, run_id: int) -> str:
        with self.db.connect() as c:
            run = dict(c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            plan_id = run["plan_id"]
            spec = {it["rec_id"]: it for it in json.loads(c.execute("SELECT spec FROM plans WHERE id=?",
                                                                  (plan_id,)).fetchone()[0])}
            work = [(r["message_id"], r["rec_id"], r["action"]) for r in c.execute(
                "SELECT message_id, rec_id, action FROM plan_messages WHERE plan_id=? AND status IN ('pending','failed') "
                "ORDER BY message_id", (plan_id,))]
        frozen = {w[0] for w in work}
        if run["cap"] is not None:
            work = work[:run["cap"]]
        self._set(run_id, total=len(work))
        ctx = load_context(self.db, self.provider.account_email())
        status = "completed"
        try:
            label_id = self.retry.call(lambda: self.provider.ensure_label(run["label_name"]))
            self._set(run_id, label_id=label_id)
            with self.db.connect() as c:
                self._audit(c, run_id, plan_id, None, "ensure_label", None, None, "confirmed",
                            f"{run['label_name']} -> {label_id}")
            for i in range(0, len(work), self.chunk):
                if self._cancelled(run_id):
                    status = "cancelled"
                    break
                chunk = work[i:i + self.chunk]
                self._set(run_id, current_op=f"revalidating {i + 1}–{i + len(chunk)} of {len(work)}")
                if not self._process_chunk(run_id, plan_id, chunk, spec, ctx, label_id, frozen):
                    status = "partial"
        except ProviderError as e:
            status = "failed"
            self._set(run_id, error=str(e))
        with self.db.tx() as c:
            r = c.execute("SELECT failed FROM runs WHERE id=?", (run_id,)).fetchone()
            if status == "completed" and r["failed"]:
                status = "partial"
            left = c.execute("SELECT COUNT(*) FROM plan_messages WHERE plan_id=? AND status IN ('pending','failed')",
                             (plan_id,)).fetchone()[0]
            plan_status = "completed" if left == 0 else ("cancelled" if status == "cancelled" else "partial")
            c.execute("UPDATE plans SET status=? WHERE id=?", (plan_status, plan_id))
            c.execute("UPDATE runs SET status=?, finished_at=?, current_op=? WHERE id=?",
                      (status, now_iso(), "finished" if status != "cancelled" else "cancelled by you", run_id))
        return status

    def _revalidate(self, mid: str, rec_id: str, action: str, m: MessageMeta | None, spec: dict, ctx,
                    replied_threads: set[str]) -> str | None:
        """Return a skip reason, or None if the message may still be changed."""
        if m is None:
            return "no longer exists"
        if m.thread_id in replied_threads:
            return "newly protected: thread contains your reply"
        if "INBOX" not in m.labels and "TRASH" not in m.labels:
            # The plan only ever selects Inbox mail; if you've filed it elsewhere since, leave it alone.
            return "no longer in Inbox"
        reason = protection_reason(labels=m.labels, thread_id=m.thread_id, from_addr=m.from_addr,
                                   subject=m.subject, ctx=ctx)
        if reason:
            return f"newly protected: {reason}"
        item = spec.get(rec_id)
        if item is None:
            return "not in plan spec"
        if m.from_addr.lower() in item["excluded_senders"]:
            return "sender excluded"
        if m.internal_date >= item["cutoff_ms"]:
            return "newer than the plan's cutoff"
        if "SENT" in m.labels:
            return "sent by you"
        return None

    def _process_chunk(self, run_id, plan_id, chunk, spec, ctx, label_id, frozen) -> bool:
        ids = [w[0] for w in chunk]
        if not set(ids) <= frozen:  # revalidation only ever narrows the frozen set
            raise ExecutionError("work list contains ids outside the approved plan")
        before = self._fetch(ids)
        threads = sorted({m.thread_id for m in before.values()})
        replied = self.retry.call(lambda: self.provider.threads_with_sent(threads)) if threads else set()
        with self.db.connect() as c:  # labels applied by earlier runs of this same plan
            own_labels = {r[0] for r in c.execute("SELECT label_id FROM runs WHERE plan_id=? AND kind='execute' "
                                                  "AND label_id IS NOT NULL", (plan_id,))}
        to_archive, to_trash, skipped, noop = [], [], [], []
        for mid, rec_id, action in chunk:
            m = before.get(mid)
            # Idempotency: an earlier run of this plan already applied the change (e.g. crashed before recording it).
            if m is not None and own_labels & set(m.labels) and (
                    (action == "archive" and "INBOX" not in m.labels) or (action == "trash" and "TRASH" in m.labels)):
                noop.append((mid, action))
                continue
            why = self._revalidate(mid, rec_id, action, m, spec, ctx, replied)
            if why:
                skipped.append((mid, action, why))
            elif action == "trash" and "TRASH" in m.labels:
                skipped.append((mid, action, "already in Trash"))
            else:
                (to_archive if action == "archive" else to_trash).append(mid)
        with self.db.tx() as c:
            for mid, action, why in skipped:
                c.execute("UPDATE plan_messages SET status='skipped', reason=?, run_id=? WHERE plan_id=? AND message_id=?",
                          (why, run_id, plan_id, mid))
                self._audit(c, run_id, plan_id, mid, action, before[mid].labels if mid in before else None,
                            None, "skipped", why)
            for mid, action in noop:
                c.execute("UPDATE plan_messages SET status='done', reason='already in target state', run_id=? "
                          "WHERE plan_id=? AND message_id=?", (run_id, plan_id, mid))
        self._bump(run_id, skipped=len(skipped), already_done=len(noop))

        ok = True
        errors: dict[str, str] = {}
        if to_archive or to_trash:
            # Write-ahead: record intent before calling the provider, so a crash mid-call still leaves a trail
            # that restore can follow.
            with self.db.tx() as c:
                for mid in to_archive + to_trash:
                    self._audit(c, run_id, plan_id, mid, "archive" if mid in to_archive else "trash",
                                before[mid].labels, None, "attempted")
        if to_archive:
            self._set(run_id, current_op=f"archiving {len(to_archive)} messages")
            self._bump(run_id, attempted=len(to_archive))
            try:
                self.retry.call(lambda: self.provider.batch_modify(to_archive, add=[label_id], remove=["INBOX"]))
            except ProviderError as e:
                errors.update({m: str(e) for m in to_archive})
        if to_trash:
            self._set(run_id, current_op=f"moving {len(to_trash)} messages to Trash")
            self._bump(run_id, attempted=len(to_trash))
            try:
                self.retry.call(lambda: self.provider.batch_modify(to_trash, add=[label_id], remove=[]))
            except ProviderError as e:
                errors.update({m: str(e) for m in to_trash})
            for mid in to_trash:
                if mid in errors:
                    continue
                try:
                    self.retry.call(lambda: self.provider.trash(mid))
                except ProviderError as e:
                    errors[mid] = str(e)

        changed = to_archive + to_trash
        if not changed:
            return True
        self._set(run_id, current_op="confirming changes")
        try:
            after = self._fetch(changed)
        except ProviderError as e:
            after = {}
            errors.update({m: errors.get(m, f"could not confirm: {e}") for m in changed})
        confirmed = failed = 0
        with self.db.tx() as c:
            for mid in changed:
                action = "archive" if mid in to_archive else "trash"
                a = after.get(mid)
                good = a is not None and label_id in a.labels and (
                    "INBOX" not in a.labels if action == "archive" else "TRASH" in a.labels)
                if good:
                    confirmed += 1
                    c.execute("UPDATE plan_messages SET status='done', reason=NULL, run_id=? WHERE plan_id=? AND message_id=?",
                              (run_id, plan_id, mid))
                    self._audit(c, run_id, plan_id, mid, action, before[mid].labels, a.labels, "confirmed")
                else:
                    failed += 1
                    why = errors.get(mid, "change not visible after the call")
                    c.execute("UPDATE plan_messages SET status='failed', reason=?, run_id=? WHERE plan_id=? AND message_id=?",
                              (why, run_id, plan_id, mid))
                    self._audit(c, run_id, plan_id, mid, action, before[mid].labels, a.labels if a else None,
                                "failed", why)
            self._sync_local(c, after)
        self._bump(run_id, confirmed=confirmed, failed=failed)
        if failed:
            ok = False
        return ok

    # -- restore --------------------------------------------------------------
    def restore(self, run_id: int) -> str:
        """Undo the confirmed changes of an execution run, using the audit log as the source of truth."""
        with self.db.connect() as c:
            run = dict(c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            src = run["source_run_id"]
            work = [(r["message_id"], r["action"]) for r in c.execute(
                "SELECT message_id, MIN(action) action FROM audit_log WHERE run_id=? "
                "AND status IN ('attempted','confirmed') AND action IN ('archive','trash') "
                "GROUP BY message_id ORDER BY MIN(id)", (src,))]
        label_id = run["label_id"]
        self._set(run_id, total=len(work))
        status = "completed"
        try:
            for i in range(0, len(work), self.chunk):
                if self._cancelled(run_id):
                    status = "cancelled"
                    break
                chunk = work[i:i + self.chunk]
                self._set(run_id, current_op=f"restoring {i + 1}–{i + len(chunk)} of {len(work)}")
                before = self._fetch([w[0] for w in chunk])
                pending, errors = [], {}
                missing = [w for w in chunk if w[0] not in before]
                for mid, action in chunk:
                    m = before.get(mid)
                    if m is None:
                        continue
                    if "INBOX" in m.labels and "TRASH" not in m.labels and label_id not in m.labels:
                        self._bump(run_id, already_done=1)
                        continue
                    pending.append((mid, action))
                self._bump(run_id, attempted=len(pending))
                for mid, action in pending:
                    if action == "trash" and "TRASH" in before[mid].labels:
                        try:
                            self.retry.call(lambda: self.provider.untrash(mid))
                        except ProviderError as e:
                            errors[mid] = str(e)
                ids = [mid for mid, _ in pending if mid not in errors]
                if ids:
                    try:
                        self.retry.call(lambda: self.provider.batch_modify(ids, add=["INBOX"],
                                                                           remove=[label_id] if label_id else []))
                    except ProviderError as e:
                        errors.update({m: str(e) for m in ids})
                after = self._fetch([mid for mid, _ in pending]) if pending else {}
                confirmed = failed = 0
                with self.db.tx() as c:
                    for mid, action in missing:
                        failed += 1
                        self._audit(c, run_id, run["plan_id"], mid, f"restore_{action}", None, None, "failed",
                                    "message no longer exists (Trash may have been emptied)")
                    for mid, action in pending:
                        a = after.get(mid)
                        good = a is not None and "INBOX" in a.labels and "TRASH" not in a.labels and label_id not in a.labels
                        if good:
                            confirmed += 1
                            c.execute("UPDATE plan_messages SET status='restored' WHERE plan_id=? AND message_id=?",
                                      (run["plan_id"], mid))
                        else:
                            failed += 1
                        self._audit(c, run_id, run["plan_id"], mid, f"restore_{action}", before[mid].labels,
                                    a.labels if a else None, "confirmed" if good else "failed",
                                    None if good else errors.get(mid, "change not visible after the call"))
                    self._sync_local(c, after)
                self._bump(run_id, confirmed=confirmed, failed=failed)
        except ProviderError as e:
            status = "failed"
            self._set(run_id, error=str(e))
        with self.db.connect() as c:
            r = c.execute("SELECT failed FROM runs WHERE id=?", (run_id,)).fetchone()
            if status == "completed" and r["failed"]:
                status = "partial"
            c.execute("UPDATE runs SET status=?, finished_at=?, current_op='finished' WHERE id=?",
                      (status, now_iso(), run_id))
        return status

    # -- first-run cap --------------------------------------------------------
    def unlock_full_runs(self) -> None:
        """Allowed only after a capped run whose changes were all restored and confirmed."""
        with self.db.connect() as c:
            ok = c.execute(
                "SELECT 1 FROM runs e JOIN runs r ON r.source_run_id=e.id AND r.kind='restore' "
                "WHERE e.kind='execute' AND e.confirmed>0 AND r.status='completed' AND r.failed=0 "
                "AND r.confirmed + r.already_done >= e.confirmed").fetchone()
        if not ok:
            raise ExecutionError("run a capped cleanup and restore it successfully before unlocking full runs")
        self.db.set_setting("cleanup.full_runs_unlocked", True)
