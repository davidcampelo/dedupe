from __future__ import annotations

import os
import random
import shutil
from pathlib import Path

import pytest
from PIL import Image

from dedupe.core.models import (
    CancelToken,
    Progress,
    ScanOptions,
    ScanResult,
    Stage,
    Verdict,
)
from dedupe.core.pipeline import run_scan
from tests.core.imagegen import photo, save

pytest.importorskip("imagehash")

ON = ScanOptions(exclude=(), similar_images=True)


@pytest.fixture
def pics(tmp_path: Path) -> Path:
    root = tmp_path / "pics"
    big = photo(1, (640, 480))
    save(big, root / "big.png")
    save(big.resize((320, 240), Image.Resampling.LANCZOS), root / "small.png")
    save(big, root / "q50.jpg", quality=50)
    save(photo(2), root / "other.png")
    return root


def names(result: ScanResult) -> list[list[str]]:
    return [[m.entry.path.name for m in g.members] for g in result.similar_groups]


def test_off_by_default(pics: Path) -> None:
    result = run_scan(pics, ScanOptions(exclude=()))
    assert result.similar_groups == ()


def test_resized_and_recompressed_copies_group_together(pics: Path) -> None:
    result = run_scan(pics, ON)
    [group] = result.similar_groups
    assert {m.entry.path.name for m in group.members} == {"big.png", "small.png", "q50.jpg"}
    assert "other.png" not in {m.entry.path.name for m in group.members}


def test_keeper_is_the_highest_resolution(pics: Path) -> None:
    [group] = run_scan(pics, ON).similar_groups
    keep = [r for r in group.recommendations if r.verdict is Verdict.KEEP]
    best = max(group.members, key=lambda m: (m.pixels, m.entry.size))
    assert keep[0].path == best.entry.path
    assert group.members[0].distance == 0
    assert group.members[0].similarity == 1.0
    assert group.reclaimable == sum(
        m.entry.size for m in group.members if m.entry.path != best.entry.path
    )


def test_exact_copies_collapse_into_aliases_and_never_appear_in_both_lists(pics: Path) -> None:
    shutil.copy(pics / "small.png", pics / "small copy.png")
    os.link(pics / "q50.jpg", pics / "q50 link.jpg")
    result = run_scan(pics, ON)
    [group] = result.similar_groups
    in_similar = {m.entry.path for m in group.members} | {
        a for m in group.members for a in m.aliases
    }
    assert {p.name for p in in_similar} == {
        "big.png",
        "small.png",
        "small copy.png",
        "q50.jpg",
        "q50 link.jpg",
    }
    assert len(group.members) == 3  # five files, three distinct images
    by_name = {m.entry.path.name: m for m in group.members}
    # the recommender keeps the original, so the "copy" is the alias
    assert [a.name for a in by_name["small.png"].aliases] == ["small copy.png"]
    linked = by_name["q50 link.jpg"]  # hard links: the first path in sorted order stands in
    assert [a.name for a in linked.aliases] == ["q50.jpg"]
    # the exact pair is still on the Duplicates tab, the similar groups only hold one of them
    [exact] = result.groups
    assert {f.path.name for f in exact.files} == {"small.png", "small copy.png"}
    members = {m.entry.path for m in group.members}
    assert len(members & {f.path for f in exact.files}) == 1


def test_result_does_not_depend_on_file_order(pics: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import dedupe.core.pipeline as pipeline

    expected = run_scan(pics, ON).similar_groups
    real = pipeline.scan_files

    def shuffled(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        entries, skipped = real(*args, **kwargs)  # type: ignore[arg-type]
        random.Random(4).shuffle(entries)
        return entries, skipped

    monkeypatch.setattr(pipeline, "scan_files", shuffled)
    assert run_scan(pics, ON).similar_groups == expected


def test_cancelled_scan_returns_no_groups(pics: Path) -> None:
    token = CancelToken()

    def progress(p: Progress) -> None:
        if p.stage is Stage.SIMILAR:
            token.cancel()

    result = run_scan(pics, ON, progress, token)
    assert result.cancelled and result.similar_groups == ()


def test_protected_image_is_never_marked_delete(pics: Path, tmp_path: Path) -> None:
    protected = tmp_path / "keepers"
    save(Image.open(pics / "small.png"), protected / "precious.png")
    options = ScanOptions(exclude=(), similar_images=True, protected_folders=(str(protected),))
    result = run_scan(tmp_path, options)
    [group] = [
        g for g in result.similar_groups if any("precious" in m.entry.path.name for m in g.members)
    ]
    verdicts = {r.path.name: r.verdict for r in group.recommendations}
    assert verdicts["precious.png"] is Verdict.KEEP
    assert Verdict.DELETE in verdicts.values()


def test_undecodable_image_is_skipped_with_a_reason_and_the_scan_continues(pics: Path) -> None:
    (pics / "broken.jpg").write_bytes(b"not an image at all")
    result = run_scan(pics, ON)
    assert any(s.path.endswith("broken.jpg") and "unsupported" in s.reason for s in result.skipped)
    assert len(result.similar_groups) == 1


def test_flat_images_never_match_each_other(tmp_path: Path) -> None:
    for i, colour in enumerate(["red", "blue", "green"]):
        save(Image.new("RGB", (80, 80), colour), tmp_path / f"flat{i}.png")
    assert run_scan(tmp_path, ON).similar_groups == ()


def test_progress_is_reported_in_images(pics: Path) -> None:
    seen: list[Progress] = []
    run_scan(pics, ON, seen.append)
    hashing = [p for p in seen if p.stage is Stage.SIMILAR]
    assert (hashing and hashing[-1].total >= 4) or any(p.total == 4 for p in hashing)


def test_unavailable_extra_is_a_warning_not_a_failure(
    pics: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dedupe.core.pipeline as pipeline

    monkeypatch.setattr(pipeline, "similar_available", lambda: False)
    result = run_scan(pics, ON)
    assert result.similar_groups == () and any("dedupe[similar]" in w for w in result.warnings)


@pytest.mark.parametrize("threshold", [0, 16])
def test_threshold_extremes_still_work(pics: Path, threshold: int) -> None:
    options = ScanOptions(exclude=(), similar_images=True, similarity_threshold=threshold)
    result = run_scan(pics, options)
    assert all(len(g.members) >= 2 for g in result.similar_groups)
