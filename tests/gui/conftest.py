from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from PySide6.QtWidgets import QApplication

from dedupe.gui.stall import THRESHOLD, StallMonitor


class UiWatchdog:
    """``with ui_watchdog.watch():`` fails the test if the GUI thread is blocked for more than
    100 ms (no heartbeat tick) at any point inside the block. Pump events with
    ``qtbot.waitUntil(..., timeout=...)`` / ``waitSignal`` so the heartbeat can run."""

    def __init__(self) -> None:
        self.worst = 0.0

    @contextmanager
    def watch(self, threshold: float = THRESHOLD) -> Iterator[StallMonitor]:
        monitor = StallMonitor(threshold)
        with monitor:
            try:
                yield monitor
            finally:
                gap = monitor.current_max_gap()
        self.worst = max(self.worst, gap)
        assert gap <= threshold, (
            f"GUI thread stalled for {gap * 1000:.0f} ms (limit {threshold * 1000:.0f} ms)"
        )


@pytest.fixture
def ui_watchdog(qapp: QApplication) -> UiWatchdog:
    return UiWatchdog()
