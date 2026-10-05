from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from dedupe.cli import main
from dedupe.core import hidden
from dedupe.core.hidden import classify, scan_hidden
from dedupe.core.models import CancelToken

MakeTree = Callable[..., Path]


@pytest.mark.parametrize(
    ("name", "is_dir", "expected"),
    [
        (".x", False, hidden.CAT_DOT_FILE),
        (".x", True, hidden.CAT_DOT_FOLDER),
        ("~x", False, hidden.CAT_TILDE),
        ("x~", False, hidden.CAT_EDITOR_BACKUP),
        ("~$x.docx", False, hidden.CAT_OFFICE_LOCK),
        (".x.swp", False, hidden.CAT_SWAP),
        ("a.swo", False, hidden.CAT_SWAP),
        (".DS_Store", False, hidden.CAT_OS_META),
        ("Thumbs.db", False, hidden.CAT_OS_META),
        ("desktop.ini", False, hidden.CAT_OS_META),
        ("a.tmp", False, hidden.CAT_TEMP),
        ("plain.txt", False, None),
        ("a.tmp", True, None),
    ],
)
def test_classify(name: str, is_dir: bool, expected: str | None) -> None:
    assert classify(name, is_dir) == expected


def test_temp_patterns_can_be_disabled() -> None:
    assert classify("a.tmp", False, temp_patterns=False) is None
    assert classify(".DS_Store", False, temp_patterns=False) == hidden.CAT_DOT_FILE
    assert classify("a.swp", False, temp_patterns=False) is None
    assert classify("x~", False, temp_patterns=False) == hidden.CAT_EDITOR_BACKUP


def by_name(root: Path, **kw: object) -> dict[str, hidden.HiddenItem]:
    return {i.path.name: i for i in scan_hidden(root, **kw).items}  # type: ignore[arg-type]


def test_scan_and_preselection(make_tree: MakeTree) -> None:
    root = make_tree(
        {
            "~$x.docx": "lock",
            "notes.txt~": "bak",
            "~tilde": "t",
            ".DS_Store": "m",
            "Thumbs.db": "m",
            "desktop.ini": "m",
            "a.tmp": "t",
            "keep.txt": "k",
            ".bashrc": "rc",
            ".mything": "x",
        }
    )
    items = by_name(root, open_files_fn=set)
    assert "keep.txt" not in items
    for name in ("~$x.docx", "notes.txt~", ".DS_Store", "Thumbs.db"):
        assert items[name].preselect, name
    for name in ("~tilde", "desktop.ini", "a.tmp", ".mything"):
        assert not items[name].preselect, name
    assert items[".bashrc"].protected and not items[".bashrc"].preselect


def test_dot_folder_reported_once_with_recursive_size(make_tree: MakeTree) -> None:
    root = make_tree(
        {".stuff/a": "12345", ".stuff/sub/b": "123", ".stuff/.inner": "1", "ok/f": "x"}
    )
    result = scan_hidden(root)
    assert [i.path.name for i in result.items] == [".stuff"]
    (item,) = result.items
    assert item.is_dir and item.size == 9 and item.category == hidden.CAT_DOT_FOLDER


def test_hidden_items_found_in_normal_subfolders(make_tree: MakeTree) -> None:
    root = make_tree({"a/b/~$lock": "x", "a/.hid": "y"})
    assert set(by_name(root)) == {"~$lock", ".hid"}


def test_protected_by_ancestor(make_tree: MakeTree) -> None:
    root = make_tree({".ssh/x": "k"})
    # Scanning *inside* a protected folder flags its hidden children as protected
    inner = make_tree({".ssh/.hidden~": "k"}) / ".ssh"
    items = by_name(inner)
    assert items[".hidden~"].protected and not items[".hidden~"].preselect
    assert by_name(root)[".ssh"].protected


def test_home_toplevel_dot_flag_and_no_preselect(isolated_home: Path, make_tree: MakeTree) -> None:
    (isolated_home / ".cache").mkdir()
    (isolated_home / ".cache" / "junk~").write_text("x")
    (isolated_home / "docs").mkdir()
    (isolated_home / "docs" / "n~").write_text("x")
    items = by_name(isolated_home, open_files_fn=set)
    assert items[".cache"].home_toplevel_dot
    inside = by_name(isolated_home / ".cache", open_files_fn=set)
    assert inside["junk~"].home_toplevel_dot and not inside["junk~"].preselect
    assert not items["n~"].home_toplevel_dot and items["n~"].preselect
    assert not hidden.is_home_toplevel_dot(Path("/elsewhere/.x"))


def test_swap_file_held_open_is_not_preselected(make_tree: MakeTree) -> None:
    root = make_tree({".held.swp": "s", ".free.swp": "s", "ignored.swo": "s"})
    code = "import sys,time; f=open(sys.argv[1]); print('ready', flush=True); time.sleep(30)"
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(root / ".held.swp")], stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout is not None
        proc.stdout.readline()
        items = by_name(root)
        assert not items[".held.swp"].preselect
        assert items[".free.swp"].preselect
        assert not items["ignored.swo"].preselect  # only *.swp is ever preselected
    finally:
        proc.kill()
        proc.wait()


def test_open_files_sees_own_handles(tmp_path: Path) -> None:
    p = tmp_path / "f"
    p.write_text("x")
    with open(p):
        assert str(p) in hidden.open_files()


def test_unreadable_dir_is_skipped(make_tree: MakeTree) -> None:
    import os

    root = make_tree({"locked/.x": "a", ".y": "b"})
    (root / "locked").chmod(0)
    try:
        result = scan_hidden(root)
    finally:
        (root / "locked").chmod(0o755)
    if os.geteuid() != 0:
        assert [Path(s.path).name for s in result.skipped] == ["locked"]
    assert ".y" in {i.path.name for i in result.items}


def test_bad_root_and_cancel(tmp_path: Path, make_tree: MakeTree) -> None:
    assert scan_hidden(tmp_path / "nope").skipped
    token = CancelToken()
    token.cancel()
    assert scan_hidden(make_tree({".a": "x"}), token).cancelled


def test_cli_hidden_json(make_tree: MakeTree, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_tree({"x~": "12", ".cfg": "3", "ok": "z"})
    assert main(["hidden", str(root), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {i["path"].rsplit("/", 1)[1] for i in data["items"]} == {"x~", ".cfg"}
    assert data["reclaimable_preselected"] == 2
    assert main(["hidden", str(root)]) == 0
    assert "2 items" in capsys.readouterr().out
    assert main(["hidden", str(root / "missing")]) == 2


def test_progress(make_tree: MakeTree) -> None:
    root = make_tree({f"f{i}": "x" for i in range(450)})
    seen: list[int] = []
    scan_hidden(root, progress=lambda p: seen.append(p.done))
    assert seen[-1] == 450
