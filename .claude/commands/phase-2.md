---
description: "Phase 2 — Real Gmail, read-only"
---

# Phase 2 — Real Gmail, read-only

Read CLAUDE.md and PROGRESS.md before starting. If PROGRESS.md shows an earlier phase whose gate has not been met, stop and tell Nick.

Set up a task list for this phase. Work autonomously on routine engineering. Stop only at the checkpoints named below or in CLAUDE.md.

Delegate where it helps:
- `gmail-specialist` for Gmail and OAuth work.
- `ui-designer` for screens.
- `test-runner` to run and triage the tests.
- `safety-reviewer` after any change to the executor, scopes, tokens, the plan builder or LLM prompts.
Add `GmailProvider` with `gmail.readonly` only.
- Write `docs/google-setup.md`: create Cloud project, enable Gmail API, consent screen (External, Testing), add me as test user, create Desktop OAuth client, where to put `credentials.json`, how to disconnect and wipe local tokens. Mention the 7-day refresh-token expiry in Testing mode.
- Store tokens per CLAUDE.md. Add a "Disconnect" button that revokes and deletes them.
- Scanner uses batched metadata requests, checkpoints to SQLite, respects quota with backoff. Show estimated scan time from measured throughput.

Then stop and tell me exactly what to set up. After I confirm credentials are in place, run a scan capped at 500 messages and report results. Do not request modify scope.

Gate: a 500-message read-only scan works on my account; then a full read-only scan with timing and error counts.

When the work is done, run `/gate` and then stop. Do not start the next phase.

Additional instructions from Nick (may be empty): $ARGUMENTS
