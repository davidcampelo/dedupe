"""The single scan entry point used by the CLI, the GUI worker and the tests."""

from __future__ import annotations

from pathlib import Path

from dedupe.core.cache import HashCache
from dedupe.core.grouper import group_duplicates
from dedupe.core.hasher import HashService, HashStore
from dedupe.core.models import (
    CancelledError,
    CancelToken,
    DuplicateGroup,
    FileEntry,
    ProgressCallback,
    ScanOptions,
    ScanResult,
    SimilarGroup,
    SkippedEntry,
)
from dedupe.core.perceptual import similar_available
from dedupe.core.recommender import recommend_all
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
        groups = recommend_all(outcome.groups, options.protected_folders, root)
        similar, similar_skipped, notes = _similar(
            entries, groups, hashes, options, cancel, progress, root
        )
    except CancelledError:
        return ScanResult(root, files_scanned=scanned, skipped=tuple(skipped), cancelled=True)
    warnings = notes
    if isinstance(store, HashCache):
        store.flush()
        warnings += tuple(store.warnings)
    result = ScanResult(
        root=root,
        warnings=warnings,
        groups=groups,
        empty_files=outcome.empty_files,
        hardlink_sets=outcome.hardlink_sets,
        skipped=tuple(skipped) + outcome.skipped + similar_skipped,
        files_scanned=scanned,
        similar_groups=similar,
    )
    _ = result.reclaimable  # computed here (worker thread) so reading it later is O(1)
    _ = result.similar_reclaimable
    return result


def _similar(
    entries: list[FileEntry],
    groups: tuple[DuplicateGroup, ...],
    hashes: HashService,
    options: ScanOptions,
    cancel: CancelToken,
    progress: ProgressCallback | None,
    root: Path,
) -> tuple[tuple[SimilarGroup, ...], tuple[SkippedEntry, ...], tuple[str, ...]]:
    """The optional similar-images pass: (groups, skipped images, warnings)."""
    if not options.similar_images:
        return (), (), ()
    if not similar_available():
        return (), (), ("similar images skipped: install dedupe[similar]",)
    from dedupe.core.similar import find_similar  # needs numpy and imagehash

    out = find_similar(
        entries,
        groups,
        hashes,
        options.similarity_threshold,
        cancel,
        progress,
        options.protected_folders,
        root,
    )
    return out.groups, out.skipped, ()
