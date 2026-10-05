#!/usr/bin/env bash
# PreToolUse(Bash): Claude may not bypass or disable the quality gates.
set -uo pipefail
cmd=$(jq -r '.tool_input.command // empty')
# Pointing hooksPath at the project's .githooks (as post-create.sh does) is the one allowed form.
if grep -qE -- '--no-verify|SKIP_CHECKS' <<<"$cmd" \
  || { grep -qE 'core\.hooksPath' <<<"$cmd" \
       && ! grep -qE 'core\.hooksPath[[:space:]]+\.githooks([[:space:]]|$|;|&)' <<<"$cmd"; }; then
  echo "Blocked: this command bypasses or reconfigures the project's git hooks. Fix the failing check instead, or ask the user." >&2
  exit 2
fi
