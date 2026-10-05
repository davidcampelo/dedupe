from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from dedupe.core.hidden import scan_hidden
from dedupe.core.models import DeleteMode
from dedupe.core.settings import Settings
from dedupe.gui.delete_dialog import DeleteChoice
from dedupe.gui.hidden_files_view import COL_NOTES, COL_PATH, COL_SIZE, HiddenFilesTab
from dedupe.gui.main_window import MainWindow
from dedupe.gui.skipped_view import SkippedTab
from dedupe.gui.workers import HiddenScanJob
from tests.gui.conftest import UiWatchdog

MakeTree = Callable[..., Path]


def items_for(root: Path):  # type: ignore[no-untyped-def]
    return scan_hidden(root, open_files_fn=set).items


def row_of(tab: HiddenFilesTab, name: str) -> int:
    return next(i for i, it in enumerate(tab.model.items) if it.path.name == name)


def checked(tab: HiddenFilesTab, name: str) -> bool:
    idx = tab.model.index(row_of(tab, name), COL_PATH)
    return tab.model.data(idx, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked


@pytest.fixture
def tab(qtbot: QtBot) -> HiddenFilesTab:
    t = HiddenFilesTab()
    qtbot.addWidget(t)
    t.show()
    return t


@pytest.fixture
def populated(tab: HiddenFilesTab, make_tree: MakeTree) -> HiddenFilesTab:
    root = make_tree(
        {
            "~$x.docx": "lock!",
            "notes.txt~": "12345678",
            ".bashrc": "rc",
            ".mything": "data",
            "~tilde": "t",
            "keep.txt": "k",
        }
    )
    tab.set_items(items_for(root))
    return tab


def test_preselection_and_protected_rows(populated: HiddenFilesTab) -> None:
    assert checked(populated, "~$x.docx") and checked(populated, "notes.txt~")
    assert not checked(populated, ".bashrc") and not checked(populated, ".mything")
    assert not checked(populated, "~tilde")
    notes = populated.model.data(populated.model.index(row_of(populated, ".bashrc"), COL_NOTES))
    assert "Protected" in notes


def test_reclaimable_total_equals_selected_sizes(populated: HiddenFilesTab) -> None:
    m = populated.model
    expected = sum(i.size for i in m.selected_items())
    assert m.selected_size == expected == 5 + 8
    assert "2 of 5 items selected" in populated.total_label.text()
    assert "13 B" in populated.total_label.text()
    populated.confirm_risky = lambda item: True  # type: ignore[method-assign]
    m.confirm_risky = lambda item: True
    assert m.set_checked(row_of(populated, ".mything"), True)
    assert m.selected_size == sum(i.size for i in m.selected_items()) == 5 + 8 + 4
    assert m.set_checked(row_of(populated, "~$x.docx"), False)
    assert m.selected_size == sum(i.size for i in m.selected_items())
    populated.select_none_button.click()
    assert (
        m.selected_count == 0 and m.selected_size == 0 and not populated.delete_button.isEnabled()
    )
    populated.select_suggested_button.click()
    assert m.selected_count == 2 and populated.delete_button.isEnabled()


def test_ticking_a_protected_item_asks_and_can_be_declined(populated: HiddenFilesTab) -> None:
    asked: list[str] = []
    answers = iter([False, True])

    def confirm(item):  # type: ignore[no-untyped-def]
        asked.append(item.path.name)
        return next(answers)

    populated.model.confirm_risky = confirm
    row = row_of(populated, ".bashrc")
    idx = populated.model.index(row, COL_PATH)
    assert not populated.model.setData(idx, Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
    assert not checked(populated, ".bashrc") and not populated.warning.isVisible()
    assert populated.model.setData(idx, Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
    assert checked(populated, ".bashrc") and asked == [".bashrc", ".bashrc"]
    assert populated.warning.isVisibleTo(populated) and "Warning" in populated.warning.text()
    # unticking never asks
    assert populated.model.setData(idx, Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
    assert asked == [".bashrc", ".bashrc"] and not populated.warning.isVisibleTo(populated)


def test_ticking_inside_a_home_dot_folder_asks(tab: HiddenFilesTab, isolated_home: Path) -> None:
    (isolated_home / ".ssh").mkdir()
    (isolated_home / ".ssh" / "id_rsa").write_text("key")
    (isolated_home / ".cache").mkdir()
    (isolated_home / ".cache" / "x").write_text("cache")
    tab.set_items(items_for(isolated_home))
    asked: list[str] = []
    tab.model.confirm_risky = lambda item: asked.append(item.path.name) or False
    for name in (".ssh", ".cache"):
        tab.model.set_checked(row_of(tab, name), True)
    assert asked == [".ssh", ".cache"]  # both are top-level dot folders of the home directory
    assert tab.model.selected_count == 0


def test_model_basics(populated: HiddenFilesTab) -> None:
    m = populated.model
    assert m.rowCount() == 5 and m.columnCount() == 5
    assert m.headerData(COL_SIZE, Qt.Orientation.Horizontal) == "Size"
    assert not m.set_checked(99, True) and not m.set_checked(
        0, m.index(0, 0).data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
    )
    assert m.data(m.index(0, COL_SIZE), Qt.ItemDataRole.TextAlignmentRole)
    assert m.data(m.index(99, 0)) is None


def test_skipped_tab_lists_entries(qtbot: QtBot) -> None:
    from dedupe.core.models import SkippedEntry

    t = SkippedTab()
    qtbot.addWidget(t)
    assert t.summary.text() == "Nothing was skipped."
    t.set_entries([SkippedEntry("/a", "permission denied"), SkippedEntry("/b", "vanished")])
    assert t.model.rowCount() == 2 and t.model.columnCount() == 2
    assert (
        t.model.data(t.model.index(0, 0)) == "/a"
        and t.model.data(t.model.index(1, 1)) == "vanished"
    )
    assert (
        "2 items" in t.summary.text()
        and t.model.headerData(1, Qt.Orientation.Horizontal) == "Reason"
    )
    assert t.model.data(t.model.index(5, 0)) is None


# -- window flow ----------------------------------------------------------------------------------


class Trash:
    def __init__(self, base: Path) -> None:
        base.mkdir(parents=True, exist_ok=True)
        self.base, self.calls = base, 0

    def __call__(self, path: str) -> None:
        self.calls += 1
        shutil.move(path, self.base / f"{self.calls}-{Path(path).name}")


@pytest.fixture
def window(qtbot: QtBot, tmp_path: Path) -> MainWindow:
    w = MainWindow(Settings(exclude=(), use_cache=False))
    qtbot.addWidget(w)
    w.show()
    w.trash_backend = Trash(tmp_path / "bin")
    w.log_path = tmp_path / "logs" / "actions.log"
    w.show_summary = lambda text: None  # type: ignore[method-assign]
    w.confirm_delete = lambda plan: DeleteChoice(DeleteMode.TRASH, False)  # type: ignore[method-assign]
    return w


def scan(qtbot: QtBot, window: MainWindow, root: Path) -> None:
    window.set_folder(root)
    with qtbot.waitSignal(window.scan_finished, timeout=20000):
        window.start_scan()


def wait_action(qtbot: QtBot, window: MainWindow, start: Callable[[], None]) -> None:
    with qtbot.waitSignal(window.action_finished, timeout=20000):
        start()
    qtbot.waitUntil(lambda: not window.busy, timeout=5000)


def test_scan_fills_the_hidden_and_skipped_tabs(
    qtbot: QtBot, window: MainWindow, make_tree: MakeTree
) -> None:
    root = make_tree(
        {"a.txt": "dup", "b.txt": "dup", "~$x.docx": "lock", "notes~": "bak", ".bashrc": "rc"}
    )
    scan(qtbot, window, root)
    tab = window.hidden_tab
    assert {i.path.name for i in tab.model.items} == {"~$x.docx", "notes~", ".bashrc"}
    assert tab.model.selected_count == 2 and not window.busy
    assert "hidden or temporary" in window.stage_label.text()
    assert window.skipped_tab.model.rowCount() == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_unreadable_folders_show_in_the_skipped_tab(
    qtbot: QtBot, window: MainWindow, make_tree: MakeTree
) -> None:
    root = make_tree({"ok.txt": "x", "locked/f.txt": "y"})
    (root / "locked").chmod(0)
    try:
        scan(qtbot, window, root)
    finally:
        (root / "locked").chmod(0o755)
    paths = [
        window.skipped_tab.model.data(window.skipped_tab.model.index(r, 0))
        for r in range(window.skipped_tab.model.rowCount())
    ]
    assert (
        str(root / "locked") in paths
        and window.skipped_tab.model.data(window.skipped_tab.model.index(0, 1))
        == "permission denied"
    )


def test_deleting_the_preselection_trashes_it_and_keeps_protected_files(
    qtbot: QtBot, window: MainWindow, make_tree: MakeTree
) -> None:
    root = make_tree({"~$x.docx": "lock", "notes~": "bak", ".bashrc": "rc", "keep.txt": "k"})
    scan(qtbot, window, root)
    wait_action(qtbot, window, window.hidden_tab.delete_button.click)
    assert not (root / "~$x.docx").exists() and not (root / "notes~").exists()
    assert (root / ".bashrc").exists() and (root / "keep.txt").exists()
    assert [i.path.name for i in window.hidden_tab.model.items] == [".bashrc"]
    log = window.log_path.read_text().splitlines()  # type: ignore[union-attr]
    assert len(log) == 2


def test_a_confirmed_protected_item_can_be_deleted_but_a_declined_one_cannot(
    qtbot: QtBot, window: MainWindow, make_tree: MakeTree
) -> None:
    root = make_tree({"notes~": "bak", ".bashrc": "rc"})
    scan(qtbot, window, root)
    tab = window.hidden_tab
    tab.model.confirm_risky = lambda item: False
    assert not tab.model.set_checked(row_of(tab, ".bashrc"), True)
    tab.model.confirm_risky = lambda item: True
    assert tab.model.set_checked(row_of(tab, ".bashrc"), True)
    wait_action(qtbot, window, tab.delete_button.click)
    assert not (root / ".bashrc").exists() and not (root / "notes~").exists()


def test_the_core_refuses_protected_items_even_if_the_gui_forgot_to_ask(
    qtbot: QtBot, window: MainWindow, make_tree: MakeTree
) -> None:
    root = make_tree({".bashrc": "rc"})
    scan(qtbot, window, root)
    refusals: list[list[str]] = []
    window.show_refusal = lambda reasons: refusals.append(list(reasons))  # type: ignore[method-assign]
    from dedupe.gui.main_window import DeleteRequest
    from dedupe.gui.workers import HiddenPlanJob

    items = window.hidden_tab.model.items
    window._delete_request = DeleteRequest("hidden", hidden_items=list(items), allow_protected=())
    window._begin(HiddenPlanJob(list(items), ()), "x", window._on_plan_finished)
    qtbot.waitUntil(lambda: bool(refusals), timeout=5000)
    assert "protected list" in refusals[0][0] and (root / ".bashrc").exists()


def test_dry_run_from_the_hidden_tab_changes_nothing(
    qtbot: QtBot, window: MainWindow, make_tree: MakeTree
) -> None:
    root = make_tree({"notes~": "bak"})
    scan(qtbot, window, root)
    window.confirm_delete = lambda plan: DeleteChoice(DeleteMode.TRASH, True)  # type: ignore[method-assign]
    wait_action(qtbot, window, window.hidden_tab.delete_button.click)
    assert (root / "notes~").exists() and window.hidden_tab.model.rowCount() == 1


def test_hidden_scan_of_a_50k_entry_tree_never_stalls_the_gui(
    qtbot: QtBot, make_tree: MakeTree, ui_watchdog: UiWatchdog
) -> None:
    root = make_tree({})
    for d in range(50):
        sub = root / f"d{d}"
        sub.mkdir()
        for i in range(1000):
            (sub / (f".h{i}" if i % 2 == 0 else f"n{i}")).write_bytes(b"x")
    tab = HiddenFilesTab()
    qtbot.addWidget(tab)
    tab.show()
    from dedupe.gui.workers import JobRunner

    runner = JobRunner(1)
    job = HiddenScanJob(root)
    with ui_watchdog.watch():
        with qtbot.waitSignal(job.signals.finished, timeout=120000) as blocker:
            runner.start(job)
        tab.set_items(blocker.args[0].items)
        qtbot.wait(50)
    assert tab.model.rowCount() == 25000
    runner.shutdown()
