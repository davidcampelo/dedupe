"""The single scan entry point used by the CLI, the GUI worker and the tests."""

from __future__ import annotations

from pathlib import Path

from dedupe.core.grouper import group_duplicates
from dedupe.core.hasher import HashService, HashStore
from dedupe.core.models import (
    CancelledError,
    CancelToken,
    ProgressCallback,
    ScanOptions,
    ScanResult,
    SkippedEntry,
)
from dedupe.core.scanner import scan_files


def run_scan(
    root: Path,
    options: ScanOptions | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    store: HashStore | None = None,
) -> ScanResult:
    """Walk, group and return a ScanResult. If cancelled, returns what is known so far with
    ``cancelled=True`` (and no groups)."""
    options = options or ScanOptions()
    cancel = cancel or CancelToken()
    root = Path(root)
    skipped: list[SkippedEntry] = []
    scanned = 0
    try:
        entries, skipped = scan_files(root, options, cancel, progress, skipped)
        scanned = len(entries)
        cancel.raise_if_cancelled()
        hashes = HashService(cancel, progress, options.workers, store)
        outcome = group_duplicates(entries, hashes, cancel, progress, options.paranoid)
    except CancelledError:
        return ScanResult(root, files_scanned=scanned, skipped=tuple(skipped), cancelled=True)
    return ScanResult(
        root=root,
        groups=outcome.groups,
        empty_files=outcome.empty_files,
        hardlink_sets=outcome.hardlink_sets,
        skipped=tuple(skipped) + outcome.skipped,
        files_scanned=scanned,
    )
