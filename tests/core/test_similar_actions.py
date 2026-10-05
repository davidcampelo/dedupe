from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from dedupe.core import actions
from dedupe.core.actions import (
    PlanRefused,
    Status,
    execute,
    plan_similar_actions,
    similar_groups_after,
)
from dedupe.core.models import DeleteMode, FileEntry, SimilarGroup, SimilarMember

MakeTree = Callable[..., Path]


def entry(path: Path) -> FileEntry:
    st = path.stat()
    return FileEntry(path, st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev, st.st_mode)


def group(*paths: Path, aliases: dict[Path, tuple[Path, ...]] | None = None) -> SimilarGroup:
    aliases = aliases or {}
    members = tuple(SimilarMember(entry(p), 100, 100, 0, 1.0, aliases.get(p, ())) for p in paths)
    return SimilarGroup("g1", members)


@pytest.fixture
def root(make_tree: MakeTree) -> Path:
    return make_tree({"a.jpg": "aaaa", "b.jpg": "bbbbb", "c.jpg": "cccccc", "d.jpg": "dd"})


@pytest.fixture
def g(root: Path) -> SimilarGroup:
    return group(root / "a.jpg", root / "b.jpg", root / "c.jpg")


def test_a_valid_selection_plans_every_item_with_its_keepers(root: Path, g: SimilarGroup) -> None:
    plan = plan_similar_actions([g], [root / "b.jpg", root / "c.jpg"])
    assert [i.entry.path.name for i in plan.items] == ["b.jpg", "c.jpg"]
    assert all(i.similar for i in plan.items)
    assert [k.path.name for k in plan.items[0].keepers] == ["a.jpg"]
    assert plan.hardlink_possible is False


def test_hard_links_are_refused_outright(root: Path, g: SimilarGroup) -> None:
    with pytest.raises(PlanRefused) as e:
        plan_similar_actions([g], [root / "b.jpg"], DeleteMode.HARDLINK)
    assert "hard links are never used for similar images" in e.value.reasons[0]


def test_selecting_every_member_is_refused(root: Path, g: SimilarGroup) -> None:
    with pytest.raises(PlanRefused) as e:
        plan_similar_actions([g], [root / "a.jpg", root / "b.jpg", root / "c.jpg"])
    assert "at least one must be kept" in e.value.reasons[0]


def test_aliases_do_not_count_as_a_kept_copy(root: Path) -> None:
    # An exact copy elsewhere is not the group's image: the member itself must still stay.
    g = group(root / "a.jpg", root / "b.jpg", aliases={root / "a.jpg": (root / "d.jpg",)})
    with pytest.raises(PlanRefused):
        plan_similar_actions([g], [root / "a.jpg", root / "b.jpg"])


def test_protected_and_unknown_paths_are_refused_with_all_reasons(
    root: Path, g: SimilarGroup
) -> None:
    with pytest.raises(PlanRefused) as e:
        plan_similar_actions(
            [g], [root / "b.jpg", root / "d.jpg"], protected_folders=[str(root / "b.jpg")]
        )
    text = "; ".join(e.value.reasons)
    assert "d.jpg: not part of any similar group" in text
    assert "b.jpg: is in a protected folder" in text


def test_execution_trashes_selected_and_verifies_keepers(
    root: Path, g: SimilarGroup, tmp_path: Path
) -> None:
    plan = plan_similar_actions([g], [root / "b.jpg"])
    trashed: list[str] = []
    summary = execute(plan, log_path=tmp_path / "log", trash=trashed.append)
    assert summary.done == 1 and trashed == [str(root / "b.jpg")]


def test_execution_rechecks_every_selected_file(
    root: Path, g: SimilarGroup, tmp_path: Path
) -> None:
    plan = plan_similar_actions([g], [root / "b.jpg", root / "c.jpg"])
    (root / "c.jpg").write_text("changed afterwards")
    trashed: list[str] = []
    summary = execute(plan, log_path=tmp_path / "log", trash=trashed.append)
    assert trashed == [str(root / "b.jpg")]
    assert summary.count(Status.CHANGED) == 1


def test_execution_recheck_of_the_keeper_blocks_deletion(
    root: Path, g: SimilarGroup, tmp_path: Path
) -> None:
    plan = plan_similar_actions([g], [root / "b.jpg", root / "c.jpg"])
    (root / "a.jpg").unlink()  # the only keeper is gone: nothing may be deleted
    trashed: list[str] = []
    summary = execute(plan, log_path=tmp_path / "log", trash=trashed.append)
    assert trashed == [] and summary.count(Status.KEEPER_CHANGED) == 2


def test_execution_never_hard_links_a_similar_plan(
    root: Path, g: SimilarGroup, tmp_path: Path
) -> None:
    from dataclasses import replace

    plan = replace(plan_similar_actions([g], [root / "b.jpg"]), mode=DeleteMode.HARDLINK)
    summary = execute(plan, log_path=tmp_path / "log")
    assert summary.count(Status.FAILED) == 1
    assert os.stat(root / "b.jpg").st_ino != os.stat(root / "a.jpg").st_ino


# -- similar_groups_after -------------------------------------------------------------------


def test_removed_members_leave_the_group_and_small_groups_vanish(
    root: Path, g: SimilarGroup
) -> None:
    [left] = similar_groups_after([g], {root / "c.jpg"})
    assert [m.entry.path.name for m in left.members] == ["a.jpg", "b.jpg"]
    assert similar_groups_after([g], {root / "b.jpg", root / "c.jpg"}) == []


def test_removed_aliases_leave_the_members_that_stand_for_them(root: Path) -> None:
    g = group(
        root / "a.jpg",
        root / "b.jpg",
        aliases={root / "a.jpg": (root / "d.jpg", root / "x.jpg")},
    )
    [left] = similar_groups_after([g], {root / "d.jpg"})
    assert left.members[0].aliases == (root / "x.jpg",)
    assert similar_groups_after([g], set()) == [g]


# -- mutation checks: each guard is load-bearing --------------------------------------------


def _scenario_last_copy(root: Path, g: SimilarGroup) -> bool:
    try:
        plan_similar_actions([g], [root / "a.jpg", root / "b.jpg", root / "c.jpg"])
    except PlanRefused:
        return False
    return True


def _scenario_hardlink(root: Path, g: SimilarGroup) -> bool:
    try:
        plan_similar_actions([g], [root / "b.jpg"], DeleteMode.HARDLINK)
    except PlanRefused:
        return False
    return True


def _scenario_protected(root: Path, g: SimilarGroup) -> bool:
    try:
        plan_similar_actions([g], [root / "b.jpg"], protected_folders=[str(root)])
    except PlanRefused:
        return False
    return True


def _scenario_unknown(root: Path, g: SimilarGroup) -> bool:
    try:
        plan_similar_actions([g], [root / "d.jpg"])
    except PlanRefused:
        return False
    return True


MUTATIONS = [
    ("_check_last_copy_similar", _scenario_last_copy, lambda *a: None),
    ("_refuse_hardlink_for_similar", _scenario_hardlink, lambda *a: None),
    ("_check_protected", _scenario_protected, lambda *a: None),
    ("_check_known_paths", _scenario_unknown, lambda *a: None),
]


@pytest.mark.parametrize(("guard", "scenario", "neutered"), MUTATIONS)
def test_each_similar_guard_is_load_bearing(
    root: Path,
    g: SimilarGroup,
    monkeypatch: pytest.MonkeyPatch,
    guard: str,
    scenario: Callable[[Path, SimilarGroup], bool],
    neutered: Callable[..., object],
) -> None:
    assert scenario(root, g) is False, f"{guard} should block the scenario"
    monkeypatch.setattr(actions, guard, neutered)
    assert scenario(root, g) is True, f"disabling {guard} must change the outcome"


def test_the_execute_time_hardlink_guard_is_load_bearing(
    root: Path, g: SimilarGroup, tmp_path: Path
) -> None:
    from dataclasses import replace

    plan = replace(plan_similar_actions([g], [root / "b.jpg"]), mode=DeleteMode.HARDLINK)
    assert execute(plan, log_path=tmp_path / "log").count(Status.FAILED) == 1
    # the same plan without the similar flag is the exact-duplicate hard-link path
    unflagged = replace(
        plan,
        items=tuple(replace(i, similar=False, link_target=g.members[0].entry) for i in plan.items),
    )
    assert execute(unflagged, log_path=tmp_path / "log2").count(Status.DONE) == 1
