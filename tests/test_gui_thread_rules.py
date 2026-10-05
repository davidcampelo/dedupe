"""Static rule: no blocking I/O on the GUI thread. Everything in dedupe/gui must hand disk,
hash, image-decode, database and process work to a Job (gui/workers.py and the job modules)."""

from __future__ import annotations

import ast
from pathlib import Path

GUI = Path(__file__).resolve().parent.parent / "dedupe" / "gui"

# module (relative to dedupe/gui) -> reason it may contain blocking calls
ALLOWLIST: dict[str, str] = {
    "workers.py": "its code runs on pool threads, never on the GUI thread",
    "thumbnails.py": "decode functions run inside ThumbnailJobs on a worker pool",
    "icons.py": "reads the bundled SVG resources (a few KB, local package data)",
}

BLOCKING_METHODS = {
    "stat", "lstat", "exists", "is_file", "is_dir", "is_symlink", "read_text", "read_bytes",
    "write_text", "write_bytes", "iterdir", "glob", "rglob", "scandir", "walk", "listdir",
    "unlink", "rmdir", "mkdir", "rename", "resolve", "samefile", "run", "check_output", "popen",
    "send2trash", "hexdigest",
}  # fmt: skip
BLOCKING_NAMES = {"open", "send2trash", "scandir", "listdir"}
BLOCKING_MODULES = {"hashlib", "blake3", "subprocess", "sqlite3", "send2trash", "shutil"}


def violations(source: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in BLOCKING_METHODS:
                # `Image.open` and friends are caught by the name `open` below only for bare
                # calls, so also flag attribute `.open(` on anything.
                found.append((node.lineno, f".{f.attr}()"))
            elif isinstance(f, ast.Attribute) and f.attr == "open":
                found.append((node.lineno, ".open()"))
            elif isinstance(f, ast.Name) and f.id in BLOCKING_NAMES:
                found.append((node.lineno, f"{f.id}()"))
        elif isinstance(node, ast.Import):
            found += [
                (node.lineno, f"import {a.name}") for a in node.names if a.name in BLOCKING_MODULES
            ]
        elif isinstance(node, ast.ImportFrom) and node.module in BLOCKING_MODULES:
            found.append((node.lineno, f"from {node.module} import ..."))
    return found


def test_detector_catches_blocking_calls() -> None:
    assert violations("import os\nos.stat('x')\n") == [(2, ".stat()")]
    assert violations("from pathlib import Path\nPath('x').exists()\n") == [(2, ".exists()")]
    assert violations("open('f')\n") == [(1, "open()")]
    assert violations("from PIL import Image\nImage.open('f')\n") == [(2, ".open()")]
    assert violations("import hashlib\n") == [(1, "import hashlib")]
    assert violations("import subprocess\nsubprocess.run(['x'])\n") == [
        (1, "import subprocess"),
        (2, ".run()"),
    ]
    assert violations("x = 1 + 2\n") == []


def test_no_blocking_calls_on_the_gui_thread() -> None:
    bad: dict[str, list[tuple[int, str]]] = {}
    for py in GUI.rglob("*.py"):
        rel = py.relative_to(GUI).as_posix()
        if rel in ALLOWLIST:
            continue
        found = violations(py.read_text())
        if found:
            bad[rel] = found
    assert not bad, f"blocking calls outside job modules: {bad}"
