from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from dedupe.core.models import CancelToken, Progress, ScanOptions
from dedupe.core.pipeline import run_scan

MakeTree = Callable[..., Path]


def test_run_scan_end_to_end(make_tree: MakeTree) -> None:
    root = make_tree({"a": "dup", "b": "dup", "c": "uniq", "locked/../d": "dup"})
    result = run_scan(root, ScanOptions(exclude=()))
    assert result.files_scanned == 4
    assert len(result.groups) == 1 and len(result.groups[0].files) == 3
    assert not result.cancelled


def test_cancel_returns_cancelled_result(make_tree: MakeTree) -> None:
    root = make_tree({f"f{i}": b"x" * 100_000 for i in range(20)})
    token = CancelToken()

    def progress(p: Progress) -> None:
        if p.stage.value != "walk":
            token.cancel()

    result = run_scan(root, ScanOptions(exclude=()), progress, token)
    assert result.cancelled and result.groups == ()


def test_cancel_before_start(make_tree: MakeTree) -> None:
    token = CancelToken()
    token.cancel()
    assert run_scan(make_tree({"a": "x"}), cancel=token).cancelled
