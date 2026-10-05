#!/usr/bin/env bash
# PostToolUse(Write|Edit): format and lint the Python file Claude just wrote.
# Remaining lint errors are fed back to Claude (exit 2) so they get fixed immediately.
set -uo pipefail
root="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"
file=$(jq -r '.tool_response.filePath // .tool_input.file_path // empty')

[[ "$file" == *.py && -f "$file" && "$file" == "$root"/* ]] || exit 0
ruff="$root/.venv/bin/ruff"
[[ -x "$ruff" ]] || ruff=$(command -v ruff) || exit 0

cd "$root"
"$ruff" format --quiet "$file"
if ! out=$("$ruff" check --fix --quiet "$file" 2>&1); then
  printf 'ruff reported problems in %s that need fixing:\n%s\n' "$file" "$out" >&2
  exit 2
fi
