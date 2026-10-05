from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from dedupe.cli import human, main

MakeTree = Callable[..., Path]


def test_scan_json_schema(make_tree: MakeTree, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_tree({"a.txt": "dup", "b/c.txt": "dup", "e": b"", "u": "unique"})
    os.link(root / "a.txt", root / "a-link")
    assert main(["scan", str(root), "--json", "--min-size", "0"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {
        "root", "files_scanned", "reclaimable", "cancelled", "groups",
        "empty_files", "hardlink_sets", "skipped",
    }  # fmt: skip
    (g,) = data["groups"]
    assert set(g) == {"hash", "size", "reclaimable", "files", "hardlinked"}
    assert g["size"] == 3 and g["reclaimable"] == 3
    assert {Path(f["path"]).name for f in g["files"]} == {"a-link", "c.txt"} or len(g["files"]) == 2
    assert len(g["hardlinked"]) == 1
    assert [Path(p).name for p in data["empty_files"]] == ["e"]
    assert data["reclaimable"] == 3


def test_scan_table(make_tree: MakeTree, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_tree({"a": "dup", "b": "dup"})
    assert main(["scan", str(root)]) == 0
    out = capsys.readouterr().out
    assert "1 duplicate groups" in out and str(root / "a") in out


def test_scan_exclude_and_options(make_tree: MakeTree, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_tree({"a": "dup", "b.log": "dup", ".h": "dup"})
    args = ["scan", str(root), "--json", "--exclude", "*.log"]
    assert main(args) == 0
    assert len(json.loads(capsys.readouterr().out)["groups"][0]["files"]) == 2
    assert main([*args, "--no-hidden", "--paranoid", "--follow-symlinks"]) == 0
    assert json.loads(capsys.readouterr().out)["groups"] == []


def test_bad_path_is_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scan", str(tmp_path / "missing")]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage" in capsys.readouterr().out


def test_human() -> None:
    assert human(0) == "0 B" and human(2048) == "2.0 KiB" and human(5 * 1024**4) == "5.0 TiB"


def test_sigint_exits_cleanly(make_tree: MakeTree) -> None:
    root = make_tree({f"f{i}": b"x" * 2_000_000 for i in range(40)})
    proc = subprocess.Popen(
        [sys.executable, "-m", "dedupe.cli", "scan", str(root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.4)
    proc.send_signal(signal.SIGINT)
    _, err = proc.communicate(timeout=10)
    assert proc.returncode in (130, 0)  # 0 if the scan finished first
    assert "Traceback" not in err


def test_cache_used_and_cleared(make_tree: MakeTree, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_tree({"a": "dup", "b": "dup"})
    assert main(["scan", str(root), "--json"]) == 0
    capsys.readouterr()
    assert main(["cache", "clear"]) == 0
    assert "cleared 2 cached hashes" in capsys.readouterr().out
    assert main(["scan", str(root), "--json", "--no-cache"]) == 0
    capsys.readouterr()
    assert main(["cache", "clear"]) == 0
    assert "cleared 0 cached hashes" in capsys.readouterr().out


def test_scan_honours_settings_file(
    make_tree: MakeTree, capsys: pytest.CaptureFixture[str]
) -> None:
    from dedupe.core.settings import Settings, save_settings

    root = make_tree({"a.log": "dup", "b.log": "dup", "c.txt": "dup", "d.txt": "dup"})
    save_settings(Settings(exclude=("*.log",)))
    assert main(["scan", str(root), "--json"]) == 0
    files = json.loads(capsys.readouterr().out)["groups"][0]["files"]
    assert sorted(Path(f["path"]).name for f in files) == ["c.txt", "d.txt"]


def test_bad_settings_file_is_an_error(
    make_tree: MakeTree, capsys: pytest.CaptureFixture[str]
) -> None:
    from dedupe.core import paths

    paths.settings_file().parent.mkdir(parents=True)
    paths.settings_file().write_text("min_size = true\n")
    assert main(["scan", str(make_tree({"a": "x"}))]) == 2
    assert "min_size" in capsys.readouterr().err
