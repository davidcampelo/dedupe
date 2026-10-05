from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from pytestqt.qtbot import QtBot

from dedupe.core import hasher
from dedupe.core.models import ScanResult
from dedupe.core.settings import Settings
from dedupe.gui.main_window import MainWindow
from tests.gui.conftest import UiWatchdog

MakeTree = Callable[..., Path]


@pytest.fixture
def window(qtbot: QtBot) -> MainWindow:
    w = MainWindow(Settings(exclude=(), use_cache=False))
    qtbot.addWidget(w)
    w.show()
    return w


def test_scan_reports_expected_groups_under_watchdog(
    qtbot: QtBot, window: MainWindow, make_tree: MakeTree, ui_watchdog: UiWatchdog
) -> None:
    root = make_tree(
        {"a/x.txt": "dup", "b/y.txt": "dup", "c/z.txt": "other", "d/w.txt": "other", "u": "uniq"}
    )
    window.set_folder(root)
    assert window.scan_button.isEnabled() and not window.cancel_button.isEnabled()
    with ui_watchdog.watch(), qtbot.waitSignal(window.scan_finished, timeout=10000) as blocker:
        window.start_scan()
        assert window.cancel_button.isEnabled() and not window.scan_button.isEnabled()
    result: ScanResult = blocker.args[0]
    assert len(result.groups) == 2
    assert window.groups_label.text() == "Duplicate groups: 2"
    assert window.files_label.text() == "Files scanned: 5"
    assert window.scan_button.isEnabled() and not window.cancel_button.isEnabled()


def test_slow_scan_cancels_within_a_second_without_stalling(
    qtbot: QtBot,
    window: MainWindow,
    make_tree: MakeTree,
    ui_watchdog: UiWatchdog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = make_tree({f"d{i % 5}/f{i}": f"content-{i % 40}" * 10 for i in range(200)})
    original = hasher.partial_hash

    def slow(path: Path, size: int, cancel: object = None) -> str:
        time.sleep(0.01)  # injected per-file delay
        return original(path, size, cancel)  # type: ignore[arg-type]

    monkeypatch.setattr(hasher, "partial_hash", slow)
    window.set_folder(root)
    with ui_watchdog.watch():
        window.start_scan()
        qtbot.waitUntil(lambda: "partial" in window.stage_label.text(), timeout=5000)
        start = time.monotonic()
        with qtbot.waitSignal(window.scan_finished, timeout=5000) as blocker:
            window.cancel_button.click()
        elapsed = time.monotonic() - start
    assert blocker.args[0].cancelled
    assert elapsed < 1.0
    assert window.stage_label.text() == "Cancelled"
    assert window.scan_button.isEnabled()


def test_bad_folder_is_reported_not_crashed(
    qtbot: QtBot, window: MainWindow, tmp_path: Path
) -> None:
    window.set_folder(tmp_path / "does-not-exist")
    with qtbot.waitSignal(window.scan_finished, timeout=5000):
        window.start_scan()
    assert window.error_label.isVisible() and "vanished" in window.error_label.text()


def test_job_exception_becomes_failed_signal(
    qtbot: QtBot, window: MainWindow, make_tree: MakeTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dedupe.gui.workers as workers

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(workers, "run_scan", boom)
    window.set_folder(make_tree({"a": "x"}))
    with qtbot.waitSignal(window.scan_failed, timeout=5000) as blocker:
        window.start_scan()
    assert "kaboom" in blocker.args[0]
    assert window.error_label.isVisible() and window.scan_button.isEnabled()


def test_recent_folders_and_drop(qtbot: QtBot, window: MainWindow, tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    window.set_folder(a)
    window.set_folder(b)
    window.set_folder(a)
    assert window.recent == [str(a), str(b)]
    assert [window.recent_combo.itemText(i) for i in range(2)] == [str(a), str(b)]
    window.recent_combo.activated.emit(1)
    assert window.folder == b

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(tmp_path / "dropped"))])
    event = QDropEvent(
        QPointF(QPoint(5, 5)),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dropEvent(event)
    assert window.folder == tmp_path / "dropped"


def test_close_cancels_and_waits_for_jobs(
    qtbot: QtBot, make_tree: MakeTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_tree({f"f{i}": f"c{i % 30}" * 20 for i in range(100)})
    original = hasher.partial_hash

    def slow(path: Path, size: int, cancel: object = None) -> str:
        time.sleep(0.02)
        return original(path, size, cancel)  # type: ignore[arg-type]

    monkeypatch.setattr(hasher, "partial_hash", slow)
    w = MainWindow(Settings(exclude=(), use_cache=False))
    qtbot.addWidget(w)
    w.set_folder(root)
    w.start_scan()
    qtbot.waitUntil(lambda: "partial" in w.stage_label.text(), timeout=5000)
    start = time.monotonic()
    w.close()
    assert time.monotonic() - start < 2.0
    assert w.job is not None and w.job.done


def test_scan_result_fills_duplicates_tab_and_selection_label(
    qtbot: QtBot, window: MainWindow, make_tree: MakeTree
) -> None:
    root = make_tree({"a/x.txt": "dupdup", "b/x.txt": "dupdup", "c/x.txt": "dupdup"})
    window.set_folder(root)
    with qtbot.waitSignal(window.scan_finished, timeout=10000):
        window.start_scan()
    model = window.duplicates_tab.model
    qtbot.waitUntil(lambda: not model.loading, timeout=5000)
    assert model.group_count == 1 and model.file_count == 3
    assert model.selected_count == 2  # two Delete suggestions, one Keep
    assert window.selected_label.text() == "Selected for deletion: 12 B"
    assert window.wasted_label.text() == "Wasted space: 12 B"
