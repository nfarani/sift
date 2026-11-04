# PROGRESS

## Current phase
Phase 3 gate **PASSED** on 2026-11-04. Next: `/phase-4` (waits for Nick; fix the Phase 4 must-fix list first).

## Phase status
| Phase | Status | Gate |
|---|---|---|
| 1. Mock end-to-end | done | PASSED 2026-11-03 (83/83 tests, safety re-audit: SAFE TO PROCEED) |
| 2. Read-only Gmail | done | PASSED 2026-11-04 (113/113 tests; safety audit SAFE TO PROCEED; 500-message and full scans on Nick's account) |
| 3. Claude classification + NL | done | PASSED 2026-11-04 (189/189 tests; safety audit BLOCKING → fixed in e00d2b8 → re-check SAFE TO PROCEED; live run on Nick's account, 20-sender spot-check OK) |
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

## Done (Phase 2)
- GmailReader with `gmail.readonly` only. Tokens with extra scopes are refused and revoked. Metadata/minimal reads only. Batches of at most 50. Quota limiter: 5,400 units/min, 600 burst. Rate-limited sub-requests retried alone. `test_gmail.py`.
- OAuth: loopback 127.0.0.1, random port, PKCE. Token in macOS Keychain (`sift-gmail`). OAuth library logs silenced. Reconnect hygiene (wipe on account change, revoke on account switch only). Expired sign-in → "Reconnect".
- Read/write split: no MailWriter exists in Gmail mode. Executor and Restore refuse.
- Disconnect: revokes at Google, deletes the Keychain token, wipes scanned metadata, keeps the audit log.
- Separate DBs: `data/sift.sqlite` (synthetic), `data/sift-gmail.sqlite` (real). `data/` is 0700 with files 0600 (start.sh umask).
- Capped test scans; measured throughput and ETA; label-name protection rules; Cc/Bcc count as people you've emailed.
- `docs/google-setup.md` (Portuguese console users: append `?hl=en`).
- Live results on Nick's account:
  - 500-message test scan: 1,000 messages (500 Inbox + 500 Sent) in 4m00s, 0 errors.
  - Full read-only scan: 98,981 messages (97,517 Inbox + 1,464 Sent) in 6h16m (4.4 msg/s), 0 errors, 2,940 senders, 5,200 protected.
  - Rules-only classification leaves 50,296 in "Needs your review".

## Done (Phase 3)
- ClaudeClassifier: unsettled sender groups only; Haiku 4.5 first pass, Opus 5.5 second opinion below 0.7 confidence. Strict JSON schema + client validation that rejects (never coerces); rejected batches keep their rules label. `test_claude.py`.
- Prompt-injection hardening: escaped `<email_data>` JSON block, untrusted-data system prompt, no tools, protected subjects never sent. `test_claude.py`, `test_injection.py`.
- Spend ledger and per-run hard cap (worst-case reservation per request, dearest price for fallbacks). The run must match the estimate the user approved. Within-cap runs cover only the groups the estimate covered, so each gets both passes (`test_within_cap_run_covers_only_estimated_groups_with_both_passes`).
- NL planner: sentence → validated filter spec → local preview → editable recommendation; it can't create or approve plans.
- Protection additions (from the Phase 3 spot-check): trip/order receipts ("Your Sunday trip with Uber", "…order with Uber Eats") and Portuguese receipt/travel/security/finance terms. On the real scan this newly protects 661 messages (Uber/Free Now receipts, ~100 Google "Alerta de segurança"/"Novo login"), with ~5 harmless false positives (promos kept). Stored flags are re-derived at startup when the rule set changes. `test_protection.py`, `test_startup_reapplies_protection_when_builtin_rules_changed`.
- Gate fixes (e00d2b8): unknown connect errors show a fixed message; disconnect/account switch also clears Claude labels and runs (spend totals kept, detail text dropped); startup refresh uses the cached address and can't crash startup; interrupted AI runs marked failed on restart; planner reserves worst case; strict keep-exception reason; run screen counts senders, not batches. Tests in `test_api.py`.
- Tests no longer read the real ANTHROPIC_API_KEY from `.env` (`conftest.py`).
- Live results on Nick's account (2026-11-04):
  - Run 1 cancelled by me after 80 senders ($0.09): it was sweeping all 2,536 senders instead of the 655 the estimate covered (fixed, ee7f9a9).
  - Run 2: 657 senders, completed, $0.54. Total spend $0.63. 677 senders labelled by Claude. "Needs your review" went from 50,296 to 7,184 messages.
  - 4 batches (80 senders) rejected, all for "keep_exception phrase must be 4-60 characters". Only 2 senders got the Opus second opinion.
  - Nick spot-checked the 20 biggest senders: "pretty much correct", no misclassifications reported.

## Next
- Nick: restart with `./start.sh --gmail` so the new protection rules apply to the stored scan.
- `/phase-4`: first real modifications. Fix the "Must fix before Phase 4" list below first, starting with items 1-5 and 14.

## Known issues
**Open from the Phase 3 safety review** (not blocking for Phase 3; the executor-related ones block Phase 4):
- P3-5: failed Anthropic calls aren't recorded in the ledger, and the SDK retries up to 3 times, so the cap and "Usage to date" can understate spend after timeouts/5xx. Fix: `max_retries=0` with a ledger check before each own retry, or record a reservation-sized row on exception.
- P3-9: one too-short keep_exception phrase throws away a whole batch of 20. Reject per group instead (still never coerce), and show the stored reason in the UI.
- P3-10: second opinions almost never happen (Haiku reports ≥0.7 on nearly everything). Also send the biggest disposable-labelled groups to the review model.
- P3-11: the spend cap is per run, not cumulative. Say "per run" in the UI and README, and optionally add a total cap.
- P3-12: the input-token reservation is a chars/3.5 estimate; non-Latin text can overshoot by cents. Use `count_tokens`.
- P3-14: the Ask panel keeps showing Claude's explanation after the filter fields are edited.
- P3-15: rule add/delete runs `_reanalyse` synchronously and can race a newly submitted job. Use a single analysis lock.
- P3-16: tests still load the other `.env` values (`SIFT_*`); also delenv them in the autouse fixture.
- `providers/gmail.py:85` passes unknown exceptions through unchanged (they no longer reach the UI as text).
- `apply_protection` records the rules version before aggregate/classify finish. If startup fails in that window, it won't retry, and sender groups can be half-rebuilt. Mail is still safe because the executor re-checks protection.

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
11. Executor.run()/restore() should call `_require_writer()` themselves (defence in depth).
12. Disconnect/startup should survive a corrupt Keychain entry (load raises before delete).
13. Exact RFC 8058 parsing for one-click unsubscribe before building unsubscribe.
14. (Phase 3 review) Executor doesn't recheck the local category or `unread_only` against the plan item. A Claude run or reclassification between approval and execution doesn't stop the plan. Fix: revalidate category/UNREAD, or block execution when an AI run finished after approval.
Done early: (P4-a) a capped test scan can't back a cleanup run (`test_capped_test_scan_cannot_drive_cleanup`).

**Other**
- Demo mode (PII masking) not built (Phase 5). It must mask: your own address (header chip, Overview subtitle, Account page, `./start.sh` "Mailbox:" line), sender names and addresses, subjects/snippets, and person names in display names. Build it before recording the Phase 4 run for publishing.
- Mail-action scope: archive and trash only. Label, remove-label and mark-as-read actions not built.
- Unsubscribe: display only (one-click vs manual counts); the flag is a loose check. Phase 4 needs exact RFC 8058 parsing.
- Mock reset keeps old runs and audit rows; restoring an old run after a reset touches regenerated messages with the same ids (mock only).
- No frontend unit tests; UI covered by typecheck/build and a manual headless click-through.
- npm 10 peer-resolution crash; `frontend/.npmrc` sets `legacy-peer-deps=true`.

## Test results
2026-11-04 (Phase 3 gate): backend 189 passed, 0 failed, 0 skipped. Frontend: `tsc --noEmit` clean, `vite build` OK.
Safety review: first audit BLOCKING (raw connect error text in /api/status; disconnect left Claude labels behind) plus FAILs 3, 4, 6, 7, 13. All fixed in e00d2b8 and a follow-up. Re-check: SAFE TO PROCEED. Remaining findings listed under Known issues.
2026-11-03: backend 83 passed, 0 failed, 0 skipped (`cd backend && .venv/bin/python -m pytest`). Frontend: `tsc --noEmit` clean, `vite build` OK. No frontend unit tests.
Safety review: first audit BLOCKING (B1 trash of re-filed mail, B2 replies after approval, B3 no CSRF/rebinding guard). All three fixed in 6f44f8a with regression tests. Re-audit: SAFE TO PROCEED for Phase 1.
