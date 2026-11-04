# Sift

Sift is a local, terminal-launched app that cleans up a Gmail inbox you can no longer manage by hand. It scans
message metadata, protects anything that looks like real correspondence or a record you'd need, uses Claude to
classify the bulk senders that rules can't settle, and proposes explainable cleanup plans. It changes mail only
after you approve a frozen plan, and every change is labelled and reversible.

It runs on your Mac and talks only to Google (Gmail API) and Anthropic (classification). There is no server.

How it's built: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Progress and known gaps: [PROGRESS.md](PROGRESS.md).

## Requirements
- macOS, with Python 3.12+, Node 20.19+ and git
- A Google Cloud OAuth client for Gmail (setup: [docs/google-setup.md](docs/google-setup.md))
- An Anthropic API key for classification (optional: without it Sift runs rules-only and says so)

## Run it
```bash
cp .env.example .env      # add ANTHROPIC_API_KEY, optionally SIFT_AI_SPEND_CAP_USD (per Claude run)
./start.sh --gmail        # your Gmail; ./start.sh alone uses a synthetic mailbox
```
`./start.sh` checks dependencies, starts the API (127.0.0.1:8765) and the UI (127.0.0.1:5173) and opens the browser.
`SIFT_NO_OPEN=1` skips the browser. Logs are in `data/logs/`. Ctrl-C stops both. Set `SIFT_PROVIDER=gmail` in
`.env` to make Gmail the default. The synthetic mailbox and Gmail never share a database.

## Using it
1. **Connect Gmail** (read-only, `gmail.readonly`). Scanning reads metadata only and never marks anything read.
2. **Scan.** Paginated, checkpointed, resumable. About 4–5 messages a second on a personal account
   (Nick's 99k messages took about 6 hours).
3. **Classify with Claude** (optional). Sift shows the token and dollar estimate first and spends nothing until you
   approve it. Each run has a hard cap (`SIFT_AI_SPEND_CAP_USD`, default $2). Only unsettled sender groups are sent:
   sender, list id, counts, up to 6 subjects and 3 short previews per sender, never bodies or attachments, never
   protected mail.
4. **Review recommendations.** Each one shows why, which senders, examples, and how many protected messages it
   leaves out. Exclude senders, set an age cutoff, or ask in plain English ("old shopping promos but keep receipts");
   Claude turns that into a filter you can see and edit. It never creates or approves a plan.
5. **Enable cleanup.** A second, separate Google consent (`gmail.modify`). It changes nothing by itself.
6. **Approve a plan.** The exact message ids are frozen at approval. The first run is capped at 50 messages from one
   recommendation; you restore it to prove recovery works before full runs unlock.
7. **Run, watch, cancel, restore.** Live progress, cancel at any time, and a before/after report.

## What Sift will and won't do
- **Default action is archive + label** (`Sift/2026-11-04-run-12`): fully reversible. Trash is optional and the
  confirmation says Gmail empties Trash after about 30 days, so restore is only guaranteed within that window.
- **Never:** permanent delete, emptying Trash, sending, replying, forwarding, drafts, following links, opening
  attachments.
- **Protection runs first and wins:** starred mail, threads you replied to, senders you've emailed, receipts, orders,
  bookings, travel, security and 2FA, bank and finance mail (English and Portuguese patterns), and your own rules.
  Claude's output can't unprotect anything.
- **Revalidation at run time:** every message is re-read just before it changes, and dropped if it's gone, moved,
  newly protected (including mail you sent since the scan) or no longer matches. Revalidation can remove ids from a
  plan, never add.
- **Audit log:** append-only, one row before each change (write-ahead) and one after it's confirmed by re-reading.
  The UI always shows "attempted" and "confirmed" separately.
- **Unsubscribe** is separate: RFC 8058 one-click only, its own approval per batch, a single HTTPS POST per sender
  with no redirects, logged with the host and status. `mailto:` unsubscribes are listed as manual and never sent.
  Senders you've emailed or that have protected mail are skipped.
- **Prompt injection:** email content is untrusted data, wrapped in an escaped block, and Claude has no tools. Its
  output is validated against a strict schema; anything else is rejected and the sender keeps its rules label.

### Gmail scopes and tokens
| Token (macOS Keychain service) | Scope | Created when | Used by |
|---|---|---|---|
| `sift-gmail` | `gmail.readonly` | **Connect Gmail** | scanner, analysis, executor revalidation |
| `sift-gmail-writer` | `gmail.modify` (+ `gmail.readonly`) | **Enable cleanup** | the executor only |

- Sift never requests the full-access Gmail scope or any send/compose scope. A token with anything else is refused
  and revoked. Scopes are re-checked after every refresh.
- Google's consent screen describes `gmail.modify` as "Read, compose, and send emails". Sift has no code that sends
  or drafts mail, and the guardrail hook blocks adding any.
- **Disable cleanup** revokes and deletes the writer token. Google ties both tokens to one grant, so this may end the
  read-only sign-in too; Sift then shows **Reconnect Gmail**.
- **Testing mode:** Google expires refresh tokens after 7 days. Sift shows **Reconnect Gmail** or **Reconnect
  cleanup** instead of failing.
- **Disconnect** revokes both tokens at Google, deletes them from the Keychain, and wipes scanned metadata, plans and
  Claude labels. The audit log and the spend totals (tokens and dollars only) are kept.

## Demo mode (for screen recordings)
Turn it on with the switch in the top bar (or `SIFT_DEMO_MODE=true`). The backend then masks every API response
before it reaches the browser:
- email addresses → `n***a1b2@mail.example.com` (domain kept)
- display names → `Sender #4821`
- subjects and previews → hidden
- your own name and address, and digit runs of 6+ (order numbers, codes) → masked

Claude's per-sender reasons are kept (they explain the recommendation) with addresses and person names masked;
they can still name a brand. Your own label names can appear in run skip reasons.

Actions keep working: the backend maps masked addresses back to the real ones on requests, and refuses any masked
address it can't map (for example from a page left open after demo mode was switched off). A badge shows demo mode
is on. `./start.sh` also prints the masked address when demo mode is on. Google's own consent screen is outside
Sift and is not masked.

## Tests
```bash
cd backend && .venv/bin/python -m pytest      # backend
cd frontend && npx tsc --noEmit && npx vite build
```
Latest results are in [PROGRESS.md](PROGRESS.md#test-results). The backend tests run offline against the synthetic
mailbox and fake Gmail/Anthropic clients; they never read your real `.env` key.

## Layout
```
backend/sift/
  api.py                FastAPI routes; local-only Host/Origin/X-Sift guard; demo-mode middleware
  config.py             settings from .env
  providers/base.py     MailReader / MailWriter interfaces (no delete, no send)
  providers/mock.py     synthetic Gmail-shaped mailbox (providers/synthetic.py generates ~45k messages)
  providers/gmail.py    GmailReader (gmail.readonly, metadata only, batched, quota-limited)
  providers/gmail_writer.py  GmailWriter (label, batchModify, trash/untrash; used by the executor only)
  providers/gmail_auth.py    OAuth loopback + PKCE, separate reader/writer Keychain tokens, scope checks
  scanner.py            metadata-only paginated scan, checkpoints, cancel/resume
  protection.py         deterministic protection rules (run before any classifier)
  analysis.py           aggregate by sender → protect → classify → recommendations
  classifier/           RuleOnlyClassifier, ClaudeClassifier (strict JSON schema)
  ai/                   spend ledger + cap, Claude run orchestration, natural-language query planner
  query.py              the structured filter spec the planner produces
  plans.py              frozen plans: exact ids + hash, DB triggers block changes
  executor.py           the only module that modifies mail: revalidate, batch, confirm, audit, restore
  unsubscribe.py        RFC 8058 parsing, frozen batches, SSRF-safe POST, append-only log
  demo.py               PII masking for demo mode
frontend/src/           React + TypeScript + Vite
docs/                   SPEC, PHASES, ARCHITECTURE, google-setup
```

## Building it with Claude Code
This repo is also the kit Sift was built with, in five gated phases (`docs/PHASES.md`).

| Command | What it does |
|---|---|
| `/phase-1` … `/phase-5` | Builds the next phase. Each phase stops at its gate. |
| `/gate` | Runs the tests and the safety audit, updates PROGRESS.md, reports pass or fail. |
| `/status` | Where things stand and what to run next. |
| `/feature <idea>` | Adds a feature after the core build, within the safety rules. |

`.claude/hooks/guard.py` checks every shell command and file edit Claude makes. It blocks app code that adds the
full-access Gmail scope, send/compose scopes, permanent delete, drafts or emptying Trash, and shell commands that
read OAuth tokens or `.env` secrets, call Gmail directly, force-push, or recursively delete your home folder. Docs
and tests are exempt from the code scan so they can name what they forbid.

Claude stops and asks before it connects a real account, spends above the cap, makes the first real change to your
mail, or sends any unsubscribe request.
