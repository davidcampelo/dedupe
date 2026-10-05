from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from dedupe.core import actions
from dedupe.core.actions import (
    ActionError,
    PlanRefused,
    Status,
    execute,
    plan_actions,
)
from dedupe.core.models import CancelToken, DeleteMode, DuplicateGroup, Progress, ScanOptions
from dedupe.core.pipeline import run_scan

MakeTree = Callable[..., Path]


class FakeTrash:
    """Stands in for send2trash: moves files into a folder inside tmp_path."""

    def __init__(self, bin_dir: Path) -> None:
        self.bin = bin_dir
        self.bin.mkdir(parents=True, exist_ok=True)
        self.calls: list[str] = []

    def __call__(self, path: str) -> None:
        self.calls.append(path)
        shutil.move(path, self.bin / f"{len(self.calls)}-{Path(path).name}")


@pytest.fixture
def trash(tmp_path: Path) -> FakeTrash:
    return FakeTrash(tmp_path / "fake-trash")


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "actions.log"


def scan(root: Path, **kw: object) -> tuple[DuplicateGroup, ...]:
    return run_scan(root, ScanOptions(exclude=(), **kw)).groups  # type: ignore[arg-type]


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(p): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def dups(make_tree: MakeTree) -> tuple[Path, tuple[DuplicateGroup, ...]]:
    root = make_tree(
        {"a/x.txt": "same", "b/x.txt": "same", "c/x.txt": "same", "keep.txt": "unique"}
    )
    return root, scan(root)


def selection(root: Path, *rel: str) -> list[Path]:
    return [root / r for r in rel]


# -- planning ---------------------------------------------------------------------------------


def test_selecting_every_copy_is_refused_before_any_mutation(
    dups: tuple[Path, tuple[DuplicateGroup, ...]], trash: FakeTrash, log_path: Path
) -> None:
    root, groups = dups
    before = snapshot(root)
    with pytest.raises(PlanRefused, match="every copy"):
        plan_actions(groups, selection(root, "a/x.txt", "b/x.txt", "c/x.txt"))
    assert snapshot(root) == before and trash.calls == [] and not log_path.exists()


def test_all_but_one_is_allowed(dups: tuple[Path, tuple[DuplicateGroup, ...]]) -> None:
    root, groups = dups
    plan = plan_actions(groups, selection(root, "b/x.txt", "c/x.txt"))
    assert [i.entry.path.parent.name for i in plan.items] == ["b", "c"]
    assert plan.total_size == 8 and plan.items[0].keepers[0].path == root / "a/x.txt"


def test_unknown_and_protected_paths_are_refused_with_all_reasons(
    dups: tuple[Path, tuple[DuplicateGroup, ...]],
) -> None:
    root, groups = dups
    with pytest.raises(PlanRefused) as exc:
        plan_actions(
            groups,
            selection(root, "keep.txt", "a/x.txt", "b/x.txt", "c/x.txt"),
            protected_folders=[str(root / "b")],
        )
    text = " | ".join(exc.value.reasons)
    assert "keep.txt: not part of any duplicate group" in text
    assert "b/x.txt: is in a protected folder" in text
    assert "every copy" in text


def test_hardlink_possible_reported_and_cross_device_refused(
    dups: tuple[Path, tuple[DuplicateGroup, ...]],
) -> None:
    root, groups = dups
    plan = plan_actions(groups, selection(root, "b/x.txt"))
    assert plan.hardlink_possible
    g = groups[0]
    moved = replace(g, files=(g.files[0], replace(g.files[1], device=g.files[1].device + 1)))
    plan = plan_actions([moved], [moved.files[1].path], DeleteMode.TRASH)
    assert not plan.hardlink_possible and "different filesystem" in plan.hardlink_reason
    with pytest.raises(PlanRefused, match="hard links are not possible"):
        plan_actions([moved], [moved.files[1].path], DeleteMode.HARDLINK)


def test_empty_selection_plan(dups: tuple[Path, tuple[DuplicateGroup, ...]]) -> None:
    plan = plan_actions(dups[1], [])
    assert plan.items == () and not plan.hardlink_possible


# -- trash execution --------------------------------------------------------------------------


def test_trash_moves_files_and_logs(
    dups: tuple[Path, tuple[DuplicateGroup, ...]], trash: FakeTrash, log_path: Path
) -> None:
    root, groups = dups
    plan = plan_actions(groups, selection(root, "b/x.txt", "c/x.txt"))
    events: list[Progress] = []
    summary = execute(plan, progress=events.append, log_path=log_path, trash=trash)
    assert summary.done == 2 and summary.freed == 8
    assert (root / "a/x.txt").exists() and not (root / "b/x.txt").exists()
    assert [e.done for e in events][:2] == [0, 1] and events[-1].done == 2
    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [(r["action"], Path(r["path"]).parent.name, r["size"], r["status"]) for r in lines] == [
        ("trash", "b", 4, "done"),
        ("trash", "c", 4, "done"),
    ]
    assert lines[0]["hash"] == groups[0].hash and "ts" in lines[0]


def test_default_trash_backend_is_send2trash(
    dups: tuple[Path, tuple[DuplicateGroup, ...]], monkeypatch: pytest.MonkeyPatch, log_path: Path
) -> None:
    root, groups = dups
    called: list[str] = []
    monkeypatch.setattr(actions.send2trash, "send2trash", called.append)
    execute(plan_actions(groups, selection(root, "b/x.txt")), log_path=log_path)
    assert called == [str(root / "b/x.txt")]


def test_changed_since_scan_is_skipped(
    dups: tuple[Path, tuple[DuplicateGroup, ...]], trash: FakeTrash, log_path: Path
) -> None:
    root, groups = dups
    plan = plan_actions(groups, selection(root, "b/x.txt", "c/x.txt"))
    (root / "b/x.txt").write_text("edited!")  # different size
    st = (root / "c/x.txt").stat()
    os.utime(root / "c/x.txt", ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # new mtime
    summary = execute(plan, log_path=log_path, trash=trash)
    assert [r.status for r in summary.results] == [Status.CHANGED, Status.CHANGED]
    assert (root / "b/x.txt").exists() and (root / "c/x.txt").exists() and trash.calls == []


@pytest.mark.parametrize("how", ["deleted", "symlink", "replaced"])
def test_vanished_or_swapped_file_is_skipped(
    dups: tuple[Path, tuple[DuplicateGroup, ...]], trash: FakeTrash, log_path: Path, how: str
) -> None:
    root, groups = dups
    plan = plan_actions(groups, selection(root, "b/x.txt"))
    victim = root / "b/x.txt"
    victim.unlink()
    if how == "symlink":
        victim.symlink_to(root / "a/x.txt")
    elif how == "replaced":
        victim.write_text("same")  # same size and content, new inode
    summary = execute(plan, log_path=log_path, trash=trash)
    assert summary.results[0].status is Status.CHANGED and trash.calls == []


def test_missing_or_changed_keeper_blocks_deletion(
    dups: tuple[Path, tuple[DuplicateGroup, ...]], trash: FakeTrash, log_path: Path
) -> None:
    root, groups = dups
    plan = plan_actions(groups, selection(root, "b/x.txt", "c/x.txt"))
    (root / "a/x.txt").write_text("not the same anymore")  # the only kept copy changed
    summary = execute(plan, log_path=log_path, trash=trash)
    assert [r.status for r in summary.results] == [Status.KEEPER_CHANGED] * 2
    assert (root / "b/x.txt").exists() and (root / "c/x.txt").exists()


def test_dry_run_touches_nothing_but_matches_real_summary(
    dups: tuple[Path, tuple[DuplicateGroup, ...]], trash: FakeTrash, log_path: Path
) -> None:
    root, groups = dups
    plan = plan_actions(groups, selection(root, "b/x.txt", "c/x.txt"))
    before = snapshot(root)
    dry = execute(plan, dry_run=True, log_path=log_path, trash=trash)
    assert snapshot(root) == before and trash.calls == [] and not log_path.exists()
    assert dry.dry_run and dry.done == 2 and dry.freed == 8
    real = execute(plan, log_path=log_path, trash=trash)
    assert [(r.path, r.size) for r in dry.results] == [(r.path, r.size) for r in real.results]
    assert (real.done, real.freed) == (dry.done, dry.freed)


def test_trash_failure_is_reported_and_never_falls_back_to_permanent_delete(
    dups: tuple[Path, tuple[DuplicateGroup, ...]], log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, groups = dups
    calls: list[str] = []

    def boom(path: str) -> None:
        calls.append(path)
        raise OSError(13, "Permission denied")

    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("permanent delete attempted after a trash failure")

    monkeypatch.setattr(os, "unlink", forbidden)
    monkeypatch.setattr(os, "remove", forbidden)
    summary = execute(
        plan_actions(groups, selection(root, "b/x.txt")), log_path=log_path, trash=boom
    )
    assert summary.results[0].status is Status.FAILED
    assert "Permission denied" in summary.results[0].detail
    assert calls == [str(root / "b/x.txt")] and (root / "b/x.txt").exists()


def test_unwritable_log_means_nothing_is_touched(
    dups: tuple[Path, tuple[DuplicateGroup, ...]], trash: FakeTrash, tmp_path: Path
) -> None:
    root, groups = dups
    bad_log = tmp_path / "logdir"
    bad_log.mkdir()  # a directory cannot be opened for appending
    with pytest.raises(ActionError, match="action log"):
        execute(plan_actions(groups, selection(root, "b/x.txt")), log_path=bad_log, trash=trash)
    assert (root / "b/x.txt").exists() and trash.calls == []


def test_cancel_between_files(
    dups: tuple[Path, tuple[DuplicateGroup, ...]], trash: FakeTrash, log_path: Path
) -> None:
    root, groups = dups
    token = CancelToken()
    plan = plan_actions(groups, selection(root, "b/x.txt", "c/x.txt"))

    def progress(p: Progress) -> None:
        if p.done == 1:
            token.cancel()

    summary = execute(plan, progress=progress, cancel=token, log_path=log_path, trash=trash)
    assert [r.status for r in summary.results] == [Status.DONE, Status.NOT_RUN]
    assert (root / "c/x.txt").exists()


def test_log_write_failure_becomes_a_warning(
    dups: tuple[Path, tuple[DuplicateGroup, ...]],
    trash: FakeTrash,
    log_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, groups = dups

    def broken(self: actions.ActionLog, record: dict[str, object]) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(actions.ActionLog, "write", broken)
    summary = execute(
        plan_actions(groups, selection(root, "b/x.txt")), log_path=log_path, trash=trash
    )
    assert summary.done == 1 and "No space left" in summary.warnings[0]


# -- permanent delete and hard links (task 10) ------------------------------------------------


def test_permanent_delete_removes_only_planned_files_and_logs_each(
    dups: tuple[Path, tuple[DuplicateGroup, ...]], log_path: Path
) -> None:
    root, groups = dups
    plan = plan_actions(groups, selection(root, "b/x.txt", "c/x.txt"), DeleteMode.PERMANENT)
    summary = execute(plan, log_path=log_path)
    assert summary.done == 2
    assert {p.name for p in root.rglob("*") if p.is_file()} == {"x.txt", "keep.txt"}
    assert (root / "a/x.txt").exists() and (root / "keep.txt").exists()
    actions_logged = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [(r["action"], r["status"]) for r in actions_logged] == [("permanent", "done")] * 2


def test_hardlink_replacement(
    dups: tuple[Path, tuple[DuplicateGroup, ...]], log_path: Path
) -> None:
    root, groups = dups
    plan = plan_actions(groups, selection(root, "b/x.txt", "c/x.txt"), DeleteMode.HARDLINK)
    summary = execute(plan, log_path=log_path)
    assert summary.done == 2
    inodes = {(root / d / "x.txt").stat().st_ino for d in "abc"}
    assert len(inodes) == 1
    assert all((root / d / "x.txt").read_text() == "same" for d in "abc")
    assert [p.name for p in root.rglob("*.tmp")] == []
    rec = json.loads(log_path.read_text().splitlines()[0])
    assert rec["action"] == "hardlink" and rec["linked_to"] == str(root / "a/x.txt")


def test_hardlink_failure_midway_leaves_no_temp_and_no_missing_path(
    dups: tuple[Path, tuple[DuplicateGroup, ...]], log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, groups = dups
    plan = plan_actions(groups, selection(root, "b/x.txt"), DeleteMode.HARDLINK)

    def boom(src: object, dst: object) -> None:
        raise OSError(5, "I/O error")

    monkeypatch.setattr(os, "replace", boom)
    summary = execute(plan, log_path=log_path)
    assert summary.results[0].status is Status.FAILED
    assert (root / "b/x.txt").read_text() == "same"
    assert (root / "b/x.txt").stat().st_ino != (root / "a/x.txt").stat().st_ino
    assert list(root.rglob("*.tmp")) == [] and len(list(root.rglob(".*"))) == 0


def test_hardlink_link_failure_leaves_everything_intact(
    dups: tuple[Path, tuple[DuplicateGroup, ...]], log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, groups = dups
    plan = plan_actions(groups, selection(root, "b/x.txt"), DeleteMode.HARDLINK)
    monkeypatch.setattr(os, "link", lambda *_: (_ for _ in ()).throw(OSError(1, "EPERM")))
    assert execute(plan, log_path=log_path).results[0].status is Status.FAILED
    assert (root / "b/x.txt").exists() and list(root.rglob("*.tmp")) == []


def test_hardlink_already_linked_is_a_noop(make_tree: MakeTree, log_path: Path) -> None:
    root = make_tree({"a": "same", "c": "same"})
    os.link(root / "a", root / "b")
    g = scan(root)[0]
    a = next(f for f in g.files if f.path.name == "a")
    b = replace(a, path=root / "b")  # same inode as a
    forged = replace(g, files=(a, b, *[f for f in g.files if f.path.name == "c"]))
    plan = plan_actions([forged], [root / "b"], DeleteMode.HARDLINK)
    summary = execute(plan, log_path=log_path)
    assert summary.results[0].status is Status.ALREADY_LINKED


def test_hardlink_dry_run_changes_nothing(
    dups: tuple[Path, tuple[DuplicateGroup, ...]], log_path: Path
) -> None:
    root, groups = dups
    before = snapshot(root)
    ino = (root / "b/x.txt").stat().st_ino
    summary = execute(
        plan_actions(groups, selection(root, "b/x.txt"), DeleteMode.HARDLINK), dry_run=True
    )
    assert summary.done == 1 and snapshot(root) == before
    assert (root / "b/x.txt").stat().st_ino == ino


# -- mutation checks: each guard is load-bearing ----------------------------------------------
# For every guard, a scenario that the guard blocks, run once normally (blocked) and once with
# the guard replaced by a no-op (the unsafe thing happens), proving a test depends on it.


def _scenario_last_copy(root: Path, groups: tuple[DuplicateGroup, ...]) -> bool:
    try:
        plan_actions(groups, selection(root, "a/x.txt", "b/x.txt", "c/x.txt"))
    except PlanRefused:
        return False
    return True


def _scenario_protected(root: Path, groups: tuple[DuplicateGroup, ...]) -> bool:
    try:
        plan_actions(groups, selection(root, "b/x.txt"), protected_folders=[str(root / "b")])
    except PlanRefused:
        return False
    return True


def _scenario_unknown(root: Path, groups: tuple[DuplicateGroup, ...]) -> bool:
    try:
        plan_actions(groups, selection(root, "keep.txt"))
    except PlanRefused:
        return False
    return True


def _scenario_hardlink(root: Path, groups: tuple[DuplicateGroup, ...]) -> bool:
    g = groups[0]
    moved = replace(g, files=(g.files[0], replace(g.files[1], device=g.files[1].device + 1)))
    try:
        plan_actions([moved], [moved.files[1].path], DeleteMode.HARDLINK)
    except PlanRefused:
        return False
    return True


def _exec_scenario(
    mutate: Callable[[Path], None],
) -> Callable[[Path, tuple[DuplicateGroup, ...]], bool]:
    def run(root: Path, groups: tuple[DuplicateGroup, ...]) -> bool:
        plan = plan_actions(groups, selection(root, "b/x.txt"))
        mutate(root)
        calls: list[str] = []
        execute(plan, log_path=root.parent / "mut.log", trash=calls.append)
        return bool(calls)  # True = the file was handed to the trash

    return run


def _edit_victim(root: Path) -> None:
    (root / "b/x.txt").write_text("edited!")


def _break_keeper(root: Path) -> None:
    (root / "a/x.txt").write_text("edited!")
    (root / "c/x.txt").unlink()


MUTATIONS = [
    ("_check_last_copy", "refuse-all", _scenario_last_copy, lambda *a: None),
    ("_check_protected", "protected", _scenario_protected, lambda *a: None),
    ("_check_known_paths", "unknown", _scenario_unknown, lambda *a: None),
    ("_check_hardlink_feasible", "hardlink", _scenario_hardlink, lambda *a: ""),
    ("_verify_unchanged", "changed", _exec_scenario(_edit_victim), lambda *a: None),
    ("_verify_keeper", "keeper", _exec_scenario(_break_keeper), lambda keepers: keepers[0]),
]


def _fresh(base: Path, name: str) -> tuple[Path, tuple[DuplicateGroup, ...]]:
    root = base / name
    for d in "abc":
        (root / d).mkdir(parents=True)
        (root / d / "x.txt").write_text("same")
    return root, scan(root)


@pytest.mark.parametrize(("guard", "label", "scenario", "neutered"), MUTATIONS)
def test_each_guard_is_load_bearing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    guard: str,
    label: str,
    scenario: Callable[[Path, tuple[DuplicateGroup, ...]], bool],
    neutered: Callable[..., object],
) -> None:
    root, groups = _fresh(tmp_path, "guarded")
    assert scenario(root, groups) is False, f"{label}: guard should block the scenario"
    monkeypatch.setattr(actions, guard, neutered)
    root, groups = _fresh(tmp_path, "mutated")  # fresh tree: execute-time scenarios edit files
    assert scenario(root, groups) is True, f"{label}: disabling {guard} must change the outcome"
