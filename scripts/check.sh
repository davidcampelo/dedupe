#!/usr/bin/env bash
# The single quality gate. Git hooks, Claude Code hooks and CI all run this script,
# so "passes locally" and "passes in CI" mean the same thing.
#
#   scripts/check.sh          full gate: lint, format, types, every test, core coverage
#   scripts/check.sh --fast   pre-commit gate: same checks, skips @pytest.mark.slow, no coverage
set -euo pipefail
cd "$(dirname "$0")/.."

mode=full
[[ "${1:-}" == "--fast" ]] && mode=fast

if [[ ! -d dedupe ]]; then
  echo "check: no dedupe/ package yet (Task 1 creates it); nothing to check."
  exit 0
fi

[[ -x .venv/bin/python ]] && PATH="$PWD/.venv/bin:$PATH"
for tool in ruff mypy pytest; do
  command -v "$tool" >/dev/null && continue
  echo "check: '$tool' not found; run: pip install -e '.[dev]'" >&2
  # .venv is created by the devcontainer; its python links to the container's interpreter,
  # so on the host the link dangles and none of its tools are usable.
  if [[ -L .venv/bin/python && ! -e .venv/bin/python ]]; then
    echo "check: .venv belongs to the devcontainer ($(readlink .venv/bin/python) does not exist here)." >&2
    echo "check: run git commit/push from the devcontainer terminal, or activate a host venv with '.[dev]' installed." >&2
  fi
  exit 1
done

export QT_QPA_PLATFORM=offscreen

echo "== ruff check";        ruff check .
echo "== ruff format";       ruff format --check .
echo "== mypy";              mypy
if [[ $mode == fast ]]; then
  echo "== pytest (fast)";   pytest -q -x -m "not slow"
else
  echo "== pytest (full)";   pytest -q --cov=dedupe.core --cov-report=term-missing:skip-covered --cov-fail-under=90
  command -v desktop-file-validate >/dev/null || {
    echo "check: 'desktop-file-validate' not found; install desktop-file-utils" >&2; exit 1; }
  shopt -s nullglob
  desktop_files=(data/*.desktop)
  [[ ${#desktop_files[@]} -gt 0 ]] || { echo "check: no data/*.desktop file to validate" >&2; exit 1; }
  echo "== desktop-file-validate"; desktop-file-validate "${desktop_files[@]}"
fi
echo "check: $mode gate passed."
