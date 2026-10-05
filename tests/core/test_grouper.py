from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from dedupe.core.grouper import files_equal
from dedupe.core.hasher import PARTIAL_BYTES
from tests.core.helpers import group, group_names

MakeTree = Callable[..., Path]


def test_identical_content_different_names_and_dates(make_tree: MakeTree) -> None:
    root = make_tree({"a.txt": "content", "sub/b.dat": "content", "c": "other!!"})
    os.utime(root / "a.txt", (1_000_000, 1_000_000))
    out = group(root)
    assert group_names(out) == [{"a.txt", "b.dat"}]
    assert out.groups[0].reclaimable == len("content")
    assert out.groups[0].hash


def test_same_size_different_content_not_grouped(make_tree: MakeTree) -> None:
    assert group(make_tree({"a": "aaaa", "b": "bbbb"})).groups == ()


def test_middle_difference_separated_by_full_hash(make_tree: MakeTree) -> None:
    head, tail = b"H" * PARTIAL_BYTES, b"T" * PARTIAL_BYTES
    mid = 200_000
    root = make_tree(
        {
            "a": head + b"x" * mid + tail,
            "b": head + b"x" * mid + tail,
            "c": head + b"y" * mid + tail,
        }
    )
    assert group_names(group(root)) == [{"a", "b"}]


def test_hardlinks_are_not_deletable_duplicates(make_tree: MakeTree) -> None:
    root = make_tree({"a": "data", "c": "data"})
    os.link(root / "a", root / "b")
    out = group(root)
    (g,) = out.groups
    assert len(g.files) == 2  # a/b collapsed to one representative, plus c
    assert {p.name for p in g.hardlinked} == {"b"}
    assert g.reclaimable == 4


def test_only_hardlinks_means_no_group_but_reported(make_tree: MakeTree) -> None:
    root = make_tree({"a": "data"})
    os.link(root / "a", root / "b")
    out = group(root)
    assert out.groups == ()
    assert [[p.name for p in s] for s in out.hardlink_sets] == [["a", "b"]]


def test_empty_files_are_own_category(make_tree: MakeTree) -> None:
    root = make_tree({"e1": b"", "e2": b"", "e3": b""})
    out = group(root, min_size=0)
    assert out.groups == ()
    assert len(out.empty_files) == 3


@pytest.mark.parametrize("paranoid", [False, True])
def test_paranoid_agrees_with_hash(make_tree: MakeTree, paranoid: bool) -> None:
    root = make_tree({"a": "same", "b": "same", "c": "diff", "d": "diff", "e": "uniq"})
    assert group_names(group(root, paranoid=paranoid)) == [{"a", "b"}, {"c", "d"}]


def test_paranoid_splits_when_hashes_would_collide(make_tree: MakeTree) -> None:
    from dedupe.core.grouper import ByteCompareStage, Context
    from dedupe.core.hasher import HashService
    from dedupe.core.models import CancelToken, ScanOptions
    from dedupe.core.scanner import scan_files

    root = make_tree({"a": "aaaa", "b": "aaaa", "c": "bbbb"})
    token = CancelToken()
    entries, _ = scan_files(root, ScanOptions(exclude=()), token)
    ctx = Context(HashService(token), token)
    out = ByteCompareStage().apply([entries], ctx)  # forced-equal bucket
    assert sorted(sorted(e.path.name for e in b) for b in out) == [["a", "b"]]


def test_files_equal(make_tree: MakeTree) -> None:
    root = make_tree({"a": b"x" * 2_500_000, "b": b"x" * 2_500_000, "c": b"x" * 2_499_999 + b"y"})
    assert files_equal(root / "a", root / "b")
    assert not files_equal(root / "a", root / "c")


def test_unreadable_during_hashing_is_skipped(make_tree: MakeTree) -> None:
    from dedupe.core.grouper import group_duplicates
    from dedupe.core.hasher import HashService
    from dedupe.core.models import CancelToken, ScanOptions
    from dedupe.core.scanner import scan_files

    root = make_tree({"a": "same", "b": "same", "c": "same"})
    token = CancelToken()
    entries, _ = scan_files(root, ScanOptions(exclude=()), token)
    (root / "b").unlink()
    out = group_duplicates(entries, HashService(token), token)
    assert group_names(out) == [{"a", "c"}]
    assert [Path(s.path).name for s in out.skipped] == ["b"]


def test_groups_sorted_by_reclaimable(make_tree: MakeTree) -> None:
    root = make_tree({"s1": "ab", "s2": "ab", "b1": "x" * 100, "b2": "x" * 100})
    assert [g.size for g in group(root).groups] == [100, 2]
