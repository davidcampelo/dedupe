#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python -m venv .venv
.venv/bin/pip install --upgrade pip

if [ -f pyproject.toml ]; then
  # Editable install with dev extras if defined, otherwise plain editable install.
  .venv/bin/pip install -e ".[dev]" || .venv/bin/pip install -e .
else
  echo "No pyproject.toml yet; installing the baseline toolchain from the spec."
  .venv/bin/pip install \
    PySide6 blake3 xxhash Pillow pillow-heif send2trash \
    pytest pytest-qt ruff mypy hatchling build
fi

# Quality gates: pre-commit runs scripts/check.sh --fast, pre-push runs the full gate.
git config core.hooksPath .githooks

echo
echo "Ready. GUI: run 'xhost +local:' on the host, then 'python -m dedupe.gui'."
echo "Headless tests: QT_QPA_PLATFORM=offscreen pytest   (or: xvfb-run pytest)"
