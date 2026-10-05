"""Partial and full file hashing, run in a thread pool with progress and cancellation."""

from __future__ import annotations

import os
import struct
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import blake3
import xxhash

from dedupe.core.models import (
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

    def get_many(self, entries: Sequence[FileEntry]) -> dict[Path, CachedHashes]: ...
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


BATCH_FILES = 64
BATCH_BYTES = 8 * 1024 * 1024


class HashService:
    """Hashes batches of files in a thread pool. Files that cannot be read are recorded in
    ``failed`` and omitted from the result. Cached hashes are looked up in bulk first, so a
    warm re-scan never touches the pool or the disk."""

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
        cached = self._lookup(entries, lambda h: h.partial)
        return self._run(
            Stage.PARTIAL,
            entries,
            cached,
            lambda e: min(e.size, 2 * PARTIAL_BYTES),
            self._partial_one,
        )

    def full(self, entries: Iterable[FileEntry]) -> dict[Path, str]:
        entries = list(entries)
        cached = self._lookup(entries, lambda h: h.full)
        return self._run(Stage.FULL, entries, cached, lambda e: e.size, self._full_one)

    # -- workers -------------------------------------------------------------------------

    def _lookup(
        self, entries: list[FileEntry], pick: Callable[[CachedHashes], str | None]
    ) -> dict[Path, str]:
        if self.store is None:
            return {}
        found = self.store.get_many(entries)
        return {p: v for p, h in found.items() if (v := pick(h))}

    def _partial_one(self, e: FileEntry, advance: Callable[[int, str], None]) -> str:
        value = partial_hash(e.path, e.size, self.cancel)
        n = min(e.size, 2 * PARTIAL_BYTES)
        with self._lock:
            self.bytes_read += n
        advance(n, str(e.path))
        if self.store:
            self.store.put_partial(e, value)
        return value

    def _full_one(self, e: FileEntry, advance: Callable[[int, str], None]) -> str:
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
        cached: dict[Path, str],
        weight: Callable[[FileEntry], int],
        work: Callable[[FileEntry, Callable[[int, str], None]], str],
    ) -> dict[Path, str]:
        total = sum(weight(e) for e in entries)
        done = sum(weight(e) for e in entries if e.path in cached)
        out: dict[Path, str] = dict(cached)
        todo = [e for e in entries if e.path not in cached]

        def advance(n: int, current: str) -> None:
            nonlocal done
            with self._lock:
                done += n
                snapshot = done
            if self.progress is not None:
                self.progress(Progress(stage, snapshot, total, current))

        def run_batch(batch: list[FileEntry]) -> list[tuple[FileEntry, str | None, OSError | None]]:
            results: list[tuple[FileEntry, str | None, OSError | None]] = []
            for e in batch:
                self.cancel.raise_if_cancelled()
                try:
                    results.append((e, work(e, advance), None))
                except OSError as err:
                    results.append((e, None, err))
            return results

        if self.progress is not None:
            self.progress(Progress(stage, done, total, ""))
        batches = _batches(todo)
        in_flight: set[Future[list[tuple[FileEntry, str | None, OSError | None]]]] = set()
        pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="hash")
        try:
            # Bounded submission keeps cancellation latency low on huge inputs.
            def top_up() -> None:
                while len(in_flight) < self.workers * 2:
                    batch = next(batches, None)
                    if batch is None:
                        return
                    in_flight.add(pool.submit(run_batch, batch))

            top_up()
            while in_flight:
                finished, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for fut in finished:
                    in_flight.discard(fut)
                    for e, value, err in fut.result():  # re-raises CancelledError
                        if err is not None:
                            self.failed.append(SkippedEntry(str(e.path), _reason(err)))
                        elif value is not None:
                            out[e.path] = value
                self.cancel.raise_if_cancelled()
                top_up()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        return out


def _batches(entries: list[FileEntry]) -> Iterator[list[FileEntry]]:
    """Group small files together so thread hand-offs don't dominate; big files go alone."""
    batch: list[FileEntry] = []
    size = 0
    for e in entries:
        batch.append(e)
        size += e.size
        if len(batch) >= BATCH_FILES or size >= BATCH_BYTES:
            yield batch
            batch, size = [], 0
    if batch:
        yield batch


def _reason(e: OSError) -> str:
    if isinstance(e, PermissionError):
        return "permission denied"
    if isinstance(e, FileNotFoundError):
        return "vanished"
    return e.strerror or type(e).__name__
