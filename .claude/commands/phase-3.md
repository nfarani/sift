---
description: "Phase 3 — Claude classification + natural-language requests"
---

# Phase 3 — Claude classification + natural-language requests

Read CLAUDE.md and PROGRESS.md before starting. If PROGRESS.md shows an earlier phase whose gate has not been met, stop and tell Nick.

Set up a task list for this phase. Work autonomously on routine engineering. Stop only at the checkpoints named below or in CLAUDE.md.

Delegate where it helps:
- `gmail-specialist` for Gmail and OAuth work.
- `ui-designer` for screens.
- `test-runner` to run and triage the tests.
- `safety-reviewer` after any change to the executor, scopes, tokens, the plan builder or LLM prompts.
- Implement `ClaudeClassifier` per CLAUDE.md: only for sender groups not settled by rules, strict JSON schema, confidence scores, reasons, per-message exceptions (e.g. airline promos vs flight confirmations from the same domain). Cheap model first, stronger model for confidence < threshold.
- Prompt-injection hardening: delimited data blocks, schema validation, tests using the injection fixtures from Phase 1.
- NL query planner: my sentence → structured filter spec shown in the UI → match count + sample messages → becomes a recommendation I can edit. It can never create or approve a plan on its own.
- Show tokens used and estimated cost per scan; respect the `.env` spend cap.

Before the first run on my real data, tell me the estimated token count and cost and wait for my OK.

Gate: recommendations on my real inbox look sensible; I spot-check 20 groups and report misclassifications for you to fix.

When the work is done, run `/gate` and then stop. Do not start the next phase.

Additional instructions from Nick (may be empty): $ARGUMENTS
