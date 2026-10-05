from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from dedupe.core.hasher import PARTIAL_BYTES, HashService, full_hash, partial_hash
from dedupe.core.models import CancelledError, CancelToken, FileEntry, Progress, Stage

MakeTree = Callable[..., Path]


def entry_for(path: Path) -> FileEntry:
    st = os.stat(path)
    return FileEntry(path, st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev, st.st_mode)


def test_full_hash_is_content_only(make_tree: MakeTree) -> None:
    root = make_tree({"a": "same", "b": "same", "c": "diff"})
    assert full_hash(root / "a") == full_hash(root / "b") != full_hash(root / "c")


def test_partial_hash_ignores_middle_but_not_size(make_tree: MakeTree) -> None:
    head, tail = b"H" * PARTIAL_BYTES, b"T" * PARTIAL_BYTES
    root = make_tree(
        {
            "a": head + b"x" * 1000 + tail,
            "b": head + b"y" * 1000 + tail,
            "c": head + b"x" * 1001 + tail,
        }
    )
    pa, pb, pc = (partial_hash(root / n, (root / n).stat().st_size) for n in "abc")
    assert pa == pb  # differ only in the middle
    assert pa != pc  # size is part of the hash


def test_small_file_partial_hash(make_tree: MakeTree) -> None:
    root = make_tree({"a": "hi", "b": "ho"})
    assert partial_hash(root / "a", 2) != partial_hash(root / "b", 2)


def test_service_hashes_and_reports_progress(make_tree: MakeTree) -> None:
    root = make_tree({"a": "x" * 3000, "b": "y" * 3000})
    events: list[Progress] = []
    svc = HashService(CancelToken(), events.append, workers=2)
    out = svc.full([entry_for(root / "a"), entry_for(root / "b")])
    assert out[root / "a"] == full_hash(root / "a")
    assert events[-1] == Progress(Stage.FULL, 6000, 6000, events[-1].current_path)
    assert svc.bytes_read == 6000


def test_unreadable_file_is_reported_not_raised(make_tree: MakeTree) -> None:
    root = make_tree({"a": "x", "gone": "y"})
    entries = [entry_for(root / "a"), entry_for(root / "gone")]
    (root / "gone").unlink()
    svc = HashService(CancelToken())
    out = svc.full(entries)
    assert set(out) == {root / "a"}
    assert svc.failed[0].reason == "vanished"


def test_cancel_raises(make_tree: MakeTree) -> None:
    root = make_tree({"a": b"x" * (3 * 1024 * 1024)})
    token = CancelToken()
    token.cancel()
    with pytest.raises(CancelledError):
        HashService(token).full([entry_for(root / "a")])


def test_cancel_during_hashing(make_tree: MakeTree) -> None:
    root = make_tree({f"f{i}": b"x" * (2 * 1024 * 1024) for i in range(6)})
    token = CancelToken()

    def progress(p: Progress) -> None:
        if p.done > 0:
            token.cancel()

    with pytest.raises(CancelledError):
        HashService(token, progress, workers=2).full([entry_for(p) for p in root.iterdir()])


class FakeStore:
    def __init__(self) -> None:
        self.data: dict[Path, tuple[str | None, str | None]] = {}

    def get(self, entry: FileEntry):  # type: ignore[no-untyped-def]
        from dedupe.core.hasher import CachedHashes

        hit = self.data.get(entry.path)
        return CachedHashes(*hit) if hit else None

    def put_partial(self, entry: FileEntry, value: str) -> None:
        self.data[entry.path] = (value, (self.data.get(entry.path) or (None, None))[1])

    def put_full(self, entry: FileEntry, value: str) -> None:
        self.data[entry.path] = ((self.data.get(entry.path) or (None, None))[0], value)


def test_store_hits_read_nothing(make_tree: MakeTree) -> None:
    root = make_tree({"a": "x" * 500, "b": "y" * 500})
    entries = [entry_for(root / "a"), entry_for(root / "b")]
    store = FakeStore()
    first = HashService(CancelToken(), store=store)  # type: ignore[arg-type]
    r1 = first.full(entries)
    second = HashService(CancelToken(), store=store)  # type: ignore[arg-type]
    r2 = second.full(entries)
    assert r1 == r2
    assert first.bytes_read == 1000 and second.bytes_read == 0
    p1 = HashService(CancelToken(), store=store).partial(entries)  # type: ignore[arg-type]
    assert set(p1) == {root / "a", root / "b"}
    assert HashService(CancelToken(), store=store).partial(entries) == p1  # type: ignore[arg-type]
