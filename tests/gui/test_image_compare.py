from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from dedupe.core.models import DuplicateGroup, ScanOptions
from dedupe.core.pipeline import run_scan
from dedupe.gui.duplicates_view import DuplicatesTab
from dedupe.gui.image_compare import MAX_CARDS
from tests.gui.conftest import UiWatchdog
from tests.gui.imaging import write_image, write_noise_image


def image_groups(root: Path, **kw: object) -> tuple[DuplicateGroup, ...]:
    return run_scan(root, ScanOptions(exclude=(), use_cache=False, **kw)).groups  # type: ignore[arg-type]


@pytest.fixture
def tab(qtbot: QtBot) -> DuplicatesTab:
    t = DuplicatesTab()
    qtbot.addWidget(t)
    t.resize(1400, 700)
    t.show()
    return t


def load(qtbot: QtBot, tab: DuplicatesTab, groups: tuple[DuplicateGroup, ...]) -> None:
    with qtbot.waitSignal(tab.view_changed, timeout=10000):
        tab.set_groups(groups)
    qtbot.waitUntil(lambda: not tab.model.loading, timeout=10000)


def select_group(tab: DuplicatesTab, n: int = 0) -> None:
    tab.view.setCurrentIndex(tab.model.index(tab.model._groups[n].row, 0))


def three_copies(tmp_path: Path) -> Path:
    first = write_image(tmp_path / "t/a/photo.jpg", (320, 160), taken="2019:12:31 23:59:00")
    for sub in ("b", "c"):
        (tmp_path / "t" / sub).mkdir()
        shutil.copy(first, tmp_path / "t" / sub / "photo.jpg")
    return tmp_path / "t"


def test_placeholders_appear_at_once_then_thumbnails_replace_them(
    qtbot: QtBot, tab: DuplicatesTab, tmp_path: Path
) -> None:
    load(qtbot, tab, image_groups(three_copies(tmp_path)))
    select_group(tab)
    panel = tab.compare
    assert panel.isVisibleTo(tab) and len(panel.cards) == 3
    assert all(c.thumb.text() == "Loading…" for c in panel.cards)  # nothing decoded yet
    assert all("…" in c.resolution.text() for c in panel.cards)
    qtbot.waitUntil(lambda: all(c.thumb.text() == "" for c in panel.cards), timeout=10000)
    card = panel.cards[0]
    assert not card.thumb.pixmap().isNull()
    assert card.resolution.text() == "Resolution: 320 × 160"
    assert card.exif.text() == "EXIF date: 2019-12-31 23:59"
    assert "B" in card.size_label.text() and card.modified.text().startswith("Modified: 20")
    assert panel.heading.text() == "Compare 3 identical images"


def test_keep_delete_toggles_stay_in_sync_with_the_list(
    qtbot: QtBot, tab: DuplicatesTab, tmp_path: Path
) -> None:
    groups = image_groups(three_copies(tmp_path))
    load(qtbot, tab, groups)
    select_group(tab)
    cards = tab.compare.cards
    model = tab.model
    # the recommendation: one Keep, two Delete
    assert sorted(c.delete_box.isChecked() for c in cards) == [False, True, True]
    keeper = next(c for c in cards if not c.delete_box.isChecked())
    # toggling a card changes the model (and so the list and the totals)
    before = model.selected_count
    keeper.delete_box.setChecked(True)
    assert model.is_checked(keeper.entry.path) and model.selected_count == before + 1
    # toggling in the list changes the card
    other = next(c for c in cards if c is not keeper)
    model.set_checked(other.entry.path, False)
    assert not other.delete_box.isChecked()
    # a click on the real checkbox widget works too
    QTest.mouseClick(other.delete_box, Qt.MouseButton.LeftButton)
    assert model.is_checked(other.entry.path)


def test_protected_copies_cannot_be_toggled(
    qtbot: QtBot, tab: DuplicatesTab, tmp_path: Path
) -> None:
    root = three_copies(tmp_path)
    groups = image_groups(root)
    with qtbot.waitSignal(tab.view_changed, timeout=10000):
        tab.set_groups(groups, [str(root / "a")])
    qtbot.waitUntil(lambda: not tab.model.loading)
    select_group(tab)
    protected = next(c for c in tab.compare.cards if c.entry.path.parent.name == "a")
    assert not protected.delete_box.isEnabled() and not protected.delete_box.isChecked()
    assert protected.delete_box.text() == "Protected (kept)"


def test_corrupt_image_shows_an_error_placeholder(
    qtbot: QtBot, tab: DuplicatesTab, tmp_path: Path
) -> None:
    for name in ("a", "b"):
        (tmp_path / "t").mkdir(exist_ok=True)
        (tmp_path / "t" / f"{name}.jpg").write_bytes(b"\xff\xd8\xff not an image at all")
    load(qtbot, tab, image_groups(tmp_path / "t"))
    select_group(tab)
    cards = tab.compare.cards
    qtbot.waitUntil(lambda: all("Cannot read" in c.thumb.text() for c in cards), timeout=10000)
    assert all(c.resolution.text() == "Resolution: unknown" for c in cards)
    assert cards[0].thumb.toolTip()  # carries the reason


def test_non_image_groups_hide_the_panel_and_cancel_thumbnails(
    qtbot: QtBot, tab: DuplicatesTab, tmp_path: Path
) -> None:
    root = tmp_path / "t"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    for d in ("a", "b"):
        (root / d / "doc.txt").write_text("same text")
    load(qtbot, tab, image_groups(root))
    select_group(tab)
    assert not tab.compare.isVisibleTo(tab) and tab.compare.cards == []


def test_double_click_opens_a_larger_preview(
    qtbot: QtBot, tab: DuplicatesTab, tmp_path: Path
) -> None:
    load(qtbot, tab, image_groups(three_copies(tmp_path)))
    select_group(tab)
    panel = tab.compare
    qtbot.waitUntil(lambda: all(c.thumb.text() == "" for c in panel.cards), timeout=10000)
    preview = panel.open_preview(panel.cards[0].entry)
    qtbot.addWidget(preview)
    qtbot.waitUntil(
        lambda: preview.label.pixmap() is not None and not preview.label.pixmap().isNull(),
        timeout=10000,
    )
    assert preview.windowTitle() == "photo.jpg" and "320 × 160" in preview.caption.text()
    preview.close()
    # double-clicking the thumbnail label emits the signal that opens it
    with qtbot.waitSignal(panel.cards[0].thumb.double_clicked, timeout=1000):
        QTest.mouseDClick(panel.cards[0].thumb, Qt.MouseButton.LeftButton)


def test_preview_of_a_broken_image_explains_itself(
    qtbot: QtBot, tab: DuplicatesTab, tmp_path: Path
) -> None:
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"nope")
    from dedupe.gui.preview_window import PreviewWindow

    preview = PreviewWindow(tab.thumbnails, bad, bad.stat().st_mtime_ns)
    qtbot.addWidget(preview)
    qtbot.waitUntil(lambda: "Cannot preview" in preview.label.text(), timeout=10000)


def test_large_groups_show_a_capped_number_of_cards(
    qtbot: QtBot, tab: DuplicatesTab, tmp_path: Path
) -> None:
    first = write_image(tmp_path / "t/0/p.png", (40, 40))
    for i in range(1, MAX_CARDS + 5):
        (tmp_path / "t" / str(i)).mkdir()
        shutil.copy(first, tmp_path / "t" / str(i) / "p.png")
    load(qtbot, tab, image_groups(tmp_path / "t"))
    select_group(tab)
    assert len(tab.compare.cards) == MAX_CARDS
    assert tab.compare.more.text() == "… and 5 more copies"


def test_selecting_another_group_replaces_the_cards(
    qtbot: QtBot, tab: DuplicatesTab, tmp_path: Path
) -> None:
    for g, color in (("g1", (255, 0, 0)), ("g2", (0, 255, 0))):
        first = write_image(tmp_path / "t" / g / "x" / "p.png", (30, 30), color)
        (tmp_path / "t" / g / "y").mkdir()
        shutil.copy(first, tmp_path / "t" / g / "y" / "p.png")
    load(qtbot, tab, image_groups(tmp_path / "t"))
    select_group(tab, 0)
    first_cards = list(tab.compare.cards)
    select_group(tab, 1)
    assert tab.compare.cards != first_cards and len(tab.compare.cards) == 2
    assert tab.compare.group is tab.model._groups[1].group


@pytest.mark.slow
def test_selecting_20_large_image_groups_quickly_never_stalls_the_gui(
    qtbot: QtBot, tab: DuplicatesTab, tmp_path: Path, ui_watchdog: UiWatchdog
) -> None:
    root = tmp_path / "big"
    for g in range(20):
        a = write_noise_image(root / f"g{g}" / "a" / "p.jpg", (2400, 1600), g)
        (root / f"g{g}" / "b").mkdir()
        shutil.copy(a, root / f"g{g}" / "b" / "p.jpg")
    load(qtbot, tab, image_groups(root))
    assert tab.model.group_count == 20
    with ui_watchdog.watch():
        for n in range(20):  # flick through every group as fast as the loop allows
            select_group(tab, n)
            qtbot.wait(5)
        panel = tab.compare
        qtbot.waitUntil(lambda: all(c.thumb.text() == "" for c in panel.cards), timeout=60000)
    assert panel.cards and not panel.cards[0].thumb.pixmap().isNull()
    qtbot.waitUntil(lambda: tab.thumbnails.pending_count() == 0, timeout=60000)
