from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from dedupe.core.models import DuplicateGroup, ScanOptions
from dedupe.core.pipeline import run_scan
from dedupe.gui import thumbnails
from dedupe.gui.duplicates_view import DuplicatesTab
from dedupe.gui.image_grid import GROUP_ROLE, TILE, ImageGridModel
from dedupe.gui.thumbnails import ThumbnailService
from tests.gui.conftest import UiWatchdog
from tests.gui.imaging import write_image


def make_image_groups(root: Path, n: int, extra_text_groups: int = 0) -> tuple[DuplicateGroup, ...]:
    for i in range(n):
        color = (i % 256, (i // 256) % 256, (i * 7) % 256)
        first = write_image(root / f"g{i}" / "a" / f"img{i}.png", (24, 16), color)
        (root / f"g{i}" / "b").mkdir()
        shutil.copy(first, root / f"g{i}" / "b" / f"img{i}.png")
    for i in range(extra_text_groups):
        for d in ("a", "b"):
            (root / f"t{i}" / d).mkdir(parents=True, exist_ok=True)
            (root / f"t{i}" / d / "note.txt").write_text(f"text {i}")
    return run_scan(root, ScanOptions(exclude=(), use_cache=False)).groups


@pytest.fixture
def tab(qtbot: QtBot) -> DuplicatesTab:
    t = DuplicatesTab()
    qtbot.addWidget(t)
    t.resize(1300, 700)
    t.show()
    return t


def load(qtbot: QtBot, tab: DuplicatesTab, groups: tuple[DuplicateGroup, ...]) -> None:
    with qtbot.waitSignal(tab.view_changed, timeout=20000):
        tab.set_groups(groups)
    qtbot.waitUntil(lambda: not tab.model.loading, timeout=20000)


def enter_grid(qtbot: QtBot, tab: DuplicatesTab) -> None:
    with qtbot.waitSignal(tab.grid_changed, timeout=10000):
        tab.grid_button.setChecked(True)


def test_grid_shows_one_tile_per_image_group_only(
    qtbot: QtBot, tab: DuplicatesTab, tmp_path: Path
) -> None:
    load(qtbot, tab, make_image_groups(tmp_path, 5, extra_text_groups=3))
    assert tab.model.group_count == 8
    assert not tab.grid_mode
    enter_grid(qtbot, tab)
    assert tab.grid_mode and tab.grid.grid_model.rowCount() == 5
    index = tab.grid.grid_model.index(0)
    text = tab.grid.grid_model.data(index)
    assert text.startswith("img") and "2 copies" in text
    assert isinstance(tab.grid.grid_model.data(index, GROUP_ROLE), DuplicateGroup)
    assert tab.grid.grid_model.data(tab.grid.grid_model.index(99)) is None


def test_thumbnails_are_requested_lazily_for_painted_tiles_only(
    qtbot: QtBot, tab: DuplicatesTab, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    load(qtbot, tab, make_image_groups(tmp_path, 400))
    requested: list[str] = []
    real = tab.thumbnails.request

    def spy(path: Path, size: int, mtime_ns: int, content_hash: str = "") -> object:
        requested.append(path.name)
        return real(path, size, mtime_ns, content_hash)

    monkeypatch.setattr(tab.thumbnails, "request", spy)
    enter_grid(qtbot, tab)
    qtbot.wait(300)
    per_screen = len(tab.grid.visible_rows())
    assert 0 < len(set(requested)) < 400 and len(set(requested)) <= per_screen + 60
    # scrolling to the end brings other tiles in
    tab.grid.verticalScrollBar().setValue(tab.grid.verticalScrollBar().maximum())
    qtbot.wait(300)
    assert len(set(requested)) > per_screen


def test_placeholder_first_then_the_thumbnail(qtbot: QtBot, tmp_path: Path) -> None:
    groups = make_image_groups(tmp_path, 1)
    service = ThumbnailService()
    model = ImageGridModel(service)
    model.set_groups(groups)
    changed: list[int] = []
    model.dataChanged.connect(lambda tl, br, roles: changed.append(tl.row()))
    icon = model.data(model.index(0), Qt.ItemDataRole.DecorationRole)
    assert not icon.isNull() and not model._pixmaps  # a placeholder, nothing decoded yet
    qtbot.waitUntil(lambda: changed == [0], timeout=10000)
    assert len(model._pixmaps) == 1
    again = model.data(model.index(0), Qt.ItemDataRole.DecorationRole)
    assert not again.pixmap(32, 32).isNull()
    service.shutdown()


def test_a_failed_thumbnail_keeps_the_placeholder(qtbot: QtBot, tmp_path: Path) -> None:
    for d in ("a", "b"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "bad.jpg").write_bytes(b"\xff\xd8\xff nonsense")
    groups = run_scan(tmp_path, ScanOptions(exclude=(), use_cache=False)).groups
    service = ThumbnailService()
    model = ImageGridModel(service)
    model.set_groups(groups)
    model.data(model.index(0), Qt.ItemDataRole.DecorationRole)
    qtbot.waitUntil(lambda: len(model._failed) == 1, timeout=10000)
    assert not model.data(model.index(0), Qt.ItemDataRole.DecorationRole).isNull()
    service.shutdown()


def test_clicking_a_tile_selects_the_group(
    qtbot: QtBot, tab: DuplicatesTab, tmp_path: Path
) -> None:
    load(qtbot, tab, make_image_groups(tmp_path, 6))
    enter_grid(qtbot, tab)
    index = tab.grid.grid_model.index(2)
    rect = tab.grid.visualRect(index)
    with qtbot.waitSignal(tab.grid.group_selected, timeout=2000) as blocker:
        QTest.mouseClick(tab.grid.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    group = blocker.args[0]
    assert group is tab.grid.grid_model.groups[2]
    assert tab.details.title.text() == "Group of 2 identical files"
    assert tab.compare.group is group and tab.compare.isVisibleTo(tab)


def test_switching_modes_preserves_the_selection(
    qtbot: QtBot, tab: DuplicatesTab, tmp_path: Path
) -> None:
    load(qtbot, tab, make_image_groups(tmp_path, 6, extra_text_groups=2))
    # select a group in the list, then go to the grid
    wanted = tab.model._groups[3].group
    assert tab.select_group(wanted)
    assert tab.view.current_group() is wanted
    enter_grid(qtbot, tab)
    assert tab.grid.selected_group() is wanted or wanted not in tab.grid.grid_model.groups
    if wanted in tab.grid.grid_model.groups:
        assert tab.grid.selected_group() is wanted
    # select another tile, go back to the list
    other = tab.grid.grid_model.groups[0]
    tab.grid.select_group(other)
    tab.grid_button.setChecked(False)
    assert not tab.grid_mode and tab.view.current_group() is other
    # and the selection survives another round trip
    enter_grid(qtbot, tab)
    assert tab.grid.selected_group() is other


def test_grid_follows_filters(qtbot: QtBot, tab: DuplicatesTab, tmp_path: Path) -> None:
    load(qtbot, tab, make_image_groups(tmp_path, 4, extra_text_groups=2))
    enter_grid(qtbot, tab)
    assert tab.grid.grid_model.rowCount() == 4
    with qtbot.waitSignal(tab.grid_changed, timeout=10000):
        tab.filter_edit.setText("g1/")
    assert tab.grid.grid_model.rowCount() == 1


def test_scrolling_away_cancels_thumbnail_jobs_for_offscreen_tiles(
    qtbot: QtBot, tab: DuplicatesTab, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = threading.Event()
    real = thumbnails.decode_image
    monkeypatch.setattr(thumbnails, "decode_image", lambda p, s: (gate.wait(5), real(p, s))[1])
    load(qtbot, tab, make_image_groups(tmp_path, 300))
    enter_grid(qtbot, tab)
    qtbot.wait(200)
    pending_before = tab.thumbnails.pending_count()
    assert pending_before > 0
    tab.grid.verticalScrollBar().setValue(tab.grid.verticalScrollBar().maximum())
    qtbot.wait(100)
    cancelled = tab.grid.prune_offscreen()
    assert cancelled > 0
    gate.set()
    qtbot.waitUntil(lambda: tab.thumbnails.pending_count() == 0, timeout=20000)


@pytest.mark.slow
def test_scrolling_1000_image_groups_never_stalls_the_gui(
    qtbot: QtBot, tab: DuplicatesTab, tmp_path: Path, ui_watchdog: UiWatchdog
) -> None:
    load(qtbot, tab, make_image_groups(tmp_path, 1000))
    assert tab.model.group_count == 1000
    with ui_watchdog.watch():
        with qtbot.waitSignal(tab.grid_changed, timeout=30000):
            tab.grid_button.setChecked(True)
        bar = tab.grid.verticalScrollBar()
        steps = 40
        for n in range(steps + 1):
            bar.setValue(bar.maximum() * n // steps)
            qtbot.wait(10)
        qtbot.wait(300)
    assert tab.grid.grid_model.rowCount() == 1000 and TILE.width() > 0
    qtbot.waitUntil(lambda: tab.thumbnails.pending_count() == 0, timeout=60000)
