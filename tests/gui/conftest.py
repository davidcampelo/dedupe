from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from PySide6.QtWidgets import QApplication

from dedupe.gui.gcutil import tune_runtime
from dedupe.gui.stall import THRESHOLD, StallMonitor

tune_runtime()  # same GIL tuning as the real app


# Shared 2-core CI runners (software GL, noisy neighbours) show 250-350 ms scheduler hiccups
# that are not event-loop blocking; keep the strict 100 ms limit for local runs.
CI_THRESHOLD = 0.5


def default_threshold() -> float:
    return CI_THRESHOLD if os.environ.get("CI") else THRESHOLD


class UiWatchdog:
    """``with ui_watchdog.watch():`` fails the test if the GUI thread is blocked for more than
    100 ms (500 ms under CI; no heartbeat tick) at any point inside the block. Pump events with
    ``qtbot.waitUntil(..., timeout=...)`` / ``waitSignal`` so the heartbeat can run."""

    def __init__(self) -> None:
        self.worst = 0.0

    @contextmanager
    def watch(self, threshold: float | None = None) -> Iterator[StallMonitor]:
        threshold = default_threshold() if threshold is None else threshold
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
