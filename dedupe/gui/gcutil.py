"""Keep cyclic-GC pauses off the GUI thread.

A full (gen 2) collection scans every tracked object and takes ~100+ ms once a scan result
holds 100k files, freezing the event loop. We pause the collector while a scan runs and move
freshly built objects to the permanent generation (``gc.freeze``) so later passes skip them.
Frozen objects are still freed by reference counting; only *cyclic* garbage in the frozen set
would leak, so the duplicates model breaks its own cycles when it is replaced.
"""

from __future__ import annotations

import gc
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager

_lock = threading.Lock()
_paused = 0


@contextmanager
def gc_paused() -> Iterator[None]:
    global _paused
    with _lock:
        if _paused == 0 and gc.isenabled():
            gc.disable()
            resume = True
        else:
            resume = False
        _paused += 1
    try:
        yield
    finally:
        with _lock:
            _paused -= 1
            if _paused == 0 and (resume or not gc.isenabled()):
                gc.enable()


def tune_runtime() -> None:
    """Make worker threads hand the GIL back quickly. With the default 5 ms switch interval a
    CPU-bound worker could starve the GUI thread for 100+ ms at a time."""
    sys.setswitchinterval(0.0002)


def freeze() -> None:
    """Move everything alive into the permanent generation (O(1))."""
    gc.freeze()
