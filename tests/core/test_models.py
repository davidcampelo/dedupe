from __future__ import annotations

from pathlib import Path

import pytest

from dedupe.core.models import (
    CancelledError,
    CancelToken,
    DuplicateGroup,
    FileEntry,
    ScanOptions,
    ScanResult,
)


def entry(name: str, size: int = 10, inode: int = 1) -> FileEntry:
    return FileEntry(Path(name), size, 0, inode, 1)


def test_scan_options_defaults_match_spec() -> None:
    o = ScanOptions()
    assert o.min_size == 1
    assert o.include_hidden is True
    assert o.follow_symlinks is False
    assert o.cross_filesystems is False
    assert o.paranoid is False
    assert {".git", "node_modules", "__pycache__", ".cache", "/proc", "/sys", "/dev"} <= set(
        o.exclude
    )


def test_cancel_token() -> None:
    t = CancelToken()
    t.raise_if_cancelled()
    t.cancel()
    assert t.cancelled
    with pytest.raises(CancelledError):
        t.raise_if_cancelled()


def test_reclaimable() -> None:
    g = DuplicateGroup("h", 100, (entry("a", 100), entry("b", 100, 2), entry("c", 100, 3)))
    assert g.reclaimable == 200
    assert ScanResult(Path("."), (g,)).reclaimable == 200
    assert entry("a").identity == (1, 1)
