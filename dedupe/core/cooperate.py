"""Let other threads (a GUI event loop) take the GIL during long pure-Python loops."""

from __future__ import annotations

import time


def cooperate(n: int, every: int = 256) -> None:
    if n % every == 0:
        time.sleep(0.0001)  # a real (tiny) sleep: sleep(0) can be re-won by this thread
