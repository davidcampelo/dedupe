from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from dedupe.core.models import SimilarGroup
from dedupe.gui.main_window import MainWindow
from dedupe.gui.similar_view import (
    COL_DIMS,
    COL_EXACT,
    COL_ITEM,
    COL_SIMILAR,
    COL_SIZE,
    COL_SUGGEST,
    OFF_TEXT,
    WARNING_TEXT,
    MemberNode,
    SimilarGroupNode,
    SimilarImagesTab,
    SimilarModel,
)
from tests.gui.conftest import UiWatchdog
from tests.gui.helpers import synthetic_similar_groups


def make_tab(
    qtbot: QtBot, groups: tuple[SimilarGroup, ...], protected: tuple[str, ...] = ()
) -> SimilarImagesTab:
    tab = SimilarImagesTab()
    qtbot.addWidget(tab)
    tab.show()
    tab.set_groups(groups, protected, Path("/pics"))
    qtbot.waitUntil(lambda: not tab.model.loading, timeout=10000)
    return tab


def cell(
    model: SimilarModel, row: int, col: int, role: int = Qt.ItemDataRole.DisplayRole
) -> object:
    return model.index(row, col).data(role)


def test_five_thousand_groups_load_without_blocking_the_gui_thread(
    qtbot: QtBot, ui_watchdog: UiWatchdog
) -> None:
    groups = synthetic_similar_groups(5000)
    tab = SimilarImagesTab()
    qtbot.addWidget(tab)
    tab.show()
    with ui_watchdog.watch():
        tab.set_groups(groups, (), Path("/pics"))
        qtbot.waitUntil(lambda: not tab.model.loading, timeout=30000)
        tab.model.expand_all()
        tab.model.collapse_all()
    assert tab.model.group_count == 5000 and tab.model.image_count == 15000


def test_nothing_is_preselected_even_though_the_recommender_suggests_deletions(
    qtbot: QtBot,
) -> None:
    tab = make_tab(qtbot, synthetic_similar_groups(10))
    m = tab.model
    assert m.selected_count == 0 and m.selected_size == 0 and m.selected_paths() == set()
    assert not tab.delete_button.isEnabled()
    node = m._groups[0].members[1]
    assert node.suggested and not node.checked  # suggested for deletion, still unchecked


def test_rows_show_dimensions_size_similarity_and_exact_copies(qtbot: QtBot) -> None:
    tab = make_tab(qtbot, synthetic_similar_groups(2, aliases_on_first=2))
    m = tab.model
    m.expand_all()
    assert "3 similar images" in str(cell(m, 0, COL_ITEM))
    assert cell(m, 1, COL_DIMS) == "4000 × 3000"
    assert cell(m, 1, COL_SIZE) == "48.8 KiB"
    assert cell(m, 1, COL_SIMILAR) == "reference"
    assert cell(m, 1, COL_EXACT) == "+2"
    assert cell(m, 2, COL_SIMILAR) == "96%" and cell(m, 2, COL_EXACT) == ""
    assert cell(m, 1, COL_SUGGEST) == "Suggest keep"
    assert cell(m, 2, COL_SUGGEST) == "Suggest delete"


def test_the_tab_explains_that_the_images_are_not_identical(qtbot: QtBot) -> None:
    tab = make_tab(qtbot, synthetic_similar_groups(1))
    assert tab.warning.text() == WARNING_TEXT and "not identical" in WARNING_TEXT


def test_checking_images_selects_them_and_updates_the_summary_and_button(qtbot: QtBot) -> None:
    tab = make_tab(qtbot, synthetic_similar_groups(3))
    m = tab.model
    m.expand_all()
    assert m.set_checked(Path("/pics/dir0/g0/img1.jpg"), True)
    assert (m.selected_count, m.selected_size) == (1, 43_000)
    assert tab.delete_button.isEnabled() and "1 images selected" in tab.summary.text()
    index = m.index(2, COL_ITEM)
    assert index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
    assert m.setData(m.index(3, COL_ITEM), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
    assert m.selected_count == 2
    assert not m.set_checked(Path("/nowhere.jpg"), True)


def test_protected_images_cannot_be_selected(qtbot: QtBot) -> None:
    tab = make_tab(qtbot, synthetic_similar_groups(1), protected=("/pics/dir0",))
    m = tab.model
    node = m.file_node(Path("/pics/dir0/g0/img1.jpg"))
    assert node is not None and node.protected
    assert not m.set_checked(node.entry.path, True)
    m.select_suggested()
    assert m.selected_count == 0
    m.expand_all()
    assert cell(m, 2, COL_SUGGEST) == "Protected"
    assert m.index(2, COL_ITEM).data(Qt.ItemDataRole.CheckStateRole) is None


def test_select_suggested_none_and_keep_this_one_are_explicit_user_actions(qtbot: QtBot) -> None:
    tab = make_tab(qtbot, synthetic_similar_groups(4))
    m = tab.model
    tab.suggested_button.click()
    assert m.selected_count == 8  # two suggested deletions in each of four groups
    tab.none_button.click()
    assert m.selected_count == 0
    m.mark_keep(Path("/pics/dir1/g1/img2.jpg"))  # keep the lowest-resolution one instead
    assert m.selected_paths() == {Path("/pics/dir1/g1/img0.jpg"), Path("/pics/dir1/g1/img1.jpg")}


def test_expand_collapse_and_keyboard(qtbot: QtBot) -> None:
    tab = make_tab(qtbot, synthetic_similar_groups(2))
    v, m = tab.view, tab.model
    v.setCurrentIndex(m.index(0, COL_ITEM))
    QTest.keyClick(v, Qt.Key.Key_Right)
    assert m.rowCount() == 5
    QTest.keyClick(v, Qt.Key.Key_Down)  # first image
    QTest.keyClick(v, Qt.Key.Key_Space)
    assert m.selected_count == 1
    QTest.keyClick(v, Qt.Key.Key_Space)
    assert m.selected_count == 0
    QTest.keyClick(v, Qt.Key.Key_Left)
    assert m.rowCount() == 2
    tab.expand_button.click()
    assert m.rowCount() == 8
    tab.collapse_button.click()
    assert m.rowCount() == 2


def test_delete_key_asks_to_delete_only_with_a_selection(qtbot: QtBot) -> None:
    tab = make_tab(qtbot, synthetic_similar_groups(1))
    asked: list[bool] = []
    tab.delete_requested.connect(lambda: asked.append(True))
    tab.view.setCurrentIndex(tab.model.index(0, 0))
    QTest.keyClick(tab.view, Qt.Key.Key_Delete)
    assert asked == []
    tab.model.set_checked(Path("/pics/dir0/g0/img1.jpg"), True)
    QTest.keyClick(tab.view, Qt.Key.Key_Delete)
    assert asked == [True]


def test_selecting_a_group_shows_the_details_and_the_compare_cards(qtbot: QtBot) -> None:
    tab = make_tab(qtbot, synthetic_similar_groups(2, aliases_on_first=2))
    tab.view.setCurrentIndex(tab.model.index(0, 0))
    assert tab.details.title.text() == "Group of 3 similar images"
    assert tab.compare.heading.text() == "Compare 3 similar images"
    cards = tab.compare.cards
    assert len(cards) == 3
    assert cards[0].match.text() == "Reference image" and cards[1].match.text() == "96% similar"
    assert cards[0].exact.text() == "+2 exact copies" and cards[1].exact.text() == ""
    assert cards[1].resolution.text() == "Resolution: 3200 × 2400"
    assert "48.8 KiB" in cards[0].size_label.text()
    tab.model.expand_all()
    tab.view.setCurrentIndex(tab.model.index(1, 0))
    assert tab.details.fields["Dimensions"].text() == "4000 × 3000"
    assert "2 exact copies" in tab.details.fields["Copies"].text()
    assert tab.details.fields["Similarity"].text() == "reference image"


def test_card_checkbox_and_list_stay_in_sync(qtbot: QtBot) -> None:
    tab = make_tab(qtbot, synthetic_similar_groups(1))
    tab.view.setCurrentIndex(tab.model.index(0, 0))
    card = tab.compare.cards[1]
    card.delete_box.setChecked(True)
    assert tab.model.selected_paths() == {card.entry.path}
    tab.model.clear_selection()
    assert not card.delete_box.isChecked()


def test_without_a_similar_search_the_tab_says_how_to_turn_it_on(qtbot: QtBot) -> None:
    tab = SimilarImagesTab()
    qtbot.addWidget(tab)
    assert tab.summary.text() == OFF_TEXT
    tab.set_groups((), (), None, searched=False)
    assert tab.summary.text() == OFF_TEXT
    tab.set_groups((), (), None, searched=True)
    assert tab.summary.text() == "No similar images found."


def test_main_window_has_the_fourth_tab_with_its_icon(qtbot: QtBot) -> None:
    w = MainWindow()
    qtbot.addWidget(w)
    assert w.tabs.count() == 4 and w.tabs.tabText(3) == "Similar Images"
    assert not w.tabs.tabIcon(3).isNull()
    assert isinstance(w.similar_tab, SimilarImagesTab)


def test_group_and_member_nodes_are_distinguishable(qtbot: QtBot) -> None:
    tab = make_tab(qtbot, synthetic_similar_groups(1))
    tab.model.expand_all()
    assert isinstance(SimilarModel.node_at(tab.model.index(0, 0)), SimilarGroupNode)
    assert isinstance(SimilarModel.node_at(tab.model.index(1, 0)), MemberNode)
    assert SimilarModel.node_at(tab.model.index(99, 0)) is None
