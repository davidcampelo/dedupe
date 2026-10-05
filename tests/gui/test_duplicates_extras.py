from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtGui import QGuiApplication
from pytestqt.qtbot import QtBot

from dedupe.core.models import DuplicateGroup, FileEntry, Recommendation, Verdict
from dedupe.core.recommender import recommend_all
from dedupe.core.settings import Settings, load_settings
from dedupe.gui import duplicates_view as dv
from dedupe.gui.duplicates_view import BulkRule, DuplicatesModel, DuplicatesTab, FileNode, GroupNode
from dedupe.gui.file_types import ARCHIVES, AUDIO, DOCUMENTS, IMAGES, OTHER, VIDEO, classify
from dedupe.gui.main_window import MainWindow
from dedupe.gui.view_options import SortKey, ViewOptions, select_groups
from tests.gui.conftest import UiWatchdog
from tests.gui.helpers import synthetic_groups


def make_group(
    name: str, size: int, copies: int, folder: str = "/d", mtime0: int = 1000
) -> DuplicateGroup:
    files = tuple(
        FileEntry(
            Path(
                f"{folder}/{name}{c}.{name.split('.')[-1]}"
                if "." in name
                else f"{folder}/{name}-{c}"
            ),
            size,
            mtime0 + c,
            hash((name, c)) & 0xFFFFF,
            1,
        )
        for c in range(copies)
    )
    recs = tuple(
        Recommendation(f.path, Verdict.KEEP if i == 0 else Verdict.DELETE, "r")
        for i, f in enumerate(files)
    )
    return DuplicateGroup(f"h-{name}", size, files, (), recs)


MIXED = (
    make_group("a.jpg", 100, 2, "/pics"),  # reclaimable 100
    make_group("b.mp4", 5000, 2, "/vid"),  # reclaimable 5000
    make_group("c.txt", 10, 6, "/docs"),  # reclaimable 50, most copies
    make_group("d.zip", 2000, 3, "/arch"),  # reclaimable 4000
    make_group("e.mp3", 30, 2, "/music"),
    make_group("f", 7, 2, "/misc"),
)


def names(groups: list[DuplicateGroup]) -> list[str]:
    return [g.hash.removeprefix("h-") for g in groups]


# -- pure helpers ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "category"),
    [
        ("a.JPG", IMAGES), ("a.heic", IMAGES), ("m.mkv", VIDEO), ("s.flac", AUDIO),
        ("d.pdf", DOCUMENTS), ("z.tar", ARCHIVES), ("x.unknown", OTHER), ("noext", OTHER),
    ],
)  # fmt: skip
def test_classify(name: str, category: str) -> None:
    assert classify(name) == category


def test_sort_orders() -> None:
    assert names(select_groups(MIXED, ViewOptions())) == [
        "b.mp4",
        "d.zip",
        "a.jpg",
        "c.txt",
        "e.mp3",
        "f",
    ]
    assert names(select_groups(MIXED, ViewOptions(SortKey.SIZE)))[:3] == ["b.mp4", "d.zip", "a.jpg"]
    assert names(select_groups(MIXED, ViewOptions(SortKey.COUNT)))[:2] == ["c.txt", "d.zip"]


def test_filters_keep_whole_groups() -> None:
    out = select_groups(MIXED, ViewOptions(file_type=VIDEO))
    assert names(out) == ["b.mp4"] and len(out[0].files) == 2
    assert names(select_groups(MIXED, ViewOptions(text="MUS"))) == ["e.mp3"]
    assert names(select_groups(MIXED, ViewOptions(file_type=IMAGES, text="vid"))) == []
    assert names(select_groups(MIXED, ViewOptions(file_type=OTHER))) == ["f"]
    assert ViewOptions().is_default and not ViewOptions(text="x").is_default


# -- tab: sort / filter / debounce -------------------------------------------------------------


@pytest.fixture
def tab(qtbot: QtBot) -> DuplicatesTab:
    t = DuplicatesTab()
    qtbot.addWidget(t)
    t.resize(1200, 600)
    t.show()
    return t


def load(qtbot: QtBot, tab: DuplicatesTab, groups, protected=(), root=None) -> None:  # type: ignore[no-untyped-def]
    with qtbot.waitSignal(tab.view_changed, timeout=10000):
        tab.set_groups(groups, protected, root)
    qtbot.waitUntil(lambda: not tab.model.loading, timeout=10000)


def shown(tab: DuplicatesTab) -> list[str]:
    return names(tab.model.groups())


def test_sort_combo_and_type_filter_rebuild_the_list(qtbot: QtBot, tab: DuplicatesTab) -> None:
    load(qtbot, tab, MIXED)
    assert shown(tab)[:2] == ["a.jpg", "b.mp4"]  # source order is kept for the default view
    with qtbot.waitSignal(tab.view_changed, timeout=5000):
        tab.sort_combo.setCurrentIndex(tab.sort_combo.findData(SortKey.COUNT))
    qtbot.waitUntil(lambda: not tab.model.loading)
    assert shown(tab)[:2] == ["c.txt", "d.zip"]
    with qtbot.waitSignal(tab.view_changed, timeout=5000):
        tab.type_combo.setCurrentIndex(tab.type_combo.findData(ARCHIVES))
    qtbot.waitUntil(lambda: not tab.model.loading)
    assert shown(tab) == ["d.zip"]
    assert "1 of 6 groups" in tab.summary.text()
    # children stay together under their group
    tab.model.expand(tab.model._groups[0])
    kinds = [type(tab.model.node_at(tab.model.index(r, 0))) for r in range(tab.model.rowCount())]
    assert kinds == [GroupNode, FileNode, FileNode, FileNode]


def test_text_filter_is_debounced_by_200ms(qtbot: QtBot, tab: DuplicatesTab) -> None:
    load(qtbot, tab, MIXED)
    emitted: list[int] = []
    tab.view_changed.connect(lambda: emitted.append(1))
    for text in ("m", "mu", "mus"):
        tab.filter_edit.setText(text)  # three quick keystrokes
    qtbot.wait(100)
    assert emitted == [] and tab.model.group_count == 6  # nothing yet
    qtbot.waitUntil(lambda: bool(emitted), timeout=3000)
    qtbot.waitUntil(lambda: not tab.model.loading)
    qtbot.wait(300)
    assert emitted == [1] and shown(tab) == ["e.mp3"]  # one rebuild for the whole burst
    assert tab.FILTER_DEBOUNCE_MS == 200


def test_user_choices_survive_filtering(qtbot: QtBot, tab: DuplicatesTab) -> None:
    load(qtbot, tab, MIXED)
    keep_path = Path("/pics/a.jpg0.jpg")
    assert tab.model.set_checked(keep_path, True)
    with qtbot.waitSignal(tab.view_changed, timeout=5000):
        tab.filter_edit.setText("vid")
        tab._options_changed()
    qtbot.waitUntil(lambda: not tab.model.loading)
    assert tab.model.file_node(keep_path) is None  # hidden...
    with qtbot.waitSignal(tab.view_changed, timeout=5000):
        tab.filter_edit.setText("")
        tab._options_changed()
    qtbot.waitUntil(lambda: not tab.model.loading)
    assert tab.model.is_checked(keep_path)  # ...but remembered


def test_stale_job_results_are_discarded(qtbot: QtBot, tab: DuplicatesTab) -> None:
    load(qtbot, tab, MIXED)
    tab.type_combo.setCurrentIndex(tab.type_combo.findData(VIDEO))
    tab.type_combo.setCurrentIndex(tab.type_combo.findData(ARCHIVES))  # supersedes the first
    qtbot.waitUntil(lambda: shown(tab) == ["d.zip"], timeout=5000)
    qtbot.wait(100)
    assert shown(tab) == ["d.zip"]


# -- bulk rules --------------------------------------------------------------------------------


def one_keep_per_group(tab: DuplicatesTab) -> bool:
    for g in tab.model._groups:
        deleted = sum(f.checked for f in g.files)
        if len(g.files) - deleted != 1:
            return False
    return True


@pytest.mark.parametrize("rule", [BulkRule.SUGGESTED, BulkRule.NEWEST, BulkRule.FOLDER])
def test_every_bulk_rule_leaves_exactly_one_keep_per_group(
    qtbot: QtBot, tab: DuplicatesTab, rule: BulkRule
) -> None:
    groups = synthetic_groups(40, copies=4)
    load(qtbot, tab, groups)
    folder = "/data/dir3"
    with qtbot.waitSignal(
        tab.model.overrides_applied, timeout=10000, raising=rule is not BulkRule.SUGGESTED
    ):
        tab.apply_rule(rule, folder)
    qtbot.waitUntil(lambda: one_keep_per_group(tab), timeout=5000)
    if rule is BulkRule.NEWEST:
        assert all(not g.files[-1].checked and g.files[0].checked for g in tab.model._groups)
    if rule is BulkRule.FOLDER:
        in_folder = [
            g for g in tab.model._groups if str(g.files[0].entry.path).startswith(folder + "/")
        ]
        assert in_folder and all(not g.files[0].checked for g in in_folder)


def test_deselect_everything_and_suggested_roundtrip(qtbot: QtBot, tab: DuplicatesTab) -> None:
    load(qtbot, tab, synthetic_groups(5, copies=3))
    assert tab.model.selected_count == 10
    tab.apply_rule(BulkRule.NONE)
    assert tab.model.selected_count == 0 and not tab.delete_button.isEnabled()
    tab.apply_rule(BulkRule.SUGGESTED)
    assert tab.model.selected_count == 10 and tab.delete_button.isEnabled()
    assert tab.model.live_overrides() == {}


def test_keep_in_folder_with_no_copy_there_changes_nothing(
    qtbot: QtBot, tab: DuplicatesTab
) -> None:
    load(qtbot, tab, synthetic_groups(5, copies=2))
    before = tab.model.selected_paths()
    with qtbot.waitSignal(tab.model.overrides_applied, timeout=5000):
        tab.apply_rule(BulkRule.FOLDER, "/nowhere")
    assert tab.model.selected_paths() == before


def test_apply_overrides_in_slices_stays_consistent(qtbot: QtBot) -> None:
    model = DuplicatesModel()
    with qtbot.waitSignal(model.load_finished, timeout=20000):
        model.set_groups(synthetic_groups(3000, copies=3))
    changes = {g.files[1].path: False for g in synthetic_groups(3000, copies=3)}
    with qtbot.waitSignal(model.overrides_applied, timeout=20000):
        model.apply_overrides(changes)
    assert model.selected_count == 3000  # only the third copy of each group is still selected
    assert model.selected_count == len(model.selected_paths())


# -- context menu / details ----------------------------------------------------------------------


def file_node(tab: DuplicatesTab, g: int, f: int) -> FileNode:
    group = tab.model._groups[g]
    tab.model.expand(group)
    node = tab.model.node_at(tab.model.index(group.row + 1 + f, 0))
    assert isinstance(node, FileNode)
    return node


def test_context_menu_actions(
    qtbot: QtBot, tab: DuplicatesTab, monkeypatch: pytest.MonkeyPatch
) -> None:
    load(qtbot, tab, synthetic_groups(2, copies=3))
    opened: list[str] = []
    monkeypatch.setattr(
        dv.QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()) or True
    )
    node = file_node(tab, 0, 1)
    menu = tab.view.build_menu(node)
    actions = {a.text(): a for a in menu.actions() if a.text()}
    assert list(actions) == [
        "Open file",
        "Open containing folder",
        "Copy path",
        "Mark as Keep",
        "Mark folder as Protected",
    ]
    actions["Open file"].trigger()
    actions["Open containing folder"].trigger()
    assert opened == [str(node.entry.path), str(node.entry.path.parent)]
    actions["Copy path"].trigger()
    assert QGuiApplication.clipboard().text() == str(node.entry.path)
    with qtbot.waitSignal(tab.folder_protected, timeout=1000) as blocker:
        actions["Mark folder as Protected"].trigger()
    assert blocker.args[0] == node.entry.path.parent
    group_menu = tab.view.build_menu(tab.model._groups[0])
    assert [a.text() for a in group_menu.actions()] == ["Collapse"]
    assert tab.view.build_menu(None).isEmpty()


def test_mark_as_keep_makes_that_copy_the_only_keeper(qtbot: QtBot, tab: DuplicatesTab) -> None:
    load(qtbot, tab, synthetic_groups(1, copies=3))
    node = file_node(tab, 0, 2)  # a Delete row
    assert node.checked
    menu_actions = {a.text(): a for a in tab.view.build_menu(node).actions()}
    menu_actions["Mark as Keep"].trigger()
    assert [f.checked for f in tab.model._groups[0].files] == [True, True, False]
    assert menu_actions["Mark as Keep"].isEnabled()


def test_details_panel_follows_the_current_row(qtbot: QtBot, tab: DuplicatesTab) -> None:
    load(qtbot, tab, synthetic_groups(1, copies=3, size=2048))
    node = file_node(tab, 0, 1)
    tab.view.setCurrentIndex(tab.model.index(node.row, 0))
    d = tab.details.fields
    assert d["Path"].text() == str(node.entry.path)
    assert "2.0 KiB" in d["Size"].text() and d["Hash"].text() == node.group.group.hash
    assert d["Modified"].text() and d["Reason"].text().startswith("Duplicate of")
    assert d["Permissions"].text().startswith("----------") or "(" in d["Permissions"].text()
    tab.view.setCurrentIndex(tab.model.index(0, 0))
    assert tab.details.title.text() == "Group of 3 identical files"
    tab.view.setCurrentIndex(tab.model.index(99, 0))
    assert tab.details.title.text().startswith("Select a file")


# -- protected folders (window level) -------------------------------------------------------------


def test_marking_a_folder_protected_updates_every_affected_group(
    qtbot: QtBot, tmp_path: Path
) -> None:
    window = MainWindow(Settings(exclude=(), use_cache=False))
    qtbot.addWidget(window)
    window.show()
    window.settings_path = tmp_path / "settings.toml"
    tab = window.duplicates_tab
    root = Path("/data")
    groups = recommend_all(synthetic_groups(30, copies=3), (), root)
    with qtbot.waitSignal(tab.view_changed, timeout=10000):
        tab.set_groups(groups, (), root)
    qtbot.waitUntil(lambda: not tab.model.loading)
    assert tab.model.selected_count == 60
    folder = Path("/data/dir5/g5")  # every copy of group 5 lives here? no: only the folder's files
    with qtbot.waitSignal(tab.view_changed, timeout=10000):
        tab.view.protect_folder_requested.emit(folder)
    qtbot.waitUntil(lambda: not tab.model.loading, timeout=10000)
    # all three copies of group g5 are inside the folder: they are now protected and kept
    g5 = next(g for g in tab.model._groups if g.group.files[0].path.parent == folder)
    assert all(f.protected and not f.checked for f in g5.files)
    assert tab.model.selected_count == 58
    qtbot.waitUntil(lambda: (tmp_path / "settings.toml").exists(), timeout=5000)
    assert load_settings(tmp_path / "settings.toml").settings.protected_folders == (str(folder),)
    assert window.settings.protected_folders == (str(folder),)


def test_protected_folder_rerun_keeps_user_choices(qtbot: QtBot, tab: DuplicatesTab) -> None:
    groups = recommend_all(synthetic_groups(4, copies=3), (), Path("/data"))
    load(qtbot, tab, groups, root=Path("/data"))
    tab.model.set_checked(Path("/data/dir0/g0/copy0.bin"), True)
    with qtbot.waitSignal(tab.view_changed, timeout=10000):
        tab.set_protected(["/data/dir2"])
    qtbot.waitUntil(lambda: not tab.model.loading)
    assert tab.model.is_checked(Path("/data/dir0/g0/copy0.bin"))
    assert not tab.model.is_checked(Path("/data/dir2/g2/copy1.bin"))


def test_remove_paths_updates_the_full_list_even_when_filtered(
    qtbot: QtBot, tab: DuplicatesTab
) -> None:
    load(qtbot, tab, MIXED)
    with qtbot.waitSignal(tab.view_changed, timeout=5000):
        tab.type_combo.setCurrentIndex(tab.type_combo.findData(VIDEO))
    qtbot.waitUntil(lambda: not tab.model.loading)
    gone = frozenset({Path("/vid/b.mp41.mp4")})
    with qtbot.waitSignal(tab.source_changed, timeout=5000):
        tab.remove_paths(gone)
    qtbot.waitUntil(lambda: tab.model.group_count == 0 and not tab.model.loading, timeout=5000)
    assert tab.source_group_count == 5  # the hidden groups are still there
    assert tab.source_reclaimable == 100 + 50 + 4000 + 30 + 7
    with qtbot.waitSignal(tab.view_changed, timeout=5000):
        tab.type_combo.setCurrentIndex(0)
    qtbot.waitUntil(lambda: tab.model.group_count == 5 and not tab.model.loading, timeout=5000)


@pytest.mark.slow
def test_filtering_100k_files_never_stalls_the_gui(qtbot: QtBot, ui_watchdog: UiWatchdog) -> None:
    groups = synthetic_groups(30000, copies=3) + synthetic_groups(2500, copies=4, start=30000)
    t = DuplicatesTab()
    qtbot.addWidget(t)
    t.show()
    with qtbot.waitSignal(t.view_changed, timeout=60000):
        t.set_groups(groups)
    qtbot.waitUntil(lambda: not t.model.loading, timeout=60000)
    with ui_watchdog.watch():
        with qtbot.waitSignal(t.view_changed, timeout=60000):
            t.filter_edit.setText("dir7/")
        qtbot.waitUntil(lambda: not t.model.loading, timeout=60000)
        assert 0 < t.model.group_count < 32500
        with qtbot.waitSignal(t.view_changed, timeout=60000):
            t.sort_combo.setCurrentIndex(t.sort_combo.findData(SortKey.COUNT))
        qtbot.waitUntil(lambda: not t.model.loading, timeout=60000)
        t.apply_rule(BulkRule.NEWEST)
        qtbot.waitUntil(lambda: t.model.selected_count > 0, timeout=60000)
        qtbot.wait(200)
