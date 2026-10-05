"""100k-file end-to-end runs under the GUI watchdog (checkpoint gates). Marked slow."""

from __future__ import annotations

import random
import time
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from dedupe.core.models import DeleteMode
from dedupe.core.settings import Settings
from dedupe.gui.delete_dialog import DeleteChoice
from dedupe.gui.main_window import MainWindow
from tests.gui.conftest import UiWatchdog

pytestmark = pytest.mark.slow

N_FILES = 100_000


@pytest.fixture(scope="module")
def big_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("big") / "tree"
    rng = random.Random(7)
    blobs = [rng.randbytes(64 + i % 900) for i in range(N_FILES // 2)]
    for i in range(N_FILES):
        d = root / f"d{i % 400:03d}" / f"s{i % 5}"
        if i < 2000:
            d.mkdir(parents=True, exist_ok=True)
        (d / f"f{i}.bin").write_bytes(blobs[i // 2] if i % 2 == 0 else blobs[(i - 1) // 2])
    return root


def make_window(qtbot: QtBot) -> MainWindow:
    w = MainWindow(Settings(exclude=(), use_cache=False))
    qtbot.addWidget(w)
    w.show()
    w.trash_backend = lambda path: (_ for _ in ()).throw(AssertionError("must be a dry run"))
    w.show_summary = lambda text: None  # type: ignore[method-assign]
    return w


def test_scan_load_and_dry_run_delete_under_the_watchdog(
    qtbot: QtBot, big_tree: Path, ui_watchdog: UiWatchdog, tmp_path: Path
) -> None:
    w = make_window(qtbot)
    w.log_path = tmp_path / "actions.log"
    summaries: list[str] = []
    w.show_summary = summaries.append  # type: ignore[method-assign]
    w.confirm_delete = lambda plan: DeleteChoice(DeleteMode.TRASH, True)  # type: ignore[method-assign]
    model = w.duplicates_tab.model
    w.set_folder(big_tree)
    with ui_watchdog.watch():
        with qtbot.waitSignal(w.scan_finished, timeout=300000):
            w.start_scan()
        qtbot.waitUntil(lambda: not model.loading, timeout=120000)
        assert model.group_count == N_FILES // 2
        assert model.selected_count == N_FILES // 2
        w.request_delete()
        qtbot.waitUntil(lambda: bool(summaries), timeout=120000)
    assert summaries[0].startswith("Dry run")
    assert f"{N_FILES // 2} files would be moved to Trash" in summaries[0]
    assert not (tmp_path / "actions.log").exists()  # a dry run writes nothing


def test_cancel_responds_within_a_second_on_100k_files(
    qtbot: QtBot, big_tree: Path, ui_watchdog: UiWatchdog
) -> None:
    w = make_window(qtbot)
    w.set_folder(big_tree)
    with ui_watchdog.watch():
        w.start_scan()
        qtbot.waitUntil(
            lambda: "full hash" in w.stage_label.text() or "partial" in w.stage_label.text(),
            timeout=120000,
        )
        start = time.monotonic()
        with qtbot.waitSignal(w.scan_finished, timeout=10000) as blocker:
            w.cancel_button.click()
        elapsed = time.monotonic() - start
    assert blocker.args[0].cancelled
    assert elapsed < 1.0, f"cancel took {elapsed:.2f}s"
