"""Iterative directory walk that yields FileEntry records."""

from __future__ import annotations

import fnmatch
import os
import stat
from pathlib import Path

from dedupe.core.models import (
    CancelToken,
    FileEntry,
    Progress,
    ProgressCallback,
    ScanOptions,
    SkippedEntry,
    Stage,
)

PROGRESS_EVERY = 200


class Excluder:
    """Glob/folder exclusions. Patterns starting with '/' match absolute path prefixes;
    others are fnmatch'd against the entry name (or the full path if they contain '/')."""

    def __init__(self, patterns: tuple[str, ...]) -> None:
        self._absolute = tuple(p.rstrip("/") or "/" for p in patterns if p.startswith("/"))
        self._names = tuple(p for p in patterns if not p.startswith("/") and "/" not in p)
        self._paths = tuple(p for p in patterns if not p.startswith("/") and "/" in p)

    def matches(self, path: str, name: str) -> bool:
        for a in self._absolute:
            if path == a or path.startswith(a.rstrip("/") + "/"):
                return True
        if any(fnmatch.fnmatch(name, p) for p in self._names):
            return True
        return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(path, "*/" + p) for p in self._paths)


def scan_files(
    root: Path,
    options: ScanOptions,
    cancel: CancelToken,
    progress: ProgressCallback | None = None,
    skipped: list[SkippedEntry] | None = None,
) -> tuple[list[FileEntry], list[SkippedEntry]]:
    """Walk ``root`` and return (entries, skipped). Stops early (returning what it has)
    when ``cancel`` fires."""
    skipped = skipped if skipped is not None else []
    entries: list[FileEntry] = []
    excluder = Excluder(options.exclude)
    root = Path(os.path.abspath(root))

    try:
        root_stat = os.stat(root)
    except OSError as e:
        skipped.append(SkippedEntry(str(root), _reason(e)))
        return entries, skipped
    if not stat.S_ISDIR(root_stat.st_mode):
        skipped.append(SkippedEntry(str(root), "not a directory"))
        return entries, skipped

    root_dev = root_stat.st_dev
    visited: set[tuple[int, int]] = {(root_stat.st_dev, root_stat.st_ino)}
    stack: list[str] = [str(root)]
    seen = 0

    while stack:
        if cancel.cancelled:
            break
        directory = stack.pop()
        try:
            it = os.scandir(directory)
        except OSError as e:
            skipped.append(SkippedEntry(directory, _reason(e)))
            continue
        with it:
            while True:
                if cancel.cancelled:
                    break
                try:
                    de = next(it)
                except StopIteration:
                    break
                except OSError as e:
                    skipped.append(SkippedEntry(directory, _reason(e)))
                    break
                seen += 1
                if progress is not None and seen % PROGRESS_EVERY == 0:
                    progress(Progress(Stage.WALK, seen, 0, de.path))
                _visit(de, options, excluder, root_dev, visited, stack, entries, skipped)

    if progress is not None:
        progress(Progress(Stage.WALK, seen, seen, ""))
    return entries, skipped


def _visit(
    de: os.DirEntry[str],
    options: ScanOptions,
    excluder: Excluder,
    root_dev: int,
    visited: set[tuple[int, int]],
    stack: list[str],
    entries: list[FileEntry],
    skipped: list[SkippedEntry],
) -> None:
    name, path = de.name, de.path
    if not options.include_hidden and name.startswith("."):
        return
    if excluder.matches(path, name):
        return
    try:
        is_link = de.is_symlink()
        if is_link and not options.follow_symlinks:
            return
        st = de.stat(follow_symlinks=options.follow_symlinks)
    except OSError as e:
        skipped.append(SkippedEntry(path, _reason(e)))
        return

    if stat.S_ISDIR(st.st_mode):
        if not options.cross_filesystems and st.st_dev != root_dev:
            return
        key = (st.st_dev, st.st_ino)
        if key in visited:  # symlink loop or a directory reached twice
            return
        visited.add(key)
        stack.append(path)
    elif stat.S_ISREG(st.st_mode):
        if st.st_size < options.min_size:
            return
        if not options.cross_filesystems and st.st_dev != root_dev:
            return
        if not os.access(path, os.R_OK):
            skipped.append(SkippedEntry(path, "permission denied"))
            return
        entries.append(
            FileEntry(Path(path), st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev, st.st_mode)
        )


def _reason(e: OSError) -> str:
    if isinstance(e, PermissionError):
        return "permission denied"
    if isinstance(e, FileNotFoundError):
        return "vanished"
    return e.strerror or type(e).__name__
