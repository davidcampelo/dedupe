from __future__ import annotations

import time
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QModelIndex, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QAbstractItemModelTester, QTest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from dedupe.core.models import DuplicateGroup
from dedupe.gui import duplicates_view as dv
from dedupe.gui.duplicates_view import DuplicatesModel, DuplicatesTab, FileNode, GroupNode
from tests.gui.conftest import UiWatchdog
from tests.gui.helpers import synthetic_groups


def load(qtbot: QtBot, model: DuplicatesModel, groups, protected=()) -> None:  # type: ignore[no-untyped-def]
    with qtbot.waitSignal(model.load_finished, timeout=60000):
        model.set_groups(groups, protected)


def group_index(model: DuplicatesModel, g: int, col: int = 0) -> QModelIndex:
    return model.index(model._groups[g].row, col)


def file_index(model: DuplicatesModel, g: int, f: int, col: int = 0) -> QModelIndex:
    node = model._groups[g]
    model.expand(node)
    return model.index(node.row + 1 + f, col)


def p(g: int, c: int) -> Path:
    return Path(f"/data/dir{g % 200}/g{g}/copy{c}.bin")


def test_checked_state_follows_recommendation_and_toggles(qtbot: QtBot) -> None:
    model = DuplicatesModel()
    load(qtbot, model, synthetic_groups(2, copies=3, size=100))
    assert model.group_count == 2 and model.file_count == 6
    states = [model.data(file_index(model, 0, f), Qt.ItemDataRole.CheckStateRole) for f in range(3)]
    assert states == [Qt.CheckState.Unchecked, Qt.CheckState.Checked, Qt.CheckState.Checked]
    assert model.selected_count == 4 and model.selected_size == 2 * (100 + 100 + 1)
    assert model.setData(
        file_index(model, 0, 1), Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole
    )
    assert model.setData(
        file_index(model, 0, 0), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole
    )
    assert model.selected_count == 4
    assert model.data(file_index(model, 0, 0, dv.COL_STATUS)) == "Delete"
    assert model.data(file_index(model, 0, 1, dv.COL_STATUS)) == "Keep"


def test_group_and_file_row_content(qtbot: QtBot) -> None:
    model = DuplicatesModel()
    load(qtbot, model, synthetic_groups(1, copies=3, size=2048))
    g = group_index(model, 0)
    assert model.data(group_index(model, 0, dv.COL_SIZE)) == "2.0 KiB"
    assert model.data(group_index(model, 0, dv.COL_COPIES)) == "3"
    assert model.data(group_index(model, 0, dv.COL_RECLAIM)) == "4.0 KiB"
    assert model.data(g).startswith("00000000")
    assert model.data(file_index(model, 0, 0)) == "/data/dir0/g0/copy0.bin"
    assert model.data(file_index(model, 0, 1, dv.COL_REASON)).startswith("Duplicate of")
    assert model.data(file_index(model, 0, 0, dv.COL_MODIFIED))
    assert not model.flags(g) & Qt.ItemFlag.ItemIsUserCheckable
    assert model.flags(file_index(model, 0, 0)) & Qt.ItemFlag.ItemIsUserCheckable
    assert model.headerData(0, Qt.Orientation.Horizontal) == "Group / File"


def test_expand_and_collapse_keep_rows_consistent(qtbot: QtBot) -> None:
    model = DuplicatesModel()
    load(qtbot, model, synthetic_groups(4, copies=3))
    assert model.rowCount() == 4
    g1, g3 = model._groups[1], model._groups[3]
    model.expand(g3)
    model.expand(g1)
    assert model.rowCount() == 10
    assert [g.row for g in model._groups] == [0, 1, 5, 6]
    kinds = [
        "G" if isinstance(model.node_at(model.index(r, 0)), GroupNode) else "F" for r in range(10)
    ]
    assert "".join(kinds) == "GGFFFGGFFF"
    assert model.data(model.index(g3.row + 1, 0)) == str(p(3, 0))
    assert model.data(model.index(g1.row + 3, 0)) == str(p(1, 2))
    model.collapse(g1)
    assert model.rowCount() == 7 and [g.row for g in model._groups] == [0, 1, 2, 3]
    model.collapse(g1)  # idempotent
    model.expand(g3)
    model.toggle_expanded(g3)
    assert model.rowCount() == 4
    model.expand_all()
    assert model.rowCount() == 16
    assert [g.row for g in model._groups] == [0, 4, 8, 12]
    model.collapse_all()
    assert model.rowCount() == 4 and [g.row for g in model._groups] == [0, 1, 2, 3]
    assert not any(g.expanded for g in model._groups)


def test_model_passes_qt_model_tester(qtbot: QtBot) -> None:
    model = DuplicatesModel()
    QAbstractItemModelTester(model, QAbstractItemModelTester.FailureReportingMode.Fatal)
    load(qtbot, model, synthetic_groups(40, copies=3))
    model.expand(model._groups[3])
    model.expand(model._groups[0])
    model.set_checked(p(0, 0), True)
    model.collapse(model._groups[3])
    model.clear_selection()
    load(qtbot, model, synthetic_groups(5))
    load(qtbot, model, ())


def test_protected_files_are_never_checked_or_checkable(qtbot: QtBot) -> None:
    model = DuplicatesModel()
    load(qtbot, model, synthetic_groups(1, copies=3), protected=["/data/dir0"])
    for f in range(3):
        idx = file_index(model, 0, f)
        assert model.data(idx, Qt.ItemDataRole.CheckStateRole) is None
        assert not model.flags(idx) & Qt.ItemFlag.ItemIsUserCheckable
        assert model.data(file_index(model, 0, f, dv.COL_STATUS)) == "Protected"
    assert model.selected_count == 0
    assert not model.set_checked(p(0, 1), True)
    model.select_all_suggested()
    assert model.selected_count == 0


def test_overrides_are_kept_apart_from_recommendations(qtbot: QtBot) -> None:
    model = DuplicatesModel()
    load(qtbot, model, synthetic_groups(2, copies=2))
    model.clear_selection()
    assert model.selected_count == 0
    model.set_checked(p(0, 0), True)
    assert model.selected_paths() == {p(0, 0)}
    model.select_all_suggested()
    assert model.selected_paths() == {p(0, 1), p(1, 1)}


def test_batches_are_small_and_the_loop_keeps_running(qtbot: QtBot) -> None:
    model = DuplicatesModel()
    durations: list[float] = []
    original = model._load_batch

    def timed() -> None:
        start = time.perf_counter()
        original()
        durations.append(time.perf_counter() - start)

    model._timer.timeout.disconnect()
    model._timer.timeout.connect(timed)
    load(qtbot, model, synthetic_groups(8000, copies=3))
    assert len(durations) > 3  # arrived in several batches
    assert max(durations) < 0.05
    assert model.group_count == 8000


def test_new_load_supersedes_a_running_one(qtbot: QtBot) -> None:
    model = DuplicatesModel()
    model.set_groups(synthetic_groups(20000), ())
    assert model.loading
    load(qtbot, model, synthetic_groups(3))
    assert model.group_count == 3 and model.file_count == 9 and not model.loading


def test_space_toggles_selected_rows_and_delete_key_requests_deletion(qtbot: QtBot) -> None:
    tab = DuplicatesTab()
    qtbot.addWidget(tab)
    tab.show()
    load(qtbot, tab.model, synthetic_groups(2, copies=3, size=10))
    view = tab.view
    view.setCurrentIndex(file_index(tab.model, 0, 0))  # a Keep row
    assert tab.model.selected_count == 4
    QTest.keyClick(view, Qt.Key.Key_Space)
    assert tab.model.is_checked(p(0, 0)) and tab.model.selected_count == 5
    QTest.keyClick(view, Qt.Key.Key_Space)
    assert tab.model.selected_count == 4
    with qtbot.waitSignal(tab.delete_requested, timeout=1000):
        QTest.keyClick(view, Qt.Key.Key_Delete)
    assert tab.delete_button.isEnabled()
    tab.model.clear_selection()
    assert not tab.delete_button.isEnabled()
    with qtbot.assertNotEmitted(tab.delete_requested):
        QTest.keyClick(view, Qt.Key.Key_Delete)


def test_space_on_a_group_row_toggles_all_its_files(qtbot: QtBot) -> None:
    tab = DuplicatesTab()
    qtbot.addWidget(tab)
    tab.show()
    load(qtbot, tab.model, synthetic_groups(1, copies=3, size=10))
    tab.view.setCurrentIndex(group_index(tab.model, 0))
    tab.view.selectRow(0)
    QTest.keyClick(tab.view, Qt.Key.Key_Space)  # mixed (1 keep, 2 delete) -> check all
    assert tab.model.selected_count == 3
    QTest.keyClick(tab.view, Qt.Key.Key_Space)  # all checked -> uncheck all
    assert tab.model.selected_count == 0


def test_keyboard_expand_collapse(qtbot: QtBot) -> None:
    tab = DuplicatesTab()
    qtbot.addWidget(tab)
    tab.show()
    load(qtbot, tab.model, synthetic_groups(2, copies=2))
    view = tab.view
    view.setCurrentIndex(group_index(tab.model, 0))
    QTest.keyClick(view, Qt.Key.Key_Right)
    assert tab.model.rowCount() == 4
    QTest.keyClick(view, Qt.Key.Key_Down)  # onto a file row
    QTest.keyClick(view, Qt.Key.Key_Left)  # collapses the parent, selects the group
    assert tab.model.rowCount() == 2
    assert tab.model.node_at(view.currentIndex()) is tab.model._groups[0]
    QTest.keyClick(view, Qt.Key.Key_Return)
    assert tab.model.rowCount() == 4
    QTest.keyClick(view, Qt.Key.Key_Return)
    assert tab.model.rowCount() == 2


def test_mouse_checkbox_and_arrow_and_double_click(qtbot: QtBot) -> None:
    tab = DuplicatesTab()
    qtbot.addWidget(tab)
    tab.resize(1000, 500)
    tab.show()
    load(qtbot, tab.model, synthetic_groups(1, copies=2, size=10))
    view = tab.view
    # arrow area toggles expansion
    rect = view.visualRect(group_index(tab.model, 0))
    QTest.mouseClick(
        view.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(rect.left() + 8, rect.center().y())
    )
    assert tab.model.rowCount() == 3
    # checkbox of the second file (a Delete row) unticks it
    rect = view.visualRect(model_index := tab.model.index(2, 0))
    assert isinstance(tab.model.node_at(model_index), FileNode)
    assert tab.model.is_checked(p(0, 1))
    QTest.mouseClick(
        view.viewport(),
        Qt.MouseButton.LeftButton,
        pos=QPoint(rect.left() + dv.INDENT + 8, rect.center().y()),
    )
    assert not tab.model.is_checked(p(0, 1))
    # double-click on the group row collapses
    rect = view.visualRect(group_index(tab.model, 0, dv.COL_SIZE))
    event = QMouseEvent(
        QEvent.Type.MouseButtonDblClick,
        QPointF(rect.center()),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(view.viewport(), event)
    assert tab.model.rowCount() == 1


def test_node_role_lookup_and_current_group_signal(qtbot: QtBot) -> None:
    tab = DuplicatesTab()
    qtbot.addWidget(tab)
    tab.show()
    load(qtbot, tab.model, synthetic_groups(2))
    model = tab.model
    idx = file_index(model, 0, 1)
    node = model.data(idx, dv.NODE_ROLE)
    assert isinstance(node, FileNode) and node.entry.path.name == "copy1.bin"
    assert model.group_of(node.entry.path) is model.groups()[0]
    assert model.group_of(Path("/nope")) is None and model.file_node(Path("/nope")) is None
    with qtbot.waitSignal(tab.view.group_activated, timeout=1000) as blocker:
        tab.view.setCurrentIndex(idx)
    group = blocker.args[0]
    assert isinstance(group, DuplicateGroup) and group is model.groups()[0]
    assert tab.view.current_group() is group
    tab.view.setCurrentIndex(QModelIndex())
    assert tab.view.current_group() is None
    assert model.node_at(QModelIndex()) is None and model.node_at(model.index(99, 0)) is None


@pytest.mark.slow
def test_loading_100k_files_never_stalls_the_gui(qtbot: QtBot, ui_watchdog: UiWatchdog) -> None:
    groups = synthetic_groups(30000, copies=3)  # 90k files; built outside the watched block
    groups += synthetic_groups(2500, copies=4, start=30000)  # + 10k = 100k files
    tab = DuplicatesTab()
    qtbot.addWidget(tab)
    tab.show()
    with ui_watchdog.watch():
        with qtbot.waitSignal(tab.model.load_finished, timeout=60000):
            tab.set_groups(groups)
        qtbot.wait(50)
        tab.model.expand(tab.model._groups[0])  # interactive actions stay cheap too
        tab.view.scrollToBottom()
        qtbot.wait(50)
    assert tab.model.file_count == 100_000
    assert tab.model.group_count == 32500
