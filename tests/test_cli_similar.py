from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from dedupe import cli
from dedupe.cli import main
from tests.core.imagegen import photo, save

pytest.importorskip("imagehash")


@pytest.fixture
def pics(tmp_path: Path) -> Path:
    root = tmp_path / "pics"
    big = photo(1, (640, 480))
    save(big, root / "big.png")
    save(big.resize((320, 240), Image.Resampling.LANCZOS), root / "small.png")
    save(photo(2), root / "other.png")
    return root


def test_json_has_similar_groups_when_asked(pics: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scan", str(pics), "--json", "--similar"]) == 0
    data = json.loads(capsys.readouterr().out)
    [group] = data["similar_groups"]
    assert set(group) == {"id", "reclaimable", "members"}
    assert {Path(m["path"]).name for m in group["members"]} == {"big.png", "small.png"}
    member = group["members"][0]
    assert set(member) == {
        "path", "size", "mtime_ns", "width", "height", "distance",
        "similarity", "aliases", "verdict", "reason",
    }  # fmt: skip
    assert data["groups"] == []  # the exact-duplicate keys are untouched


def test_json_is_byte_identical_to_before_when_similar_is_off(
    pics: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["scan", str(pics), "--json"]) == 0
    off = capsys.readouterr().out
    assert "similar" not in off
    assert set(json.loads(off)) == {
        "root", "files_scanned", "reclaimable", "cancelled", "groups",
        "empty_files", "hardlink_sets", "skipped",
    }  # fmt: skip


def test_table_has_a_similar_section(pics: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scan", str(pics), "--similar"]) == 0
    out = capsys.readouterr().out
    assert "Similar images: 1 groups" in out
    assert "% similar" in out and "big.png" in out and "[KEEP]" in out


def test_table_without_similar_has_no_section(
    pics: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["scan", str(pics)]) == 0
    assert "Similar images" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("value", "bits"), [("strict", 4), ("Normal", 8), ("loose", 12), ("0", 0), ("16", 16)]
)
def test_threshold_values(value: str, bits: int) -> None:
    assert cli._threshold(value) == bits


@pytest.mark.parametrize("bad", ["17", "-1", "huge", ""])
def test_bad_threshold_is_a_usage_error(
    bad: str, pics: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as e:
        main(["scan", str(pics), "--similar", "--threshold", bad])
    assert e.value.code == 2
    assert "strict, normal, loose" in capsys.readouterr().err


def test_a_stricter_threshold_can_exclude_a_pair(
    pics: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["scan", str(pics), "--json", "--similar", "--threshold", "0"]) == 0
    data = json.loads(capsys.readouterr().out)
    # threshold 0 still allows grey-zone pairs that pass SSIM, but never loosens anything
    assert len(data["similar_groups"]) <= 1


def test_without_the_extra_similar_exits_2_with_install_hint(
    pics: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "similar_available", lambda: False)
    assert main(["scan", str(pics), "--similar"]) == 2
    assert "dedupe[similar]" in capsys.readouterr().err


def test_without_the_extra_a_plain_scan_still_works(
    pics: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "similar_available", lambda: False)
    assert main(["scan", str(pics), "--json"]) == 0
