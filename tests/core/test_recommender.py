from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dedupe.core.models import DuplicateGroup, FileEntry, Verdict
from dedupe.core.recommender import (
    in_disposable_folder,
    keep_in_folder,
    keep_newest,
    looks_like_copy,
    recommend_all,
    recommend_group,
    suggested_deletions,
)

ROOT = Path("/r")


def fe(path: str, mtime: int = 100) -> FileEntry:
    return FileEntry(Path(path), 10, mtime, hash(path) & 0xFFFFFF, 1)


def grp(*files: FileEntry) -> DuplicateGroup:
    return DuplicateGroup("h", 10, tuple(files))


def keeper(g: DuplicateGroup, protected: tuple[str, ...] = ()) -> str:
    recs = recommend_group(g, protected, ROOT)
    (k,) = [r for r in recs if r.verdict is Verdict.KEEP]
    return str(k.path)


def reason(g: DuplicateGroup, protected: tuple[str, ...] = ()) -> str:
    return next(r.reason for r in recommend_group(g, protected, ROOT) if r.verdict is Verdict.KEEP)


# Each case: rule N decides while every earlier rule ties.
CASES = [
    ("1 protected", [fe("/r/a/x.txt"), fe("/r/pref/x.txt", 999)], ("/r/pref",), "/r/pref/x.txt", "preferred"),
    ("2 disposable", [fe("/r/Downloads/x.txt", 1), fe("/r/docs/x.txt", 2)], (), "/r/docs/x.txt", "disposable"),
    ("3 copy name", [fe("/r/d/x copy.txt", 1), fe("/r/d/x.txt", 2)], (), "/r/d/x.txt", "copy"),
    ("4 oldest", [fe("/r/a/x.txt", 5), fe("/r/b/x.txt", 3)], (), "/r/b/x.txt", "oldest"),
    ("5 shorter", [fe("/r/a/b/x.txt"), fe("/r/a/x.txt")], (), "/r/a/x.txt", "shorter"),
    ("6 alphabetical", [fe("/r/b/x.txt"), fe("/r/a/x.txt")], (), "/r/a/x.txt", "alphabetical"),
]  # fmt: skip


@pytest.mark.parametrize(("label", "files", "protected", "expected", "why"), CASES)
def test_each_rule_decides_when_earlier_rules_tie(
    label: str, files: list[FileEntry], protected: tuple[str, ...], expected: str, why: str
) -> None:
    g = grp(*files)
    assert keeper(g, protected) == expected
    assert why in reason(g, protected)


def test_rule_order_earlier_rule_beats_later() -> None:
    # Older and shorter, but in Downloads: rule 2 outranks rules 4 and 5.
    g = grp(fe("/r/Downloads/x.txt", 1), fe("/r/deep/er/x.txt", 999))
    assert keeper(g) == "/r/deep/er/x.txt"
    # Disposable beats copy-name: a clean name in a disposable folder loses to a copy name outside.
    g = grp(fe("/r/old/x.txt"), fe("/r/docs/x (1).txt"))
    assert keeper(g) == "/r/docs/x (1).txt"


@pytest.mark.parametrize(
    "name",
    ["Copy of a.txt", "a - Copy.txt", "a (1).txt", "a (2).jpg", "a_1.txt", "a.bak", "a copy.txt"],
)
def test_copy_names(name: str) -> None:
    assert looks_like_copy(name)


@pytest.mark.parametrize(
    "name", ["a.txt", "report_final.pdf", "photocopy.txt", "a1.txt", "x.tar.gz"]
)
def test_non_copy_names(name: str) -> None:
    assert not looks_like_copy(name)


def test_disposable_uses_path_relative_to_root() -> None:
    assert in_disposable_folder(Path("/r/Downloads/x"), ROOT)
    assert in_disposable_folder(Path("/r/my-backup/x"), ROOT)
    assert not in_disposable_folder(Path("/tmp/scan/docs/x"), Path("/tmp/scan"))
    assert in_disposable_folder(Path("/tmp/scan/docs/x"))  # without a root, /tmp counts


def test_all_protected_marks_every_file_keep() -> None:
    g = grp(fe("/r/p/a"), fe("/r/p/q/b"))
    assert [r.verdict for r in recommend_group(g, ("/r/p",), ROOT)] == [Verdict.KEEP] * 2


def test_protected_and_unprotected_mix() -> None:
    g = grp(fe("/r/p/a"), fe("/r/p2/b"), fe("/r/o/c"))
    recs = {str(r.path): r.verdict for r in recommend_group(g, ("/r/p", "/r/p2"), ROOT)}
    assert recs == {"/r/p/a": Verdict.KEEP, "/r/p2/b": Verdict.KEEP, "/r/o/c": Verdict.DELETE}


def test_protected_prefix_is_not_substring_match() -> None:
    g = grp(fe("/r/pics/a"), fe("/r/pics-old/b"))
    assert keeper(g, ("/r/pics",)) == "/r/pics/a"


@given(
    names=st.lists(
        st.text(alphabet="abc/_ ()1-.", min_size=1, max_size=12),
        min_size=2,
        max_size=6,
        unique=True,
    ),
    mtimes=st.lists(st.integers(0, 5), min_size=6, max_size=6),
    protected=st.sets(st.sampled_from(["/r/a", "/r/b", "/r/c"]), max_size=3),
)
def test_property_keep_and_protection(
    names: list[str], mtimes: list[int], protected: set[str]
) -> None:
    files = [
        fe("/r/" + n.strip("/").replace("//", "/") + f"/f{i}", mtimes[i])
        for i, n in enumerate(names)
    ]
    g = grp(*files)
    recs = recommend_group(g, tuple(protected), ROOT)
    assert len(recs) == len(files)
    keeps = [r for r in recs if r.verdict is Verdict.KEEP]
    assert keeps  # never delete every copy
    prot_files = [f for f in files if any(str(f.path).startswith(p + "/") for p in protected)]
    if prot_files:
        assert {r.path for r in keeps} == {f.path for f in prot_files}
    else:
        assert len(keeps) == 1
    # deterministic regardless of input order
    assert recommend_group(grp(*reversed(files)), tuple(protected), ROOT) == tuple(
        sorted(recs, key=lambda r: [f.path for f in reversed(files)].index(r.path))
    )


def test_bulk_keep_newest_and_folder_and_select_all() -> None:
    g = grp(fe("/r/a/x", 1), fe("/r/b/x", 9), fe("/r/c/x", 5))
    assert [r.path.parent.name for r in keep_newest(g, (), ROOT) if r.verdict is Verdict.KEEP] == [
        "b"
    ]
    kept = [r.path for r in keep_in_folder(g, "/r/c", (), ROOT) if r.verdict is Verdict.KEEP]
    assert kept == [Path("/r/c/x")]
    # folder holding no copy: recommendations unchanged
    assert keep_in_folder(g, "/elsewhere", (), ROOT) == recommend_group(g, (), ROOT)
    # protected files survive a bulk rule
    recs = keep_newest(g, ("/r/a",), ROOT)
    assert {r.path.parent.name for r in recs if r.verdict is Verdict.KEEP} == {"a", "b"}


def test_bulk_rules_leave_one_keep_per_group() -> None:
    groups = recommend_all(
        [
            grp(fe("/r/a/x", 1), fe("/r/b/x", 1)),
            grp(fe("/r/a/y", 2), fe("/r/c/y", 3), fe("/r/d/y", 3)),
        ],
        (),
        ROOT,
    )
    for g in groups:
        for recs in (keep_newest(g, (), ROOT), keep_in_folder(g, "/r/a", (), ROOT)):
            assert sum(r.verdict is Verdict.KEEP for r in recs) == 1
    assert len(suggested_deletions(groups)) == 3
