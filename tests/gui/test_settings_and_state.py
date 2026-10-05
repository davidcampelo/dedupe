from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from dedupe.core.hidden import scan_hidden
from dedupe.core.models import DeleteMode
from dedupe.core.settings import Settings, load_settings
from dedupe.gui.hidden_files_view import HiddenFilesTab
from dedupe.gui.main_window import MainWindow
from dedupe.gui.settings_dialog import SettingsDialog
from dedupe.gui.workers import JobRunner
from tests.gui.test_icons import ink

MakeTree = Callable[..., Path]


# -- the dialog ---------------------------------------------------------------------------------


def dialog_for(qtbot: QtBot, settings: Settings, **kw: object) -> SettingsDialog:
    d = SettingsDialog(settings, JobRunner(1), **kw)  # type: ignore[arg-type]
    qtbot.addWidget(d)
    return d


def test_dialog_reflects_every_setting_and_round_trips_untouched(qtbot: QtBot) -> None:
    s = Settings(
        exclude=("*.log", "/proc"),
        protected_folders=("/a", "/b"),
        min_size=4096,
        follow_symlinks=True,
        cross_filesystems=True,
        include_hidden=False,
        paranoid=True,
        workers=3,
        default_delete_mode="permanent",
        use_cache=False,
    )
    d = dialog_for(qtbot, s)
    assert d.exclude_edit.toPlainText() == "*.log\n/proc"
    assert [d.protected_list.item(i).text() for i in range(2)] == ["/a", "/b"]
    assert d.min_size.value() == 4096 and d.workers.value() == 3
    assert (
        d.follow_symlinks.isChecked()
        and d.cross_fs.isChecked()
        and not d.include_hidden.isChecked()
    )
    assert d.paranoid.isChecked() and not d.use_cache.isChecked()
    assert d.delete_mode.currentData() == "permanent"
    assert d.result_settings() == s
    assert dialog_for(qtbot, Settings()).result_settings() == Settings()


def test_dialog_edits_produce_new_settings_including_falsy_values(qtbot: QtBot) -> None:
    d = dialog_for(qtbot, Settings())
    d.exclude_edit.setPlainText("  *.bak \n\n.git\n")
    d.min_size.setValue(0)
    d.workers.setValue(0)
    d.include_hidden.setChecked(False)
    d.delete_mode.setCurrentIndex(d.delete_mode.findData("hardlink"))
    d.add_protected_folder("/photos")
    d.add_protected_folder("/photos")  # no duplicates
    new = d.result_settings()
    assert new == replace(
        Settings(),
        exclude=("*.bak", ".git"),
        min_size=0,
        workers=0,
        include_hidden=False,
        default_delete_mode="hardlink",
        protected_folders=("/photos",),
    )
    d.protected_list.setCurrentRow(0)
    d.protected_list.item(0).setSelected(True)
    d.remove_protected.click()
    assert d.result_settings().protected_folders == ()
    assert d.result_settings().exclude == ("*.bak", ".git")
    d.exclude_edit.setPlainText("")
    assert d.result_settings().exclude == ()


def test_clear_cache_runs_as_a_job_and_only_the_button_is_busy(qtbot: QtBot) -> None:
    gate = threading.Event()

    def slow_clear() -> int:
        gate.wait(5)
        return 42

    d = dialog_for(qtbot, Settings(), clear_cache_fn=slow_clear)
    d.show()
    d.clear_cache.click()
    assert not d.clear_cache.isEnabled() and d.clear_cache.text() == "Clearing…"
    assert d.buttons.isEnabled() and d.exclude_edit.isEnabled()  # the dialog stays usable
    gate.set()
    qtbot.waitUntil(lambda: d.clear_cache.isEnabled(), timeout=5000)
    assert d.clear_cache_status.text() == "Cleared 42 cached hashes"
    assert d.clear_cache.text() == "Clear hash cache"


def test_clear_cache_failure_is_reported(qtbot: QtBot) -> None:
    def boom() -> int:
        raise RuntimeError("database is locked")

    d = dialog_for(qtbot, Settings(), clear_cache_fn=boom)
    d.clear_cache.click()
    qtbot.waitUntil(lambda: d.clear_cache.isEnabled(), timeout=5000)
    assert "database is locked" in d.clear_cache_status.text()


def test_the_real_clear_cache_uses_the_isolated_cache_dir(qtbot: QtBot) -> None:
    from dedupe.core.cache import HashCache
    from dedupe.gui.settings_dialog import clear_default_cache

    cache = HashCache()
    assert cache.db_path.is_relative_to(Path.home())  # the test HOME, never the real one
    cache.close()
    assert clear_default_cache() == 0


# -- window: settings apply to the next scan --------------------------------------------------------


@pytest.fixture
def window(qtbot: QtBot, tmp_path: Path) -> MainWindow:
    w = MainWindow(Settings(exclude=(), use_cache=False))
    qtbot.addWidget(w)
    w.show()
    w.settings_path = tmp_path / "cfg" / "settings.toml"
    return w


def test_changed_settings_are_saved_and_apply_to_the_next_scan(
    qtbot: QtBot, window: MainWindow, make_tree: MakeTree
) -> None:
    root = make_tree({"a.log": "dup", "b.log": "dup", "c.txt": "dup", "d.txt": "dup"})
    window.set_folder(root)
    with qtbot.waitSignal(window.scan_finished, timeout=10000):
        window.start_scan()
    qtbot.waitUntil(lambda: not window.duplicates_tab.model.loading)
    assert len(window.duplicates_tab.model.groups()[0].files) == 4

    new = replace(window.settings, exclude=("*.log",), default_delete_mode="permanent")
    window.ask_settings = lambda: new  # type: ignore[method-assign]
    window.open_settings()
    qtbot.waitUntil(
        lambda: window.settings_path is not None and window.settings_path.exists(), timeout=5000
    )
    saved = load_settings(window.settings_path).settings  # type: ignore[arg-type]
    assert saved.exclude == ("*.log",) and saved.default_delete_mode == "permanent"
    with qtbot.waitSignal(window.scan_finished, timeout=10000):
        window.start_scan()
    qtbot.waitUntil(lambda: not window.duplicates_tab.model.loading)
    assert len(window.duplicates_tab.model.groups()[0].files) == 2  # the next scan used it
    assert "apply to the next scan" in window.statusBar().currentMessage()


def test_cancelling_the_settings_dialog_changes_nothing(qtbot: QtBot, window: MainWindow) -> None:
    before = window.settings
    window.ask_settings = lambda: None  # type: ignore[method-assign]
    window.open_settings()
    assert window.settings is before and not window.settings_path.exists()  # type: ignore[union-attr]


def test_the_default_delete_mode_setting_reaches_the_confirmation_dialog(
    qtbot: QtBot, window: MainWindow, make_tree: MakeTree
) -> None:
    from dedupe.gui.delete_dialog import DeleteDialog

    plan = __import__("tests.gui.test_delete_flow", fromlist=["plan_for"]).plan_for(make_tree, 3)
    window.settings = replace(window.settings, default_delete_mode="permanent")
    dialog = DeleteDialog(plan, DeleteMode(window.settings.default_delete_mode))
    qtbot.addWidget(dialog)
    assert dialog.mode is DeleteMode.PERMANENT and not dialog.ok_button.isEnabled()


def test_protected_folder_changes_in_settings_rerun_the_recommendations(
    qtbot: QtBot, window: MainWindow, make_tree: MakeTree
) -> None:
    root = make_tree({"a/x.txt": "dup", "b/x.txt": "dup"})
    window.set_folder(root)
    with qtbot.waitSignal(window.scan_finished, timeout=10000):
        window.start_scan()
    qtbot.waitUntil(lambda: not window.duplicates_tab.model.loading)
    assert window.duplicates_tab.model.selected_count == 1
    with qtbot.waitSignal(window.duplicates_tab.view_changed, timeout=10000):
        window.apply_settings(
            replace(window.settings, protected_folders=(str(root / "a"), str(root / "b")))
        )
    qtbot.waitUntil(lambda: not window.duplicates_tab.model.loading)
    assert window.duplicates_tab.model.selected_count == 0  # both copies are now protected


# -- persisted UI state ---------------------------------------------------------------------------------


def test_a_restart_restores_size_splitters_and_folders(qtbot: QtBot, tmp_path: Path) -> None:
    ini = str(tmp_path / "ui.ini")
    first = MainWindow(Settings(use_cache=False), QSettings(ini, QSettings.Format.IniFormat))
    qtbot.addWidget(first)
    first.show()
    first.resize(700, 500)  # smaller than the layout minimum: the window grows to fit
    first.duplicates_tab.splitter.setSizes([700, 200])
    a, b = tmp_path / "a", tmp_path / "b"
    first.set_folder(a)
    first.set_folder(b)
    sizes = first.duplicates_tab.splitter.sizes()
    first_size = (first.width(), first.height())
    first.close()

    second = MainWindow(Settings(use_cache=False), QSettings(ini, QSettings.Format.IniFormat))
    qtbot.addWidget(second)
    second.show()
    assert (second.width(), second.height()) == first_size
    restored = second.duplicates_tab.splitter.sizes()
    assert abs(restored[0] / sum(restored) - sizes[0] / sum(sizes)) < 0.03
    assert second.folder == b and second.recent == [str(b), str(a)]
    assert second.recent_combo.currentText() == str(b)
    assert second.scan_button.isEnabled()  # ready to scan, nothing scanned automatically
    assert second.job is None


def test_a_window_without_ui_settings_persists_nothing(qtbot: QtBot, tmp_path: Path) -> None:
    w = MainWindow(Settings(use_cache=False))
    qtbot.addWidget(w)
    w.set_folder(tmp_path)
    w.close()  # must not touch any QSettings store
    assert w.ui_settings is None


def test_a_single_recent_folder_survives_qsettings_quirks(qtbot: QtBot, tmp_path: Path) -> None:
    ini = str(tmp_path / "ui.ini")
    first = MainWindow(Settings(use_cache=False), QSettings(ini, QSettings.Format.IniFormat))
    qtbot.addWidget(first)
    first.set_folder(tmp_path / "only")
    first.close()  # QSettings stores a one-element list as a bare string
    second = MainWindow(Settings(use_cache=False), QSettings(ini, QSettings.Format.IniFormat))
    qtbot.addWidget(second)
    assert second.recent == [str(tmp_path / "only")]


# -- theme --------------------------------------------------------------------------------------------------


@pytest.fixture
def restore_palette(qapp: QApplication) -> Iterator[None]:
    original = qapp.palette()
    yield
    qapp.setPalette(original)


def palette(bg: str, fg: str) -> QPalette:
    p = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, bg),
        (QPalette.ColorRole.WindowText, fg),
        (QPalette.ColorRole.Text, fg),
        (QPalette.ColorRole.ButtonText, fg),
    ):
        p.setColor(role, QColor(color))
    return p


def test_a_light_dark_switch_recolours_icons_without_a_restart(
    qtbot: QtBot, qapp: QApplication, restore_palette: None
) -> None:
    qapp.setPalette(palette("#f4f6f5", "#101a1f"))
    w = MainWindow(Settings(use_cache=False))
    qtbot.addWidget(w)
    w.show()
    hidden = HiddenFilesTab()
    qtbot.addWidget(hidden)
    light = {
        "settings": ink(w.settings_button.icon()),
        "tab": ink(w.tabs.tabIcon(0)),
        "delete": ink(w.duplicates_tab.delete_button.icon()),
    }
    qapp.setPalette(palette("#1c2024", "#e6ebe9"))  # what the system theme switch does
    dark = {
        "settings": ink(w.settings_button.icon()),
        "tab": ink(w.tabs.tabIcon(0)),
        "delete": ink(w.duplicates_tab.delete_button.icon()),
    }
    for key in light:
        assert light[key] != dark[key], f"{key} icon was not recoloured"
    assert dark["settings"].lightness() > 150 and light["settings"].lightness() < 100
    qapp.setPalette(palette("#f4f6f5", "#101a1f"))
    assert ink(w.settings_button.icon()) == light["settings"]


# -- keyboard -------------------------------------------------------------------------------------------------


def test_window_shortcuts(
    qtbot: QtBot, window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "files"
    root.mkdir()
    for i in range(3):
        (root / f"f{i}").write_text("same")
    window.set_folder(root)
    QTest.keyClick(window, Qt.Key.Key_Escape)  # idle: nothing happens
    assert window.job is None
    with qtbot.waitSignal(window.scan_finished, timeout=10000):
        QTest.keyClick(window, Qt.Key.Key_R, Qt.KeyboardModifier.ControlModifier)
        assert window.busy
    with qtbot.waitSignal(window.scan_finished, timeout=10000):
        QTest.keyClick(window, Qt.Key.Key_F5)
    QTest.keyClick(window, Qt.Key.Key_2, Qt.KeyboardModifier.ControlModifier)
    assert window.tabs.currentIndex() == 1
    QTest.keyClick(window, Qt.Key.Key_3, Qt.KeyboardModifier.ControlModifier)
    assert window.tabs.currentIndex() == 2
    QTest.keyClick(window, Qt.Key.Key_1, Qt.KeyboardModifier.ControlModifier)
    assert window.tabs.currentIndex() == 0
    opened: list[int] = []
    window.ask_settings = lambda: opened.append(1)  # type: ignore[method-assign]
    QTest.keyClick(window, Qt.Key.Key_Comma, Qt.KeyboardModifier.ControlModifier)
    assert opened == [1]


def test_escape_cancels_a_running_scan(
    qtbot: QtBot, window: MainWindow, make_tree: MakeTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from dedupe.core import hasher

    root = make_tree({f"f{i}": f"content-{i % 20}" * 20 for i in range(150)})
    real = hasher.partial_hash
    monkeypatch.setattr(
        hasher, "partial_hash", lambda p, s, c=None: (time.sleep(0.01), real(p, s, c))[1]
    )
    window.set_folder(root)
    window.start_scan()
    qtbot.waitUntil(lambda: "partial" in window.stage_label.text(), timeout=5000)
    with qtbot.waitSignal(window.scan_finished, timeout=5000) as blocker:
        QTest.keyClick(window, Qt.Key.Key_Escape)
    assert blocker.args[0].cancelled


def test_tab_order_follows_the_visual_order(qtbot: QtBot, window: MainWindow) -> None:
    chain = []
    w = window.choose_button
    for _ in range(12):
        chain.append(w)
        w = w.nextInFocusChain()
    wanted = [
        window.choose_button,
        window.recent_combo,
        window.scan_button,
        window.cancel_button,
        window.settings_button,
    ]
    positions = [chain.index(x) for x in wanted]
    assert positions == sorted(positions)


def test_keyboard_walkthrough_of_the_hidden_tab(qtbot: QtBot, tmp_path: Path) -> None:
    root = tmp_path / "h"
    root.mkdir()
    for name in ("a~", "b~", "c.tmp"):
        (root / name).write_text("x")
    tab = HiddenFilesTab()
    qtbot.addWidget(tab)
    tab.show()
    tab.set_items(scan_hidden(root, open_files_fn=set).items)
    assert tab.model.selected_count == 2  # a~ and b~
    tab.view.setFocus()
    tab.view.setCurrentIndex(tab.model.index(0, 0))
    tab.view.selectRow(0)
    QTest.keyClick(tab.view, Qt.Key.Key_Space)  # toggles row 0 off
    assert tab.model.selected_count == 1
    QTest.keyClick(tab.view, Qt.Key.Key_Down)
    QTest.keyClick(tab.view, Qt.Key.Key_Down)
    QTest.keyClick(tab.view, Qt.Key.Key_Space)  # row 2 (c.tmp, unticked) on
    assert tab.model.selected_count == 2
    with qtbot.waitSignal(tab.delete_requested, timeout=1000):
        QTest.keyClick(tab.view, Qt.Key.Key_Delete)
    tab.model.select_none()
    with qtbot.assertNotEmitted(tab.delete_requested):
        QTest.keyClick(tab.view, Qt.Key.Key_Delete)
