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

`./start.sh --gmail` connects your real Gmail **read-only** (setup: `docs/google-setup.md`); `./start.sh --mock` or the default uses the synthetic mailbox. The two never share a database.

### Gmail scopes and tokens
| Token (macOS Keychain service) | Scope | Created when | Used by |
|---|---|---|---|
| `sift-gmail` | `gmail.readonly` | **Connect Gmail** | scanner, analysis, executor revalidation |
| `sift-gmail-writer` | `gmail.modify` (+ `gmail.readonly`) | **Enable cleanup**, a separate consent you trigger | the executor only |

- Sift never requests the full-access Gmail scope or any send/compose scope. A token carrying anything else is refused and revoked. Scopes are re-checked after every token refresh.
- `gmail.modify` lets Sift archive, label and move to Trash. It can't permanently delete. Google's consent screen describes it as "Read, compose, and send emails". Sift has no code that sends or drafts mail, and the guardrail hook blocks adding any.
- Enabling cleanup changes nothing by itself. Every change still needs an approved plan, and plans approved before cleanup was enabled have to be approved again.
- **Disable cleanup** revokes and deletes only the writer token. Google ties both Sift tokens to one grant, so revoking may end the read-only sign-in too. If that happens, Sift shows **Reconnect Gmail**.
- **Testing mode:** Google expires refresh tokens after 7 days. When the read-only token expires, Sift shows **Reconnect Gmail**. When the cleanup token expires, it shows **Reconnect cleanup**. Neither crashes the app.
- An unreadable Keychain entry is treated as missing: Sift deletes it and tells you.
- **Unsubscribe** needs cleanup enabled plus its own approval for each batch. Only RFC 8058 one-click is used: a single HTTPS POST, no redirects followed, no cookies, and nothing sent to private or local addresses. `mailto:` unsubscribes are shown as manual and never sent. Every attempt is logged with the sender, URL host and status code.

By default the app runs against a **synthetic mailbox** of ~45,000 fictional messages (`data/mock_mailbox.sqlite`),
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
  providers/gmail.py    GmailReader (gmail.readonly, metadata only)
  providers/gmail_writer.py  GmailWriter (gmail.modify: label, batchModify, trash/untrash; executor only)
  providers/gmail_auth.py    OAuth loopback flow, separate reader/writer Keychain tokens, scope checks
  unsubscribe.py        RFC 8058 one-click parsing, frozen batches, SSRF-safe single POST, append-only log
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
