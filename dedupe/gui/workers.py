"""Background jobs. The GUI thread never does I/O: every long operation is a Job on a
QThreadPool that owns a CancelToken and reports through queued signals."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from dedupe.core.cache import HashCache
from dedupe.core.models import CancelToken, Progress, ProgressCallback, ScanOptions
from dedupe.core.pipeline import run_scan
from dedupe.gui.gcutil import freeze, gc_paused

PROGRESS_INTERVAL = 0.05  # seconds: at most 20 progress signals per second

JobFn = Callable[[CancelToken, ProgressCallback], Any]


class JobSignals(QObject):
    progress = Signal(object)  # Progress
    finished = Signal(object)  # the job's result
    failed = Signal(str)


class Job(QRunnable):
    """Runs ``fn(cancel, progress)`` on a pool thread. Exactly one of ``finished`` or
    ``failed`` is emitted. Create it on the GUI thread so its signals deliver queued."""

    def __init__(self, fn: JobFn) -> None:
        super().__init__()
        self.setAutoDelete(False)  # Python owns it; the window keeps a reference
        self.fn = fn
        self.cancel_token = CancelToken()
        self.signals = JobSignals()
        self.done = False
        self._last_emit = 0.0

    def cancel(self) -> None:
        self.cancel_token.cancel()

    def run(self) -> None:
        try:
            result = self.fn(self.cancel_token, self._on_progress)
        except Exception as e:
            self.done = True
            self.signals.failed.emit(f"{type(e).__name__}: {e}")
        else:
            self.done = True
            self.signals.finished.emit(result)

    def _on_progress(self, p: Progress) -> None:
        now = time.monotonic()
        if now - self._last_emit >= PROGRESS_INTERVAL:
            self._last_emit = now
            self.signals.progress.emit(p)


class ScanJob(Job):
    def __init__(self, root: Path, options: ScanOptions) -> None:
        def work(cancel: CancelToken, progress: ProgressCallback) -> Any:
            cache = HashCache() if options.use_cache else None
            try:
                with gc_paused():
                    result = run_scan(root, options, progress, cancel, cache)
                    freeze()  # the result is big and long-lived: keep GC passes off it
                return result
            finally:
                if cache is not None:
                    cache.close()

        super().__init__(work)
        self.root = root


class JobRunner:
    """A dedicated pool plus bookkeeping so closing the window can cancel and wait."""

    def __init__(self, max_threads: int = 2) -> None:
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(max_threads)
        self._jobs: list[Job] = []

    def start(self, job: Job) -> Job:
        self._jobs = [j for j in self._jobs if not j.done]
        self._jobs.append(job)
        self.pool.start(job)
        return job

    def cancel_all(self) -> None:
        for job in self._jobs:
            job.cancel()

    def shutdown(self, timeout_ms: int = 2000) -> bool:
        """Cancel everything and wait (bounded). True if all jobs finished."""
        self.cancel_all()
        return self.pool.waitForDone(timeout_ms)
