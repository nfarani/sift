---
description: Phase gate — run tests, run the safety review, update PROGRESS.md, report honestly
---

Run the gate check for the current phase. The current phase is the latest one PROGRESS.md marks as in progress.

1. Use the `test-runner` subagent to run every test suite. Record the exact pass and fail counts.
2. Use the `safety-reviewer` subagent for a full audit. Record its verdict.
3. Check each gate requirement for this phase in docs/PHASES.md. For each one, state whether it is MET, NOT MET or NEEDS NICK, along with the evidence: a test name, a command output, or a manual check you actually performed.
4. Update PROGRESS.md:
   - Move verified items to Done.
   - List what's next.
   - List known issues, including every FAIL from the safety review.
   - Record the test counts and the date.
5. Give Nick a short report in this order:
   - Gate status: PASSED or NOT PASSED.
   - What works.
   - What doesn't work.
   - What you need from him, if anything.
   - The exact command to start the next phase.

Never mark the gate PASSED if any test fails or the safety review has blocking issues.
