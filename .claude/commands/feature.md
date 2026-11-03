---
description: Add or change a feature after the core build, staying inside Sift's safety rules
argument-hint: <what you want>
---

Nick wants: $ARGUMENTS

1. Re-read CLAUDE.md. If the request conflicts with a Safety rule or is listed as out of scope, say so plainly and propose the closest compliant version. Do not build it until Nick confirms.
2. Make a short plan (files touched, tests to add). If it touches the executor, scopes, tokens, plans or LLM prompts, flag it as safety-relevant.
3. Implement it, with tests.
4. Run the tests via `test-runner`. If the change is safety-relevant, also run `safety-reviewer`.
5. Update PROGRESS.md and commit with a clear message.
6. Report what changed and how to try it.
