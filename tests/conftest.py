from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """No test may touch the real Trash, cache, config or action log."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    for var, sub in (
        ("XDG_CACHE_HOME", ".cache"),
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_DATA_HOME", ".local/share"),
        ("XDG_STATE_HOME", ".local/state"),
    ):
        monkeypatch.setenv(var, str(home / sub))
    monkeypatch.delenv("XDG_DATA_DIRS", raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", os.environ.get("QT_QPA_PLATFORM", "offscreen"))
    yield home


Tree = dict[str, "bytes | str | None"]


@pytest.fixture
def make_tree(tmp_path: Path) -> Callable[[Tree], Path]:
    """Build a directory tree: value bytes/str = file content, None = empty directory."""

    def build(spec: Tree) -> Path:
        root = tmp_path / "tree"
        root.mkdir(exist_ok=True)
        for rel, content in spec.items():
            target = root / rel
            if content is None:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode() if isinstance(content, str) else content)
        return root

    return build
