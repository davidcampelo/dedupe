"""GUI-thread stall detection.

A 10 ms timer on the GUI thread records the gap between ticks; a gap far above 10 ms means
the event loop was blocked. Used by the ``ui_watchdog`` test fixture and, with
``DEDUPE_STALL_LOG=1``, by the running app (which also logs a stack sample of the GUI thread
while it is stuck).
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from collections.abc import Callable
from types import TracebackType

from PySide6.QtCore import QObject, QTimer

THRESHOLD = 0.1  # seconds
INTERVAL_MS = 10

StallCallback = Callable[[float, str], None]


class StallMonitor(QObject):
    def __init__(
        self,
        threshold: float = THRESHOLD,
        on_stall: StallCallback | None = None,
        sample_stack: bool = False,
    ) -> None:
        super().__init__()
        self.threshold = threshold
        self.on_stall = on_stall
        self.max_gap = 0.0
        self._last = time.monotonic()
        self._gui_thread = threading.get_ident()
        self._timer = QTimer(self)
        self._timer.setInterval(INTERVAL_MS)
        self._timer.timeout.connect(self._tick)
        self._sampler: threading.Thread | None = None
        self._sample_stack = sample_stack
        self._running = False

    def start(self) -> None:
        self.reset()
        self._running = True
        self._timer.start()
        if self._sample_stack and self._sampler is None:
            self._sampler = threading.Thread(target=self._sample, name="stall-sampler", daemon=True)
            self._sampler.start()

    def stop(self) -> None:
        self._running = False
        self._timer.stop()

    def reset(self) -> None:
        self.max_gap = 0.0
        self._last = time.monotonic()

    def current_max_gap(self) -> float:
        """Longest gap so far, including the time since the last tick."""
        return max(self.max_gap, time.monotonic() - self._last)

    def _tick(self) -> None:
        now = time.monotonic()
        self.max_gap = max(self.max_gap, now - self._last)
        self._last = now

    def _sample(self) -> None:
        reported = 0.0  # time of the last report; repeated every 0.25 s while a stall lasts
        while self._running:
            time.sleep(0.02)
            last = self._last
            now = time.monotonic()
            if now - last > self.threshold and now - reported > 0.25:
                reported = now
                frame = sys._current_frames().get(self._gui_thread)
                stack = "".join(traceback.format_stack(frame)) if frame else "(no frame)"
                if self.on_stall is not None:
                    self.on_stall(time.monotonic() - last, stack)

    def __enter__(self) -> StallMonitor:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()


def log_stall(gap: float, stack: str) -> None:
    print(f"[dedupe] GUI thread stalled for >{gap * 1000:.0f} ms:\n{stack}", file=sys.stderr)
