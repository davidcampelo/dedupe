from __future__ import annotations

import time

import pytest
from pytestqt.qtbot import QtBot

from tests.gui.conftest import UiWatchdog


def test_watchdog_fails_when_the_gui_thread_blocks(qtbot: QtBot, ui_watchdog: UiWatchdog) -> None:
    with pytest.raises(AssertionError, match="GUI thread stalled"), ui_watchdog.watch():
        qtbot.wait(50)
        time.sleep(0.25)  # blocks the event loop
        qtbot.wait(50)


def test_watchdog_passes_when_the_loop_stays_responsive(
    qtbot: QtBot, ui_watchdog: UiWatchdog
) -> None:
    with ui_watchdog.watch():
        for _ in range(20):
            time.sleep(0.01)
            qtbot.wait(5)
    assert ui_watchdog.worst < 0.1
