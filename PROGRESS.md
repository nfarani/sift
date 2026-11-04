# PROGRESS

## Current phase
Phase 4 gate **PASSED** on 2026-11-04. Next: `/phase-5` (waits for Nick).

## Phase status
| Phase | Status | Gate |
|---|---|---|
| 1. Mock end-to-end | done | PASSED 2026-11-03 (83/83 tests, safety re-audit: SAFE TO PROCEED) |
| 2. Read-only Gmail | done | PASSED 2026-11-04 (113/113 tests; safety audit SAFE TO PROCEED; 500-message and full scans on Nick's account) |
| 3. Claude classification + NL | done | PASSED 2026-11-04 (189/189 tests; safety audit BLOCKING → fixed in e00d2b8 → re-check SAFE TO PROCEED; live run on Nick's account, 20-sender spot-check OK) |
| 4. First real modifications | done | 2026-11-04: 322/322 tests; safety audit SAFE TO PROCEED (3 FAILs fixed in 8c26df5, re-checked); live 50-message run + restore 50/50 confirmed by re-read and DB audit; Nick checked Gmail: label search returns nothing after restore |
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

## Done (Phase 4)
- Executor must-fixes 1-14 (2d12e52, 8c26df5), each with a test in `test_executor.py` / `test_plans.py` / `test_protection.py`:
  - fresh sent-since-scan check and a 7-day maximum scan age
  - archive skips mail already in Trash
  - restore and unlock keyed on attempted (write-ahead) changes
  - one capped, single-recommendation run until a full restore; the proof resets on disconnect or account switch
  - the idempotency shortcut is audited (`already_done`) and flags newly protected mail
  - final plan states; frozen message lists; plans refused if approved while read-only or built from an older analysis
  - `unread_only` re-checked; writer required in run/restore; writer must be the same account
  - nested label rules; the wipe can't fail with runs present; cap clamped to 1-50; the writer never changes UNREAD
- Cleanup permission: a separate Keychain writer token with gmail.modify. The reader stays gmail.readonly.
  - GmailWriter has no delete/send/draft. Scopes are re-checked after every refresh. Corrupt Keychain entries are survived.
  - Enable/disable cleanup routes. `test_gmail.py`.
- Unsubscribe: exact RFC 8058 one-click parsing.
  - Frozen batches with their own hash-echo approval, plus a separate execute step.
  - SSRF-safe single POST without redirects; append-only log (host only); mailto shown as manual.
  - Senders you've emailed, protect, or have protected mail from are skipped. `test_unsubscribe.py`.
  - Not used on the real account yet.
- UI (f256589, 447ed16): Enable cleanup explanation screen, one-recommendation first-run picker, attempted vs confirmed, restore and unlock, unsubscribe batches, backend refusals shown verbatim.
- Synthetic end-to-end smoke test (API), each step behaving as expected:
  - two-recommendation first run refused
  - capped run 50/50
  - second capped run refused
  - unlock refused before restore
  - restore 50/50, then unlock OK
- **Live on Nick's account (2026-11-04):**
  - Cleanup enabled 14:40.
  - Run #1: "advertising", capped: 50 attempted, 50 confirmed, 0 failed, label `Sift/2026-11-04-run-1`. Inbox 97,517 → 97,467.
  - Run #2 (restore of #1): 50 attempted, 50 confirmed, 0 failed. Inbox back to 97,517.
  - Safety check of the live DB: same 50 ids, all in the plan, none protected, no UNREAD or Trash changes.

## Next
- Nick: click "Unlock full runs" (backend allows it: run 1 fully restored).
- `/phase-5`: full run, polish, demo mode (build PII masking before recording anything for publishing).
- Ask Nick before the first real unsubscribe batch.

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

**Open from the Phase 4 safety review** (none blocking; the reviewer gave SAFE TO PROCEED):
- P4-4: when a refresh comes back with drifted scopes, `fresh()` deletes the stored token but the refreshed object stays usable in memory for the rest of that call. Set `creds.token = None` before raising.
- P4-5: a run message that can never be restored (deleted, or Trash emptied) keeps the cap locked for good. Add an audited "accept as unrecoverable" path.
- P4-6: `_refresh_sent` stops at the first page with a known id; sent mail listed out of date order (scheduled, imported) can be missed. Thread replies are still checked per chunk.
- P4-8: the no-network test guard is autouse only in `test_gmail.py`; move it to `conftest.py` with a socket block. No test touches the network today.
- P4-10: unsubscribe `run_batch` doesn't re-check, at execute time, whether a sender has become someone you've emailed since approval.
- P4-11: on reconnect, `_reattach_writer` revokes cleanup on a transient network error, which can also end the read-only sign-in. Revoke only on an account mismatch.
- P4-13: no test for the `unread_only` re-check.
- P4-14: after a reader token refresh, check by hand that it still reports only gmail.readonly. If not, Sift fails safe but asks to reconnect.
- Disabling cleanup may also sign out read-only access (Google ties tokens to the user's grant for the project); the UI says so.
- Unsubscribe can't verify DKIM over the List-Unsubscribe headers (metadata only); the per-batch approval is the safeguard.

**Other**
- Demo mode (PII masking) not built (Phase 5). It must mask: your own address (header chip, Overview subtitle, Account page, `./start.sh` "Mailbox:" line), sender names and addresses, subjects/snippets, and person names in display names. Build it before recording the Phase 4 run for publishing.
- Mail-action scope: archive and trash only. Label, remove-label and mark-as-read actions not built.
- Unsubscribe: display only (one-click vs manual counts); the flag is a loose check. Phase 4 needs exact RFC 8058 parsing.
- Mock reset keeps old runs and audit rows; restoring an old run after a reset touches regenerated messages with the same ids (mock only).
- No frontend unit tests; UI covered by typecheck/build and a manual headless click-through.
- npm 10 peer-resolution crash; `frontend/.npmrc` sets `legacy-peer-deps=true`.

## Test results
2026-11-04 (Phase 4 gate): backend 322 passed, 0 failed, 0 skipped. Frontend: `tsc --noEmit` clean, `vite build` OK. Synthetic API smoke test passed.
Safety review: SAFE TO PROCEED for the capped run; FAILs (writer account mismatch, unsubscribe of senders with protected mail, unlock surviving a wipe) fixed in 8c26df5; re-check SAFE; final live-DB check SAFE TO PROCEED. Open notes under Known issues.
2026-11-04 (Phase 3 gate): backend 189 passed, 0 failed, 0 skipped. Frontend: `tsc --noEmit` clean, `vite build` OK.
Safety review: first audit BLOCKING (raw connect error text in /api/status; disconnect left Claude labels behind) plus FAILs 3, 4, 6, 7, 13. All fixed in e00d2b8 and a follow-up. Re-check: SAFE TO PROCEED. Remaining findings listed under Known issues.
2026-11-03: backend 83 passed, 0 failed, 0 skipped (`cd backend && .venv/bin/python -m pytest`). Frontend: `tsc --noEmit` clean, `vite build` OK. No frontend unit tests.
Safety review: first audit BLOCKING (B1 trash of re-filed mail, B2 replies after approval, B3 no CSRF/rebinding guard). All three fixed in 6f44f8a with regression tests. Re-audit: SAFE TO PROCEED for Phase 1.
