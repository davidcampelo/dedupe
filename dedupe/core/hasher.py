"""Partial and full file hashing, run in a thread pool with progress and cancellation."""

from __future__ import annotations

import os
import struct
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import blake3
import xxhash

from dedupe.core.models import (
    CancelledError,
    CancelToken,
    FileEntry,
    Progress,
    ProgressCallback,
    SkippedEntry,
    Stage,
)

PARTIAL_BYTES = 64 * 1024
CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class CachedHashes:
    partial: str | None = None
    full: str | None = None


class HashStore(Protocol):
    """Persistent hash cache (see core/cache.py). Keyed on the entry's stat identity."""

    def get(self, entry: FileEntry) -> CachedHashes | None: ...
    def put_partial(self, entry: FileEntry, value: str) -> None: ...
    def put_full(self, entry: FileEntry, value: str) -> None: ...


def partial_hash(path: Path, size: int, cancel: CancelToken | None = None) -> str:
    """xxh3_128 of the size plus the first and last 64 KiB."""
    h = xxhash.xxh3_128()
    h.update(struct.pack("<Q", size))
    with open(path, "rb") as f:
        h.update(f.read(PARTIAL_BYTES))
        if size > PARTIAL_BYTES:
            f.seek(max(PARTIAL_BYTES, size - PARTIAL_BYTES))
            h.update(f.read(PARTIAL_BYTES))
    if cancel is not None:
        cancel.raise_if_cancelled()
    return h.hexdigest()


def full_hash(
    path: Path,
    cancel: CancelToken | None = None,
    on_bytes: Callable[[int], None] | None = None,
) -> str:
    """BLAKE3 of the whole content, read in 1 MiB chunks; cancel is checked per chunk."""
    h = blake3.blake3()
    with open(path, "rb", buffering=0) as f:
        while chunk := f.read(CHUNK):
            if cancel is not None:
                cancel.raise_if_cancelled()
            h.update(chunk)
            if on_bytes is not None:
                on_bytes(len(chunk))
    return h.hexdigest()


def default_workers() -> int:
    return min(4, os.cpu_count() or 1)


class HashService:
    """Hashes batches of files in a thread pool. Files that cannot be read are recorded in
    ``failed`` and omitted from the result."""

    def __init__(
        self,
        cancel: CancelToken,
        progress: ProgressCallback | None = None,
        workers: int = 0,
        store: HashStore | None = None,
    ) -> None:
        self.cancel = cancel
        self.progress = progress
        self.workers = workers or default_workers()
        self.store = store
        self.failed: list[SkippedEntry] = []
        self.bytes_read = 0  # bytes actually hashed from disk (cache hits read nothing)
        self._lock = threading.Lock()

    def partial(self, entries: Iterable[FileEntry]) -> dict[Path, str]:
        entries = list(entries)
        total = sum(min(e.size, 2 * PARTIAL_BYTES) for e in entries)
        return self._run(Stage.PARTIAL, entries, total, self._partial_one)

    def full(self, entries: Iterable[FileEntry]) -> dict[Path, str]:
        entries = list(entries)
        total = sum(e.size for e in entries)
        return self._run(Stage.FULL, entries, total, self._full_one)

    # -- workers -------------------------------------------------------------------------

    def _partial_one(self, e: FileEntry, advance: Callable[[int, str], None]) -> str:
        if self.store and (hit := self.store.get(e)) and hit.partial:
            advance(min(e.size, 2 * PARTIAL_BYTES), str(e.path))
            return hit.partial
        value = partial_hash(e.path, e.size, self.cancel)
        n = min(e.size, 2 * PARTIAL_BYTES)
        with self._lock:
            self.bytes_read += n
        advance(n, str(e.path))
        if self.store:
            self.store.put_partial(e, value)
        return value

    def _full_one(self, e: FileEntry, advance: Callable[[int, str], None]) -> str:
        if self.store and (hit := self.store.get(e)) and hit.full:
            advance(e.size, str(e.path))
            return hit.full

        def on_bytes(n: int) -> None:
            with self._lock:
                self.bytes_read += n
            advance(n, str(e.path))

        value = full_hash(e.path, self.cancel, on_bytes)
        if self.store:
            self.store.put_full(e, value)
        return value

    def _run(
        self,
        stage: Stage,
        entries: list[FileEntry],
        total: int,
        work: Callable[[FileEntry, Callable[[int, str], None]], str],
    ) -> dict[Path, str]:
        done = 0
        out: dict[Path, str] = {}

        def advance(n: int, current: str) -> None:
            nonlocal done
            with self._lock:
                done += n
                snapshot = done
            if self.progress is not None:
                self.progress(Progress(stage, snapshot, total, current))

        if self.progress is not None:
            self.progress(Progress(stage, 0, total, ""))
        pending = iter(entries)
        in_flight: dict[Future[str], FileEntry] = {}
        pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="hash")
        try:
            # Bounded submission keeps cancellation latency low on huge batches.
            def top_up() -> None:
                while len(in_flight) < self.workers * 2:
                    e = next(pending, None)
                    if e is None:
                        return
                    in_flight[pool.submit(work, e, advance)] = e

            top_up()
            while in_flight:
                finished, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for fut in finished:
                    e = in_flight.pop(fut)
                    try:
                        out[e.path] = fut.result()
                    except CancelledError:
                        raise
                    except OSError as err:
                        self.failed.append(SkippedEntry(str(e.path), _reason(err)))
                self.cancel.raise_if_cancelled()
                top_up()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        return out


def _reason(e: OSError) -> str:
    if isinstance(e, PermissionError):
        return "permission denied"
    if isinstance(e, FileNotFoundError):
        return "vanished"
    return e.strerror or type(e).__name__
