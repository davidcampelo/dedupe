"""Safety net: filesystem-removing calls may only live in core/actions.py (or the allowlist)."""

from __future__ import annotations

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "dedupe"

# module path (relative to dedupe/) -> reason deletion calls are acceptable there
ALLOWLIST: dict[str, str] = {
    "core/actions.py": "the one place that deletes user files, behind plan/guards",
    "core/settings.py": "atomic replace of our own settings.toml (never a user file)",
}

FORBIDDEN_ATTRS = {
    ("os", "remove"),
    ("os", "unlink"),
    ("os", "rmdir"),
    ("os", "removedirs"),
    ("os", "replace"),
    ("os", "rename"),
    ("shutil", "rmtree"),
    ("shutil", "move"),
}
FORBIDDEN_METHODS = {"unlink", "rmdir", "rmtree"}
FORBIDDEN_NAMES = {"send2trash", "rmtree"}


def violations(source: str) -> list[int]:
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                if (
                    (isinstance(f.value, ast.Name) and (f.value.id, f.attr) in FORBIDDEN_ATTRS)
                    or f.attr in FORBIDDEN_METHODS
                    or f.attr == "send2trash"
                ):
                    found.append(node.lineno)
            elif isinstance(f, ast.Name) and f.id in FORBIDDEN_NAMES:
                found.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.module in {"send2trash", "shutil"}:
            if any(a.name in FORBIDDEN_NAMES | {"move"} for a in node.names):
                found.append(node.lineno)
    return found


def test_detector_catches_a_stray_remove() -> None:
    assert violations("import os\nos.remove('x')\n") == [2]
    assert violations("from pathlib import Path\nPath('x').unlink()\n") == [2]
    assert violations("from send2trash import send2trash\n") == [1]
    assert violations("x = 1\n") == []


def test_no_deletion_calls_outside_actions() -> None:
    bad = {}
    for py in PKG.rglob("*.py"):
        rel = py.relative_to(PKG).as_posix()
        if rel in ALLOWLIST:
            continue
        lines = violations(py.read_text())
        if lines:
            bad[rel] = lines
    assert not bad, f"deletion calls outside core/actions.py: {bad}"
