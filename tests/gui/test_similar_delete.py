from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image
from pytestqt.qtbot import QtBot

from dedupe.core.actions import plan_similar_actions
from dedupe.core.models import DeleteMode
from dedupe.gui.delete_dialog import SIMILAR_WARNING, DeleteDialog
from tests.core.imagegen import photo, save
from tests.gui.helpers import synthetic_similar_groups
from tests.gui.test_delete_flow import Harness


@pytest.fixture
def harness(qtbot: QtBot, tmp_path: Path) -> Harness:
    h = Harness(qtbot, tmp_path)
    h.window.settings = replace(h.window.settings, similar_images=True)
    return h


@pytest.fixture
def pics(tmp_path: Path) -> Path:
    root = tmp_path / "pics"
    big = photo(1, (640, 480))
    save(big, root / "big.png")
    save(big.resize((320, 240), Image.Resampling.LANCZOS), root / "small.png")
    shutil.copy(root / "small.png", root / "small copy.png")
    save(photo(2), root / "other.png")
    return root


def scan(h: Harness, root: Path) -> None:
    h.scan(root)
    h.qtbot.waitUntil(lambda: not h.window.similar_tab.model.loading, timeout=30000)


def finish(h: Harness, start: object) -> None:
    w = h.window
    before = len(h.summaries) + len(h.refusals) + len(h.plans)
    start()  # type: ignore[operator]
    h.qtbot.waitUntil(
        lambda: not w.busy and len(h.summaries) + len(h.refusals) + len(h.plans) > before,
        timeout=60000,
    )
    h.qtbot.waitUntil(
        lambda: not w.similar_tab.model.loading and not w.duplicates_tab.model.loading,
        timeout=30000,
    )


def delete_similar(h: Harness) -> None:
    finish(h, h.window.request_similar_delete)


def names(paths: set[Path]) -> set[str]:
    return {p.name for p in paths}


# -- dialog -----------------------------------------------------------------------------------


def similar_plan(n: int = 2):  # type: ignore[no-untyped-def]
    g = synthetic_similar_groups(1, n)[0]
    return plan_similar_actions([g], [m.entry.path for m in g.members[1:]])


def test_dialog_warns_that_similar_is_not_identical_and_offers_no_hard_links(
    qtbot: QtBot,
) -> None:
    dlg = DeleteDialog(similar_plan(3), default_mode=DeleteMode.HARDLINK)
    qtbot.addWidget(dlg)
    dlg.show()
    assert dlg.similar_warning.isVisible() and SIMILAR_WARNING in dlg.similar_warning.text()
    assert "similar, not identical" in dlg.similar_warning.text()
    assert "content will not exist anywhere else" in dlg.similar_warning.text()
    assert dlg.hardlink_radio.isHidden() and not dlg.hardlink_radio.isEnabled()
    assert dlg.mode is DeleteMode.TRASH  # the hard-link default fell back to the safe one
    assert "At least one image of every group is always kept" in dlg.summary_label.text()


def test_the_exact_duplicate_dialog_is_unchanged(qtbot: QtBot, tmp_path: Path) -> None:
    from dedupe.core.actions import plan_actions
    from dedupe.core.pipeline import run_scan

    root = tmp_path / "t"
    for d in "abc":
        (root / d).mkdir(parents=True)
        (root / d / "x.txt").write_text("same")
    groups = run_scan(root).groups
    dlg = DeleteDialog(plan_actions(groups, [root / "b/x.txt"]))
    qtbot.addWidget(dlg)
    dlg.show()
    assert not dlg.similar_warning.isVisible() and not dlg.hardlink_radio.isHidden()
    assert "One copy of every group is always kept" in dlg.summary_label.text()


# -- flow -------------------------------------------------------------------------------------


def test_scan_fills_both_tabs_and_the_similar_group_collapses_exact_copies(
    harness: Harness, pics: Path
) -> None:
    scan(harness, pics)
    w = harness.window
    [group] = w.similar_tab.model.groups()
    assert {m.entry.path.name for m in group.members} == {"big.png", "small.png"}
    small = next(m for m in group.members if m.entry.path.name == "small.png")
    assert names(set(small.aliases)) == {"small copy.png"}
    [exact] = w.duplicates_tab.model.groups()
    assert {f.path.name for f in exact.files} == {"small.png", "small copy.png"}
    assert w.similar_label.text() == "Similar groups: 1"
    assert w.similar_tab.model.selected_count == 0  # nothing pre-selected


def test_deleting_a_similar_image_updates_both_tabs(harness: Harness, pics: Path) -> None:
    scan(harness, pics)
    w = harness.window
    assert w.similar_tab.model.set_checked(pics / "small.png", True)
    delete_similar(harness)
    assert harness.trash.calls == 1
    assert not (pics / "small.png").exists() and (pics / "big.png").exists()
    assert (pics / "small copy.png").exists()  # its exact copy was never touched
    assert w.similar_tab.model.group_count == 0  # one member left: no longer a group
    assert w.duplicates_tab.model.group_count == 0  # the exact pair lost a file too
    assert w.similar_label.text() == "Similar groups: 0"
    assert "1 files moved to Trash" in harness.summaries[-1]


def test_deleting_an_exact_copy_on_the_duplicates_tab_updates_the_aliases(
    harness: Harness, pics: Path
) -> None:
    scan(harness, pics)
    w = harness.window
    assert w.duplicates_tab.model.selected_paths() == {pics / "small copy.png"}
    harness.delete()
    harness.qtbot.waitUntil(lambda: not w.similar_tab.model.loading, timeout=30000)
    harness.qtbot.waitUntil(lambda: w.similar_tab.model.group_count == 1, timeout=30000)
    [group] = w.similar_tab.model.groups()
    small = next(m for m in group.members if m.entry.path.name == "small.png")
    assert small.aliases == ()
    assert not (pics / "small copy.png").exists()


def test_the_last_image_of_a_group_cannot_be_selected_for_deletion(
    harness: Harness, pics: Path
) -> None:
    scan(harness, pics)
    w = harness.window
    for name in ("big.png", "small.png"):
        w.similar_tab.model.set_checked(pics / name, True)
    delete_similar(harness)
    assert harness.refusals and "at least one must be kept" in harness.refusals[-1][0]
    assert harness.plans == [] and harness.trash.calls == 0
    assert (pics / "big.png").exists() and (pics / "small.png").exists()


def test_the_confirmation_plan_is_flagged_similar_and_rejecting_it_changes_nothing(
    harness: Harness, pics: Path
) -> None:
    scan(harness, pics)
    w = harness.window
    w.similar_tab.model.set_checked(pics / "small.png", True)
    harness.choice = None
    delete_similar(harness)
    [plan] = harness.plans
    assert all(i.similar for i in plan.items)  # type: ignore[attr-defined]
    assert harness.trash.calls == 0 and (pics / "small.png").exists()
    assert w.similar_tab.model.selected_count == 1  # the selection is untouched


def test_a_dry_run_changes_nothing(harness: Harness, pics: Path) -> None:
    from dedupe.gui.delete_dialog import DeleteChoice

    scan(harness, pics)
    w = harness.window
    w.similar_tab.model.set_checked(pics / "small.png", True)
    harness.choice = DeleteChoice(DeleteMode.TRASH, True)
    delete_similar(harness)
    assert harness.trash.calls == 0 and (pics / "small.png").exists()
    assert w.similar_tab.model.group_count == 1


def test_the_similar_search_off_leaves_the_tab_empty(
    qtbot: QtBot, tmp_path: Path, pics: Path
) -> None:
    h = Harness(qtbot, tmp_path)  # default settings: similar images off
    h.scan(pics)
    assert h.window.similar_tab.model.group_count == 0
    assert "Similar-image search is off" in h.window.similar_tab.summary.text()
