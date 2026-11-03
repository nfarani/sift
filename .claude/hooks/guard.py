#!/usr/bin/env python3
"""Sift safety guard — Claude Code PreToolUse hook.

Blocks (exit 2, reason on stderr, which Claude sees):
  * App code that introduces forbidden Gmail scopes or destructive/sending API methods.
  * Shell commands that touch OAuth tokens, call Gmail directly, or are destructive.

Docs (*.md), tests/ and .claude/ are exempt from the code scan, because they
legitimately *mention* forbidden things in order to forbid or test for them.
"""
import json
import re
import sys

FORBIDDEN_CODE = [
    (r"https://mail\.google\.com/", "full-access Gmail scope (permits permanent delete)"),
    (r"gmail\.send\b", "gmail.send scope — the app must never send mail"),
    (r"gmail\.compose\b", "gmail.compose scope — the app must never draft/send mail"),
    (r"gmail\.insert\b", "gmail.insert scope is not needed"),
    (r"\.batchDelete\s*\(", "messages.batchDelete — permanent delete is forbidden"),
    (r"messages\(\)\s*\.\s*delete\s*\(", "messages.delete — permanent delete is forbidden"),
    (r"threads\(\)\s*\.\s*delete\s*\(", "threads.delete — permanent delete is forbidden"),
    (r"messages\(\)\s*\.\s*send\s*\(", "messages.send — the app must never send mail"),
    (r"drafts\(\)", "drafts API — the app must never compose mail"),
    (r"emptyTrash|empty_trash", "emptying Trash is forbidden"),
]

FORBIDDEN_SHELL = [
    (r"gmail\.googleapis\.com", "calling Gmail directly from the shell — go through the app"),
    (r"(token|credentials)[^\s]*\.json", "reading/printing OAuth credential or token files"),
    (r"\bkeyring\s+get\b", "reading secrets from the keychain"),
    (r"\bsecurity\s+find-(generic|internet)-password\b", "reading secrets from the macOS keychain"),
    (r"(cat|less|more|head|tail|grep)\s+[^|;]*\.env\b(?!\.example)", "printing .env secrets"),
    (r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME)(\s|$)", "recursive delete of root/home"),
    (r"git\s+push\s+.*--force", "force-push"),
]

EXEMPT_PATH = re.compile(r"(\.md$|(^|/)tests?/|(^|/)\.claude/|test_[^/]*\.py$|\.test\.tsx?$)")


def block(reason: str) -> None:
    print(f"Blocked by Sift safety guard: {reason}. See CLAUDE.md > Safety. "
          "Find a compliant approach instead of working around this hook.", file=sys.stderr)
    sys.exit(2)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    tool = data.get("tool_name", "")
    inp = data.get("tool_input", {}) or {}

    if tool == "Bash":
        cmd = inp.get("command", "")
        for pat, why in FORBIDDEN_SHELL:
            if re.search(pat, cmd, re.IGNORECASE):
                block(why)
        sys.exit(0)

    if tool in ("Write", "Edit", "MultiEdit"):
        path = inp.get("file_path", "")
        if EXEMPT_PATH.search(path):
            sys.exit(0)
        texts = [inp.get("content", ""), inp.get("new_string", "")]
        texts += [e.get("new_string", "") for e in inp.get("edits", []) or []]
        blob = "\n".join(t for t in texts if t)
        for pat, why in FORBIDDEN_CODE:
            if re.search(pat, blob):
                block(f"{why} (in {path})")
    sys.exit(0)


if __name__ == "__main__":
    main()
