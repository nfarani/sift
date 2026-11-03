# Sift: build kit

This is a Claude Code project kit. It builds **Sift**, a local AI inbox cleaner for Gmail, in five gated phases. You run everything from your terminal. Once the app is built, it lives in this same folder, so you can keep working on it here.

## Requirements
- macOS, with Python 3.12+, Node 20+ and git
- [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) installed (`claude --version`)
- An Anthropic API key, from Phase 3 onwards, for the app's AI classification. Phases 1 and 2 spend no API money.

## Start
```bash
unzip sift-agent.zip && cd sift-agent
./kickoff.sh          # git init + launches Claude Code on Phase 1
```
Or do it manually: `git init`, then run `claude` and type `/phase-1`.

## Running the app
```bash
./start.sh            # checks deps + .env, starts API (127.0.0.1:8765) and UI (127.0.0.1:5173), opens the browser
```
`SIFT_NO_OPEN=1 ./start.sh` skips opening the browser. Logs are in `data/logs/`. Ctrl-C stops both servers.

Until Phase 2 the app runs against a **synthetic mailbox** of ~45,000 fictional messages (`data/mock_mailbox.sqlite`),
badged "Synthetic data" everywhere. Classification is **rules only, no AI** until Phase 3.

Tests: `cd backend && .venv/bin/python -m pytest` · frontend typecheck: `cd frontend && npm run typecheck`.

### Layout
```
backend/sift/
  providers/base.py     MailProvider interface (read methods + executor-only write methods; no delete/send)
  providers/mock.py     MockProvider (SQLite-backed, Gmail-shaped, fault injection)
  providers/synthetic.py  deterministic 45k fictional mailbox incl. prompt-injection subjects
  scanner.py            metadata-only paginated scan, checkpoints, cancel/resume
  protection.py         deterministic protection rules (run before any classifier)
  analysis.py           aggregation → protection → classification → recommendations
  classifier/           Classifier interface + RuleOnlyClassifier
  plans.py              frozen plans (exact ids + hash, DB triggers block changes)
  executor.py           the only module that modifies mail: revalidate, batch, confirm, audit, restore
  api.py                FastAPI routes the UI reads from
frontend/src/           React + TS + Vite UI
```

## Commands, from inside Claude Code
| Command | What it does |
|---|---|
| `/phase-1` … `/phase-5` | Builds the next phase. Each phase stops at its gate. |
| `/gate` | Runs the tests and the safety audit, updates PROGRESS.md and reports pass or fail. |
| `/status` | Shows where things stand and what to run next. |
| `/feature <idea>` | Adds features after the core build, within the safety rules. |

Any phase command also accepts extra notes, for example `/phase-1 use a muted green accent`.

## The phases
1. **Mock end-to-end.** Builds the full app against 45,000 synthetic emails. Uses no Google account and costs nothing.
2. **Read-only Gmail.** Claude will stop and walk you through the Google Cloud setup.
3. **Claude classification and natural-language requests.** Claude shows you the estimated cost and waits for your OK before spending anything.
4. **First real changes.** The first run is capped at 50 messages, and restoring them is verified before larger runs are allowed.
5. **Full run, polish and demo mode** for the LinkedIn recording.

## What's in the kit
```
CLAUDE.md                    standing rules: architecture, safety, Gmail facts
docs/SPEC.md                 your original full spec
docs/PHASES.md               the five phases with their gates
PROGRESS.md                  living progress log (Claude updates it)
.claude/commands/            /phase-1..5, /gate, /status, /feature
.claude/agents/              safety-reviewer, test-runner, gmail-specialist, ui-designer
.claude/hooks/guard.py       hard guardrail, explained below
.claude/settings.json        permissions and hook wiring
.env.example                 config template (copy it to .env)
```

## The guardrail hook
Before every shell command or file edit Claude makes, `guard.py` checks it. It **blocks**:
- App code that adds the full-access Gmail scope, send or compose scopes, permanent delete, drafts, or emptying Trash.
- Shell commands that read OAuth tokens or `.env` secrets, call Gmail directly, force-push, or recursively delete your home folder.

These rules are enforced in code, not just requested in a prompt. Docs and tests are exempt from the code scan, because they need to mention these things in order to forbid them.

## Your checkpoints
Claude will stop and ask you before it does any of these:
- Connects your real Gmail account.
- Spends API money above the cap in `.env`.
- Makes the first real change to your mail.
- Sends any unsubscribe request.
