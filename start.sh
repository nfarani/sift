#!/usr/bin/env bash
# Sift launcher: checks dependencies and config, starts backend + frontend, prints the URL.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
die()  { printf "  \033[31m✗\033[0m %s\n" "$*"; exit 1; }

# Optional: ./start.sh --gmail | --mock overrides SIFT_PROVIDER from .env for this launch.
for arg in "$@"; do
  case "$arg" in
    --gmail) export SIFT_PROVIDER=gmail ;;
    --mock)  export SIFT_PROVIDER=mock ;;
    *) echo "unknown option $arg (use --gmail or --mock)"; exit 2 ;;
  esac
done

bold "Sift: checking your setup"

# --- Python 3.12+ --------------------------------------------------------------
PY=""
for c in python3.13 python3.12 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
[ -n "$PY" ] || die "Python 3.12+ not found (brew install python@3.13)"
ok "Python $("$PY" -c 'import platform; print(platform.python_version())')"

# --- Node 20.19+ ---------------------------------------------------------------
command -v node >/dev/null 2>&1 || die "Node.js not found (install Node 20.19+ or 22+)"
node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a>22||(a===22&&b>=12)||(a===20&&b>=19)||a===21?0:1)' \
  || die "Node $(node -v) is too old for Vite; need 20.19+ or 22.12+"
ok "Node $(node -v)"
command -v npm >/dev/null 2>&1 || die "npm not found"

# --- .env ----------------------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  ok "Created .env from .env.example"
else
  ok ".env present"
fi
# Load ports and flags only; secrets stay in the backend process.
API_PORT="$("$PY" - <<'EOF'
import re, pathlib
v = "8765"
for line in pathlib.Path(".env").read_text().splitlines():
    m = re.match(r"\s*SIFT_API_PORT\s*=\s*(\d+)", line)
    if m: v = m.group(1)
print(v)
EOF
)"
UI_PORT="$("$PY" - <<'EOF'
import re, pathlib
v = "5173"
for line in pathlib.Path(".env").read_text().splitlines():
    m = re.match(r"\s*SIFT_UI_PORT\s*=\s*(\d+)", line)
    if m: v = m.group(1)
print(v)
EOF
)"
export SIFT_API_PORT="$API_PORT" SIFT_UI_PORT="$UI_PORT"

# --- backend deps --------------------------------------------------------------
VENV="$ROOT/backend/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  "$PY" -m venv "$VENV"
  ok "Created backend virtualenv"
fi
REQ_HASH="$(shasum backend/requirements.txt | cut -d' ' -f1)"
if [ ! -f "$VENV/.req-hash" ] || [ "$(cat "$VENV/.req-hash")" != "$REQ_HASH" ]; then
  "$VENV/bin/pip" install -q --upgrade pip >/dev/null
  "$VENV/bin/pip" install -q -r backend/requirements.txt || die "pip install failed"
  echo "$REQ_HASH" > "$VENV/.req-hash"
fi
ok "Backend dependencies"

# --- frontend deps -------------------------------------------------------------
if [ ! -d frontend/node_modules ] || [ frontend/package.json -nt frontend/node_modules ]; then
  (cd frontend && npm install --no-fund --no-audit --silent) || die "npm install failed"
  touch frontend/node_modules
fi
ok "Frontend dependencies"

# --- ports ---------------------------------------------------------------------
for p in "$API_PORT" "$UI_PORT"; do
  if lsof -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
    die "Port $p is already in use (is Sift already running?). Change SIFT_API_PORT/SIFT_UI_PORT in .env."
  fi
done

# --- start ---------------------------------------------------------------------
# Local data (scanned metadata, logs) is private to your user account.
umask 077
mkdir -p data
chmod 700 data
find data -type f -exec chmod 600 {} + 2>/dev/null || true
LOG_DIR="$ROOT/data/logs"; mkdir -p "$LOG_DIR"
PIDS=()
cleanup() {
  echo; bold "Stopping Sift…"
  for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

(cd backend && exec "$VENV/bin/python" -m uvicorn sift.api:default_app --factory \
   --host 127.0.0.1 --port "$API_PORT" --log-level warning) > "$LOG_DIR/backend.log" 2>&1 &
PIDS+=($!)

for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:$API_PORT/api/status" >/dev/null 2>&1 && break
  sleep 0.25
done
STATUS="$(curl -fsS "http://127.0.0.1:$API_PORT/api/status" 2>/dev/null)" || {
  tail -20 "$LOG_DIR/backend.log"; die "Backend did not start (log: data/logs/backend.log)"; }
ok "Backend on http://127.0.0.1:$API_PORT"

(cd frontend && exec npx vite --port "$UI_PORT" --strictPort --logLevel warn) > "$LOG_DIR/frontend.log" 2>&1 &
PIDS+=($!)
for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:$UI_PORT/" >/dev/null 2>&1 && break
  sleep 0.25
done
curl -fsS "http://127.0.0.1:$UI_PORT/" >/dev/null 2>&1 || { tail -20 "$LOG_DIR/frontend.log"; die "Frontend did not start"; }
ok "Frontend on http://127.0.0.1:$UI_PORT"

echo
bold "Configuration"
STATUS_JSON="$STATUS" "$PY" - <<'EOF'
import json, os
s = json.loads(os.environ["STATUS_JSON"])
if s["synthetic"]:
    mailbox = "SYNTHETIC mock mailbox (no real account)"
else:
    conn = s["connection"]["state"]
    mailbox = f"Gmail: {s['account']}" if conn == "connected" and s["account"] else f"Gmail: {conn.replace('_', ' ')}"
    if s.get("client_secrets_present") is False:
        mailbox += " (OAuth client file missing, see docs/google-setup.md)"
cap = "unlocked" if s["full_runs_unlocked"] else "locked until a run + restore are verified"
print(f"  Mailbox:       {mailbox}")
print(f"  Access:        {s['mode']}")
print(f"  Classifier:    {s['classifier']['label']}")
print(f"  Anthropic key: {s['anthropic_api_key']} (used for Claude classification)")
print(f"  First-run cap: {s['first_run_cap']} messages ({cap})")
EOF
echo
bold "Sift is running →  http://127.0.0.1:$UI_PORT"
echo "  Logs: data/logs/   ·   Ctrl-C to stop"
if [ "${SIFT_NO_OPEN:-}" != "1" ] && command -v open >/dev/null 2>&1; then open "http://127.0.0.1:$UI_PORT" || true; fi

wait
