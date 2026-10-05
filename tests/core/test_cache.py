from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from dedupe.core import paths
from dedupe.core.cache import HashCache
from dedupe.core.hasher import HashService
from dedupe.core.models import CancelToken, ScanOptions
from dedupe.core.pipeline import run_scan
from dedupe.core.scanner import scan_files

MakeTree = Callable[..., Path]


@pytest.fixture
def cache() -> Iterator[HashCache]:
    c = HashCache()
    yield c
    c.close()


def scan_and_hash(root: Path, cache: HashCache) -> HashService:
    token = CancelToken()
    entries, _ = scan_files(root, ScanOptions(exclude=()), token)
    svc = HashService(token, store=cache)
    svc.partial(entries)
    svc.full(entries)
    cache.flush()
    return svc


def test_db_lives_under_xdg_cache(cache: HashCache) -> None:
    assert cache.db_path == paths.hash_db_file()
    assert cache.db_path.is_relative_to(Path(os.environ["XDG_CACHE_HOME"]))


def test_second_scan_reads_zero_bytes(make_tree: MakeTree, cache: HashCache) -> None:
    root = make_tree({"a": "x" * 5000, "b": "y" * 5000})
    first = scan_and_hash(root, cache)
    second = scan_and_hash(root, cache)
    assert first.bytes_read > 0
    assert second.bytes_read == 0


def test_change_invalidates_only_that_entry(make_tree: MakeTree, cache: HashCache) -> None:
    root = make_tree({"a": "x" * 5000, "b": "y" * 5000})
    scan_and_hash(root, cache)
    (root / "a").write_bytes(b"z" * 5000)  # same size, new mtime
    os.utime(root / "a", ns=(10**18, 10**18))
    second = scan_and_hash(root, cache)
    assert second.bytes_read == 5000 + 5000  # only a: partial (5000) + full (5000)


def test_inode_change_invalidates(make_tree: MakeTree, cache: HashCache) -> None:
    root = make_tree({"a": "x" * 100})
    scan_and_hash(root, cache)
    st = (root / "a").stat()
    (root / "a").unlink()
    (root / "a").write_bytes(b"x" * 100)
    os.utime(root / "a", ns=(st.st_atime_ns, st.st_mtime_ns))
    entry = scan_files(root, ScanOptions(exclude=()), CancelToken())[0][0]
    if entry.inode == st.st_ino:  # filesystem reused the inode; force a differing key
        entry = replace(entry, inode=entry.inode + 1)
    assert cache.get(entry) is None


def test_partial_and_full_merge(make_tree: MakeTree, cache: HashCache) -> None:
    root = make_tree({"a": "abc"})
    entry = scan_files(root, ScanOptions(exclude=()), CancelToken())[0][0]
    cache.put_partial(entry, "p")
    cache.flush()
    hit = cache.get(entry)
    assert hit is not None and (hit.partial, hit.full) == ("p", None)
    cache.put_full(entry, "f")
    cache.flush()
    hit = cache.get(entry)
    assert hit is not None and (hit.partial, hit.full) == ("p", "f")
    assert cache.count() == 1


def test_clear(make_tree: MakeTree, cache: HashCache) -> None:
    root = make_tree({"a": "x" * 10, "b": "y" * 10})
    scan_and_hash(root, cache)
    assert cache.count() == 2
    assert cache.clear() == 2
    assert cache.count() == 0


def test_corrupt_db_degrades_with_warning(make_tree: MakeTree) -> None:
    db = paths.hash_db_file()
    db.parent.mkdir(parents=True)
    db.write_bytes(b"this is not a sqlite database" * 100)
    cache = HashCache()
    assert not cache.enabled and "unavailable" in cache.warnings[0]
    root = make_tree({"a": "same", "b": "same"})
    result = run_scan(root, ScanOptions(exclude=()), store=cache)
    assert len(result.groups) == 1
    assert any("hash cache" in w for w in result.warnings)


def test_locked_db_degrades_on_write(make_tree: MakeTree) -> None:
    cache = HashCache(timeout=0.2)
    blocker = sqlite3.connect(cache.db_path, timeout=0.2)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        root = make_tree({"a": "same", "b": "same"})
        result = run_scan(root, ScanOptions(exclude=()), store=cache)
        cache.flush()
    finally:
        blocker.rollback()
        blocker.close()
    assert len(result.groups) == 1
    assert not cache.enabled
    assert "write failed" in cache.warnings[0] or "read failed" in cache.warnings[0]
    cache.close()
