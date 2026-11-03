# PROGRESS

## Current phase
Phase 2 in progress (code done, commit 2a8a70d, 104 tests pass). Waiting on:
1. Safety review of Phase 2: first pass BLOCKING (quota limiter hang, OAuth code in log), fixed. Re-check: SAFE TO CONNECT (read-only); its 4 follow-ups fixed too (113 pass).
2. Nick: Google Cloud setup per docs/google-setup.md.
3. Then: 500-message test scan, full read-only scan with timing and error counts, /gate.

## Phase status
| Phase | Status | Gate |
|---|---|---|
| 1. Mock end-to-end | done | PASSED 2026-11-03 (83/83 tests, safety re-audit: SAFE TO PROCEED) |
| 2. Read-only Gmail | in progress | — |
| 3. Claude classification + NL | not started | — |
| 4. First real modifications | not started | — |
| 5. Full run, polish, demo | not started | — |

## Done (Phase 1, each verified by a test or a manual check)
- `./start.sh`: checks Python 3.12+/Node 20.19+, creates `.env`, installs deps, starts API (127.0.0.1:8765) + UI (127.0.0.1:5173), prints URL and config status. Manually run.
- `MailProvider` interface; `MockProvider` over a deterministic ~45.5k synthetic mailbox (job alerts, promos with receipts/bookings mixed in, newsletters, social, notifications, security, bank, personal threads with SENT replies, 6 prompt-injection subjects). `test_pagination.py`.
- Scanner: metadata only, paginated, checkpoints after each page, cancel/resume, retry with backoff, estimate = Inbox + Sent label totals. `test_scan_resume.py`, `test_api.py::test_scan_cancel_and_resume_via_api`. Full 45k scan manually: 42,147 messages, 0 errors, about 27s.
- Protection rules run before classification: starred, thread with your reply, sender you've emailed, receipt/travel/security/finance patterns, user rules. IMPORTANT and List-Unsubscribe are not decisive. `test_protection.py`, `test_injection.py`.
- `RuleOnlyClassifier`, labelled "Rules only, no AI" in the UI and terminal.
- Plan builder: exact frozen ids, hash echo needed to approve, DB triggers freeze rows/fields/status, executor re-verifies the hash. `test_plans.py`.
- Executor (sole module making write calls):
  - fresh revalidation per chunk (exists, still in Inbox, no new reply in the thread, not newly protected, filter still matches); removes ids, never adds
  - batches of 500 or fewer
  - write-ahead audit, confirm by re-read, attempted vs confirmed counts
  - cancel; idempotent re-run
  - first-run cap of 50, unlocked only by a verified restore
  - restore (archive and trash)

  `test_executor.py`.
- Local-only API: Host allowlist; X-Sift header + Origin check on state changes. `test_api.py::test_cross_site_and_rebinding_requests_rejected`, manual curl probes return 403.
- UI: overview, scan (live state, cancel, resume), recommendations (filters by category/action/sender/age, per-sender exclusions, always-protect, examples, protected exceptions), confirmation (exact counts, protection exclusions, cap and Trash warnings), run progress, results (before/after, attempted vs confirmed), restore, unlock, run history. Manual click-through in headless Chrome: scan → select → exclude sender → confirm → 50-message run → restore → unlock → remaining 15,411 run.

## Next
- Nick: click through http://127.0.0.1:5173 (`./start.sh`) to confirm the gate's manual check.
- `/phase-2`: GmailProvider (read-only). Must do first: S1, S6 below.

## Known issues
**Fixed in Phase 2 (2a8a70d, not yet safety-reviewed)**
- S1: MailReader and MailWriter are now split; in Gmail mode no writer exists at all.
- S6: label rules now resolve names to label IDs; Cc/Bcc recipients now count as people you've emailed.

**Must fix before Phase 3**
- S7: classifier output is coerced to `uncertain` on unknown categories. It needs strict schema validation that rejects, plus confidence bounds.

**Must fix before Phase 4 (first real modification)**, from the safety re-audit, most severe first:
1. "Sender you've emailed" isn't re-checked at run time (only threads are). There's also no maximum scan age. Add a fresh sent-since-scan check or refuse stale scans.
2. Archive action lets messages already in Trash through revalidation (`executor.py` `_revalidate`: the TRASH exemption should apply to trash actions only). It labels them and reports them as confirmed.
3. Changes from a crashed or unconfirmed run can't be restored from the UI (Restore button needs `confirmed > 0`). Key on `attempted` audit rows; review `unlock_full_runs` too.
4. The first-run cap applies per run; repeated capped runs need no unlock. Decide: one capped run until unlock.
5. The idempotency shortcut (own run label already applied) writes no audit row and doesn't flag a now-protected message.
6. DB hardening: block cancelled/completed → approved, and DELETE on `plan_messages` once a plan leaves draft.
7. (Phase 2 review P4-b) Plans approved while read-only stay approved; require approval after cleanup is enabled.
8. (P4-c) Once gmail.modify is granted, the reader's token can modify too. Use a separate writer token, or record this as an accepted deviation; re-check scopes after refresh.
9. (P4-d) Label rules resolve through labels.map from the last scan (can be stale); nested labels ("Work/Clients") aren't matched by "Work".
10. (P4-e) Check that disconnect can't fail midway once runs exist (it currently wipes plans).
Done early: (P4-a) a capped test scan can't back a cleanup run (`test_capped_test_scan_cannot_drive_cleanup`).

**Other**
- Demo mode (PII masking) not built (Phase 5).
- Mail-action scope: archive and trash only. Label, remove-label and mark-as-read actions not built.
- Unsubscribe: display only (one-click vs manual counts); the flag is a loose check. Phase 4 needs exact RFC 8058 parsing.
- Mock reset keeps old runs and audit rows; restoring an old run after a reset touches regenerated messages with the same ids (mock only).
- No frontend unit tests; UI covered by typecheck/build and a manual headless click-through.
- npm 10 peer-resolution crash; `frontend/.npmrc` sets `legacy-peer-deps=true`.
- Rule classifier puts "Food for Thought" (a recipe newsletter in Gmail's Promotions tab) under promotions. Expected rules-only imprecision; revisit with Claude in Phase 3.

## Test results
2026-11-03: backend 83 passed, 0 failed, 0 skipped (`cd backend && .venv/bin/python -m pytest`). Frontend: `tsc --noEmit` clean, `vite build` OK. No frontend unit tests.
Safety review: first audit BLOCKING (B1 trash of re-filed mail, B2 replies after approval, B3 no CSRF/rebinding guard). All three fixed in 6f44f8a with regression tests. Re-audit: SAFE TO PROCEED for Phase 1.
