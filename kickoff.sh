#!/usr/bin/env bash
# Starts the Sift build: checks tools, initialises git, launches Claude Code on Phase 1.
set -euo pipefail
cd "$(dirname "$0")"

ok=true
for t in claude git python3 node npm; do
  if command -v "$t" >/dev/null 2>&1; then printf "  ✓ %s\n" "$t"; else printf "  ✗ %s missing\n" "$t"; ok=false; fi
done
$ok || { echo "Install the missing tools, then re-run ./kickoff.sh"; exit 1; }

if [ ! -d .git ]; then
  git init -q && git add -A && git commit -qm "Sift build kit" && echo "  ✓ git repo initialised"
fi
[ -f .env ] || { cp .env.example .env; echo "  ✓ created .env from template (fill ANTHROPIC_API_KEY before Phase 3)"; }

if grep -q "Not started" PROGRESS.md; then
  echo; echo "Launching Claude Code on Phase 1…"; exec claude "/phase-1"
else
  echo; echo "Build already under way, launching Claude Code with /status…"; exec claude "/status"
fi
