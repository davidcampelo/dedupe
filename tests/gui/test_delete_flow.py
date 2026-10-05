from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog
from pytestqt.qtbot import QtBot

from dedupe.core import actions
from dedupe.core.actions import ActionSummary, PlanRefused, plan_actions
from dedupe.core.models import DeleteMode
from dedupe.core.settings import Settings
from dedupe.gui import workers
from dedupe.gui.delete_dialog import (
    ACK_TEXT,
    SORT_ROLE,
    DeleteChoice,
    DeleteDialog,
    format_summary,
)
from dedupe.gui.main_window import MainWindow
from tests.gui.conftest import UiWatchdog

MakeTree = Callable[..., Path]


class Trash:
    def __init__(self, bin_dir: Path) -> None:
        bin_dir.mkdir(parents=True, exist_ok=True)
        self.bin, self.calls = bin_dir, 0

    def __call__(self, path: str) -> None:
        self.calls += 1
        shutil.move(path, self.bin / f"{self.calls}-{Path(path).name}")


class Harness:
    def __init__(self, qtbot: QtBot, tmp_path: Path) -> None:
        self.window = MainWindow(Settings(exclude=(), use_cache=False))
        qtbot.addWidget(self.window)
        self.window.show()
        self.qtbot = qtbot
        self.trash = Trash(tmp_path / "bin")
        self.window.trash_backend = self.trash
        self.window.log_path = tmp_path / "logs" / "actions.log"
        self.summaries: list[str] = []
        self.refusals: list[list[str]] = []
        self.window.show_summary = self.summaries.append  # type: ignore[method-assign]
        self.window.show_refusal = lambda reasons: self.refusals.append(list(reasons))  # type: ignore[method-assign]
        self.choice: DeleteChoice | None = DeleteChoice(DeleteMode.TRASH, False)
        self.plans: list[object] = []

        def confirm(plan: object) -> DeleteChoice | None:
            self.plans.append(plan)
            return self.choice

        self.window.confirm_delete = confirm  # type: ignore[method-assign,assignment]

    def scan(self, root: Path) -> None:
        self.window.set_folder(root)
        with self.qtbot.waitSignal(self.window.scan_finished, timeout=30000):
            self.window.start_scan()
        self.qtbot.waitUntil(lambda: not self.window.duplicates_tab.model.loading, timeout=30000)

    def delete(self) -> ActionSummary | None:
        """Run request_delete and wait until the flow ends (summary, refusal or rejection)."""
        w = self.window
        before = len(self.summaries) + len(self.refusals) + len(self.plans)
        w.request_delete()
        self.qtbot.waitUntil(
            lambda: (
                not w.busy and len(self.summaries) + len(self.refusals) + len(self.plans) > before
            ),
            timeout=60000,
        )
        return None


@pytest.fixture
def harness(qtbot: QtBot, tmp_path: Path) -> Harness:
    return Harness(qtbot, tmp_path)


def make_dups(make_tree: MakeTree) -> Path:
    return make_tree(
        {"a/x.txt": "dupe", "b/x.txt": "dupe", "c/x.txt": "dupe", "keep.txt": "unique"}
    )


# -- dialog -----------------------------------------------------------------------------------


def plan_for(make_tree: MakeTree, n_files: int = 3) -> actions.ActionPlan:
    from dedupe.core.pipeline import run_scan

    root = make_tree({f"d{i}/x.txt": "same" for i in range(n_files)})
    groups = run_scan(root).groups
    return plan_actions(groups, [root / f"d{i}/x.txt" for i in range(1, n_files)])


def test_dialog_defaults_to_trash_and_lists_the_selection(
    qtbot: QtBot, make_tree: MakeTree
) -> None:
    plan = plan_for(make_tree, 3)
    dlg = DeleteDialog(plan)
    qtbot.addWidget(dlg)
    assert dlg.mode is DeleteMode.TRASH and dlg.ok_button.isEnabled()
    assert "2 files" in dlg.summary_label.text() and "8 B" in dlg.summary_label.text()
    assert dlg.proxy.rowCount() == 2
    assert not dlg.ack_box.isVisibleTo(dlg) and dlg.ok_button.text() == "Move to Trash"
    assert dlg.hardlink_radio.isEnabled()
    assert dlg.choice == DeleteChoice(DeleteMode.TRASH, False)
    dlg.dry_run_box.setChecked(True)
    assert dlg.choice.dry_run


def test_dialog_lists_every_file_and_sorts_by_each_column(
    qtbot: QtBot, make_tree: MakeTree
) -> None:
    n = 30
    dlg = DeleteDialog(plan_for(make_tree, n))
    qtbot.addWidget(dlg)
    assert dlg.proxy.rowCount() == n - 1  # no truncation
    for col in range(4):
        for order in (Qt.SortOrder.AscendingOrder, Qt.SortOrder.DescendingOrder):
            dlg.paths_view.sortByColumn(col, order)
            keys = [dlg.proxy.index(r, col).data(SORT_ROLE) for r in range(dlg.proxy.rowCount())]
            assert keys == sorted(keys, reverse=order is Qt.SortOrder.DescendingOrder)


def test_permanent_needs_the_checkbox_and_switching_modes_clears_it(
    qtbot: QtBot, make_tree: MakeTree
) -> None:
    dlg = DeleteDialog(plan_for(make_tree))
    qtbot.addWidget(dlg)
    dlg.show()
    dlg.permanent_radio.setChecked(True)
    assert dlg.ack_box.isVisible() and dlg.ack_box.text() == ACK_TEXT
    assert not dlg.ok_button.isEnabled()
    dlg.accept()  # Enter / programmatic accept must not bypass the checkbox
    assert dlg.result() != QDialog.DialogCode.Accepted
    dlg.ack_box.setChecked(True)
    assert dlg.ok_button.isEnabled() and dlg.ok_button.text() == "Delete permanently"
    dlg.trash_radio.setChecked(True)  # switch away...
    assert dlg.ok_button.isEnabled()
    dlg.permanent_radio.setChecked(True)  # ...and back: acknowledgement is gone
    assert not dlg.ack_box.isChecked() and not dlg.ok_button.isEnabled()
    dlg.ack_box.setChecked(True)
    dlg.accept()
    assert dlg.result() == QDialog.DialogCode.Accepted and dlg.mode is DeleteMode.PERMANENT


def test_hardlink_only_offered_when_the_plan_allows_it(qtbot: QtBot, make_tree: MakeTree) -> None:
    from dataclasses import replace

    plan = plan_for(make_tree)
    blocked = replace(plan, hardlink_possible=False, hardlink_reason="different filesystem")
    dlg = DeleteDialog(blocked, default_mode=DeleteMode.HARDLINK)
    qtbot.addWidget(dlg)
    assert (
        not dlg.hardlink_radio.isEnabled()
        and "different filesystem" in dlg.hardlink_radio.toolTip()
    )
    assert dlg.mode is DeleteMode.TRASH  # fell back to the safe default
    dlg2 = DeleteDialog(plan, default_mode=DeleteMode.HARDLINK)
    qtbot.addWidget(dlg2)
    assert dlg2.mode is DeleteMode.HARDLINK and dlg2.ok_button.text() == "Replace with hard links"


def test_format_summary_lists_problems(make_tree: MakeTree, tmp_path: Path) -> None:
    plan = plan_for(make_tree, 3)
    (plan.items[0].entry.path).write_text("changed!")
    summary = actions.execute(plan, trash=Trash(tmp_path / "t"), log_path=tmp_path / "l.log")
    text = format_summary(summary)
    assert "1 files moved to Trash" in text and "changed since scan" in text
    dry = actions.execute(plan_for(make_tree, 3), dry_run=True)
    assert format_summary(dry).startswith("Dry run: nothing was changed.")


# -- flow -------------------------------------------------------------------------------------


def test_rejecting_the_dialog_leaves_everything_intact(
    harness: Harness, make_tree: MakeTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_dups(make_tree)
    harness.scan(root)
    calls: list[object] = []
    monkeypatch.setattr(workers, "execute", lambda *a, **k: calls.append(a))
    harness.choice = None
    harness.delete()
    assert calls == [] and harness.trash.calls == 0 and harness.summaries == []
    assert all(p.exists() for p in root.rglob("*.txt"))
    assert harness.window.duplicates_tab.model.selected_count == 2  # selection untouched
    assert not (harness.window.log_path or Path("/nonexistent")).exists()


def test_accepting_trashes_logs_and_updates_the_model(
    harness: Harness, make_tree: MakeTree
) -> None:
    root = make_dups(make_tree)
    harness.scan(root)
    model = harness.window.duplicates_tab.model
    assert model.selected_count == 2
    harness.delete()
    assert harness.trash.calls == 2 and len(harness.summaries) == 1
    assert "2 files moved to Trash" in harness.summaries[0]
    remaining = [p for p in root.rglob("x.txt")]
    assert len(remaining) == 1
    harness.qtbot.waitUntil(
        lambda: model.group_count == 0 and not model.loading, timeout=5000
    )  # one copy left: no longer a group
    assert model.selected_count == 0
    assert harness.window.groups_label.text() == "Duplicate groups: 0"
    log = [json.loads(line) for line in harness.window.log_path.read_text().splitlines()]  # type: ignore[union-attr]
    assert [r["action"] for r in log] == ["trash", "trash"]


def test_dry_run_changes_nothing(harness: Harness, make_tree: MakeTree) -> None:
    root = make_dups(make_tree)
    harness.scan(root)
    harness.choice = DeleteChoice(DeleteMode.TRASH, True)
    harness.delete()
    assert harness.trash.calls == 0 and len(list(root.rglob("x.txt"))) == 3
    assert harness.summaries[0].startswith("Dry run")
    assert harness.window.duplicates_tab.model.group_count == 1


def test_hardlink_mode_through_the_window(harness: Harness, make_tree: MakeTree) -> None:
    root = make_dups(make_tree)
    harness.scan(root)
    harness.choice = DeleteChoice(DeleteMode.HARDLINK, False)
    harness.delete()
    assert len({p.stat().st_ino for p in root.rglob("x.txt")}) == 1
    assert len(list(root.rglob("x.txt"))) == 3 and harness.trash.calls == 0


def test_selecting_every_copy_is_refused_before_the_dialog(
    harness: Harness, make_tree: MakeTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_dups(make_tree)
    harness.scan(root)
    model = harness.window.duplicates_tab.model
    for p in root.rglob("x.txt"):
        model.set_checked(p, True)
    executed: list[object] = []
    monkeypatch.setattr(workers, "execute", lambda *a, **k: executed.append(a))
    harness.delete()
    assert harness.plans == [] and executed == [] and harness.trash.calls == 0
    assert "every copy" in harness.refusals[0][0]
    assert len(list(root.rglob("x.txt"))) == 3


def test_request_delete_is_ignored_when_nothing_selected_or_busy(
    harness: Harness, make_tree: MakeTree
) -> None:
    root = make_dups(make_tree)
    harness.scan(root)
    model = harness.window.duplicates_tab.model
    model.clear_selection()
    harness.window.request_delete()
    assert not harness.window.busy and harness.plans == []
    model.select_all_suggested()
    harness.window.job = object()  # type: ignore[assignment]
    harness.window.request_delete()
    assert harness.plans == []
    harness.window.job = None


def test_refusal_object_from_plan(make_tree: MakeTree) -> None:
    with pytest.raises(PlanRefused):
        plan = plan_for(make_tree, 2)
        plan_actions([], [plan.items[0].entry.path])


def test_trashing_5k_files_never_stalls_the_gui(
    harness: Harness, make_tree: MakeTree, ui_watchdog: UiWatchdog
) -> None:
    root = make_tree(
        {f"d{i % 50}/g{i}-{c}.txt": f"content {i}" for i in range(2500) for c in range(2)}
    )
    harness.scan(root)
    model = harness.window.duplicates_tab.model
    assert model.group_count == 2500 and model.selected_count == 2500
    with ui_watchdog.watch():
        harness.delete()
        harness.qtbot.waitUntil(lambda: model.group_count == 0 and not model.loading, timeout=30000)
    assert harness.trash.calls == 2500
    assert len(list(root.rglob("*.txt"))) == 2500
    assert model.group_count == 0


def test_dry_run_of_5k_leaves_files_on_disk(
    harness: Harness, make_tree: MakeTree, ui_watchdog: UiWatchdog
) -> None:
    root = make_tree(
        {f"d{i % 50}/g{i}-{c}.txt": f"content {i}" for i in range(2500) for c in range(2)}
    )
    harness.scan(root)
    harness.choice = DeleteChoice(DeleteMode.TRASH, True)
    with ui_watchdog.watch():
        harness.delete()
    assert len(list(root.rglob("*.txt"))) == 5000 and harness.trash.calls == 0
