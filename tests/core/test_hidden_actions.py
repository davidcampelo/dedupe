from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from dedupe.core.actions import PlanRefused, Status, execute, plan_hidden_actions
from dedupe.core.hidden import HiddenItem, scan_hidden
from dedupe.core.models import DeleteMode

MakeTree = Callable[..., Path]


class Trash:
    def __init__(self, base: Path) -> None:
        base.mkdir(parents=True, exist_ok=True)
        self.base, self.calls = base, []

    def __call__(self, path: str) -> None:
        self.calls.append(path)
        shutil.move(path, self.base / f"{len(self.calls)}-{Path(path).name}")


@pytest.fixture
def trash(tmp_path: Path) -> Trash:
    return Trash(tmp_path / "bin")


@pytest.fixture
def tree(make_tree: MakeTree) -> tuple[Path, dict[str, HiddenItem]]:
    root = make_tree(
        {
            "notes.txt~": "backup",
            "~$lock.docx": "lock",
            ".cache/a/b/c.bin": "x" * 100,
            ".cache/d.bin": "y" * 10,
            ".bashrc": "rc",
            "keep.txt": "keep",
        }
    )
    items = {i.path.name: i for i in scan_hidden(root, open_files_fn=set).items}
    return root, items


def pick(items: dict[str, HiddenItem], *names: str) -> list[HiddenItem]:
    return [items[n] for n in names]


def test_trash_file_and_folder(tree, trash: Trash, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    root, items = tree
    plan = plan_hidden_actions(pick(items, "notes.txt~", ".cache"))
    assert not plan.hardlink_possible and plan.total_size == 6 + 110
    summary = execute(plan, trash=trash, log_path=tmp_path / "a.log")
    assert summary.done == 2 and summary.freed == 116
    assert not (root / "notes.txt~").exists() and not (root / ".cache").exists()
    assert (root / "keep.txt").exists() and (root / ".bashrc").exists()
    records = [json.loads(line) for line in (tmp_path / "a.log").read_text().splitlines()]
    assert [(r["kind"], Path(r["path"]).name) for r in records] == [
        ("folder", ".cache"),
        ("file", "notes.txt~"),
    ]


def test_permanent_delete_removes_a_folder_tree_and_nothing_else(tree, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    root, items = tree
    outside = root.parent / "outside.txt"
    outside.write_text("untouched")
    (root / ".cache" / "link").symlink_to(outside)  # a link inside must not be followed
    items = {i.path.name: i for i in scan_hidden(root, open_files_fn=set).items}
    plan = plan_hidden_actions(pick(items, ".cache", "~$lock.docx"), DeleteMode.PERMANENT)
    summary = execute(plan, log_path=tmp_path / "a.log")
    assert summary.done == 2
    assert not (root / ".cache").exists() and not (root / "~$lock.docx").exists()
    assert outside.read_text() == "untouched" and (root / "keep.txt").exists()


def test_changed_since_scan_is_skipped(tree, trash: Trash, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    root, items = tree
    plan = plan_hidden_actions(pick(items, "notes.txt~", ".cache", "~$lock.docx"))
    (root / ".cache" / "new.bin").write_text("added after the scan")  # folder mtime changes
    (root / "notes.txt~").write_text("edited so the size differs")
    (root / "~$lock.docx").unlink()
    (root / "~$lock.docx").mkdir()  # type changed: file -> folder
    summary = execute(plan, trash=trash, log_path=tmp_path / "a.log")
    assert [r.status for r in summary.results] == [Status.CHANGED] * 3
    assert trash.calls == [] and (root / ".cache" / "new.bin").exists()


def test_dry_run_touches_nothing(tree, trash: Trash, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    root, items = tree
    before = sorted(str(p) for p in root.rglob("*"))
    summary = execute(
        plan_hidden_actions(pick(items, ".cache")),
        dry_run=True,
        trash=trash,
        log_path=tmp_path / "a.log",
    )
    assert summary.done == 1 and trash.calls == [] and not (tmp_path / "a.log").exists()
    assert sorted(str(p) for p in root.rglob("*")) == before


def test_protected_items_need_explicit_allowance(tree) -> None:  # type: ignore[no-untyped-def]
    _, items = tree
    bashrc = items[".bashrc"]
    assert bashrc.protected
    with pytest.raises(PlanRefused, match="protected list"):
        plan_hidden_actions([bashrc])
    plan = plan_hidden_actions([bashrc], allow_protected=[bashrc.path])
    assert len(plan.items) == 1


def test_protected_ancestor_counts_too(make_tree: MakeTree) -> None:
    root = make_tree({".ssh/.hidden~": "k"}) / ".ssh"
    (item,) = scan_hidden(root, open_files_fn=set).items
    with pytest.raises(PlanRefused, match="protected list"):
        plan_hidden_actions([item])


def test_root_and_home_are_always_refused(isolated_home: Path, tree) -> None:  # type: ignore[no-untyped-def]
    _, items = tree
    base = items["notes.txt~"]
    for victim in (Path("/"), isolated_home, isolated_home.parent):
        forged = HiddenItem(victim, True, 0, "x", mtime_ns=0)
        with pytest.raises(PlanRefused, match="root or home"):
            plan_hidden_actions([forged, base], allow_protected=[victim])


def test_hardlink_mode_is_refused(tree) -> None:  # type: ignore[no-untyped-def]
    _, items = tree
    with pytest.raises(PlanRefused, match="hard links"):
        plan_hidden_actions(pick(items, "notes.txt~"), DeleteMode.HARDLINK)


def test_trash_failure_is_reported_and_folder_is_kept(
    tree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    root, items = tree

    def boom(path: str) -> None:
        raise OSError(13, "Permission denied")

    def forbidden(*a: object, **k: object) -> None:
        raise AssertionError("fell back to a permanent delete")

    monkeypatch.setattr(shutil, "rmtree", forbidden)
    monkeypatch.setattr(os, "unlink", forbidden)
    summary = execute(
        plan_hidden_actions(pick(items, ".cache")), trash=boom, log_path=tmp_path / "a.log"
    )
    assert summary.results[0].status is Status.FAILED and (root / ".cache" / "d.bin").exists()


def test_duplicate_items_are_planned_once(tree) -> None:  # type: ignore[no-untyped-def]
    _, items = tree
    plan = plan_hidden_actions(pick(items, "notes.txt~", "notes.txt~"))
    assert len(plan.items) == 1
