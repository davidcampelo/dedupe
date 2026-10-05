from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from dedupe.core.models import CancelToken, Progress, ScanOptions
from dedupe.core.scanner import scan_files

MakeTree = Callable[..., Path]


def names(entries: list) -> set[str]:  # type: ignore[type-arg]
    return {e.path.name for e in entries}


def run(root: Path, **kw: object) -> tuple[set[str], list]:  # type: ignore[type-arg]
    entries, skipped = scan_files(root, ScanOptions(**kw), CancelToken())  # type: ignore[arg-type]
    return names(entries), skipped


def test_collects_metadata(make_tree: MakeTree) -> None:
    root = make_tree({"a/b/f.txt": "hello"})
    entries, skipped = scan_files(root, ScanOptions(), CancelToken())
    assert skipped == []
    (e,) = entries
    st = os.stat(e.path)
    assert (e.size, e.mtime_ns, e.inode, e.device) == (5, st.st_mtime_ns, st.st_ino, st.st_dev)


def test_symlinks_skipped_by_default(make_tree: MakeTree) -> None:
    root = make_tree({"real.txt": "x", "d/inner.txt": "y"})
    (root / "link.txt").symlink_to(root / "real.txt")
    (root / "dlink").symlink_to(root / "d")
    assert run(root)[0] == {"real.txt", "inner.txt"}


def test_symlink_loop_terminates_when_following(make_tree: MakeTree) -> None:
    root = make_tree({"d/f.txt": "x"})
    (root / "d" / "loop").symlink_to(root)
    (root / "alias").symlink_to(root / "d")
    found, skipped = run(root, follow_symlinks=True)
    assert "f.txt" in found
    assert skipped == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_unreadable_goes_to_skipped(make_tree: MakeTree) -> None:
    root = make_tree({"ok.txt": "a", "locked/x.txt": "b", "secret.txt": "c"})
    (root / "locked").chmod(0)
    (root / "secret.txt").chmod(0)
    try:
        found, skipped = run(root)
    finally:
        (root / "locked").chmod(0o755)
    assert found == {"ok.txt"}
    assert {Path(s.path).name for s in skipped} == {"locked", "secret.txt"}
    assert all(s.reason == "permission denied" for s in skipped)


def test_exclusions(make_tree: MakeTree) -> None:
    root = make_tree(
        {
            ".git/config": "x",
            "node_modules/m.js": "x",
            "keep.txt": "x",
            "skip.log": "x",
            "sub/dir/z.txt": "x",
            "sub/other.txt": "x",
        }
    )
    found, _ = run(root, exclude=(".git", "node_modules", "*.log", str(root / "sub" / "dir")))
    assert found == {"keep.txt", "other.txt"}


def test_relative_path_glob(make_tree: MakeTree) -> None:
    root = make_tree({"a/b/x.txt": "1", "c/x.txt": "1"})
    found, _ = run(root, exclude=("a/b",))
    assert found == {"x.txt"}
    entries, _ = scan_files(root, ScanOptions(exclude=("a/b",)), CancelToken())
    assert [e.path.parent.name for e in entries] == ["c"]


def test_min_size_and_hidden(make_tree: MakeTree) -> None:
    root = make_tree(
        {"empty": b"", "small": "a", "big": "abcdef", ".hid": "abcdef", ".d/in": "abcdef"}
    )
    assert run(root, exclude=())[0] == {"small", "big", ".hid", "in"}
    assert run(root, exclude=(), min_size=0)[0] == {"empty", "small", "big", ".hid", "in"}
    assert run(root, exclude=(), min_size=3)[0] == {"big", ".hid", "in"}
    assert run(root, exclude=(), include_hidden=False)[0] == {"small", "big"}


def test_root_errors(tmp_path: Path) -> None:
    _, skipped = scan_files(tmp_path / "nope", ScanOptions(), CancelToken())
    assert skipped[0].reason == "vanished"
    f = tmp_path / "file"
    f.write_text("x")
    _, skipped = scan_files(f, ScanOptions(), CancelToken())
    assert skipped[0].reason == "not a directory"


def test_progress_reported(make_tree: MakeTree) -> None:
    root = make_tree({f"f{i}": "x" for i in range(450)})
    seen: list[Progress] = []
    scan_files(root, ScanOptions(), CancelToken(), seen.append)
    assert seen[-1].done == 450
    assert len(seen) >= 3


def test_cancel_mid_walk_is_prompt(make_tree: MakeTree) -> None:
    root = make_tree({f"d{i}/f": "x" for i in range(300)})
    token = CancelToken()
    count = 0

    def on_progress(p: Progress) -> None:
        nonlocal count
        count += 1
        if count == 1:
            token.cancel()

    start = time.monotonic()
    entries, _ = scan_files(root, ScanOptions(), token, on_progress)
    assert time.monotonic() - start < 0.1
    assert len(entries) < 300


def test_cancel_from_other_thread(make_tree: MakeTree) -> None:
    root = make_tree({f"f{i}": "x" for i in range(50)})
    token = CancelToken()
    threading.Timer(0, token.cancel).start()
    scan_files(root, ScanOptions(), token)
