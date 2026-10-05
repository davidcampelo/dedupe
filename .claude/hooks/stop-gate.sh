#!/usr/bin/env bash
# Stop: when Python code or pyproject.toml has uncommitted changes, Claude may not finish
# its turn until the fast gate passes. Blocks once per stop attempt (stop_hook_active
# prevents an endless loop); the failure output goes back to Claude.
set -uo pipefail
input=$(cat)
[[ $(jq -r '.stop_hook_active // false' <<<"$input") == true ]] && exit 0

cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"
changed=$(git status --porcelain --untracked-files=all -- '*.py' pyproject.toml | head -n1)
[[ -z "$changed" ]] && exit 0

if ! out=$(scripts/check.sh --fast 2>&1); then
  { echo "Quality gate failed (scripts/check.sh --fast). Fix it before finishing, or tell the user why it can't pass:"
    tail -n 60 <<<"$out"; } >&2
  exit 2
fi
