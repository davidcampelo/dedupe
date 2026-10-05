"""Decide which copy in each duplicate group to keep (spec section 5).

Rules, applied in order; the first rule that narrows the candidates decides:
  1. a file in a protected/preferred folder (protected files are never suggested for deletion)
  2. not in a disposable-looking folder (Downloads, tmp, Trash, cache, backup, copy, old)
  3. a name that doesn't look like a copy ("Copy of", "(1)", "_1", "- Copy", ".bak")
  4. the oldest modification time
  5. the shorter path
  6. alphabetical order (determinism)
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path, PurePath

from dedupe.core.models import DuplicateGroup, FileEntry, Recommendation, Verdict

DISPOSABLE_WORDS = frozenset({"downloads", "tmp", "trash", "cache", "backup", "copy", "old"})
COPY_NAME = re.compile(
    r"(\bcopy\b|\(\d+\)|_\d+$|\.bak$)",
    re.IGNORECASE,
)


def is_under(path: PurePath, folder: str | PurePath) -> bool:
    p = os.path.abspath(path)
    f = os.path.abspath(folder).rstrip(os.sep)
    return p == f or p.startswith(f + os.sep)


def is_protected(path: PurePath, protected_folders: Iterable[str]) -> bool:
    return any(is_under(path, f) for f in protected_folders)


def in_disposable_folder(path: Path, root: Path | None = None) -> bool:
    parent = path.parent
    if root is not None and is_under(parent, root):
        parts = parent.relative_to(os.path.abspath(root)).parts
    else:
        parts = parent.parts
    return any(
        token in DISPOSABLE_WORDS
        for part in parts
        for token in re.split(r"[^a-z0-9]+", part.lower())
    )


def looks_like_copy(name: str) -> bool:
    stem = name[: -len(Path(name).suffix)] if Path(name).suffix and name.count(".") else name
    return bool(COPY_NAME.search(name) or COPY_NAME.search(stem))


def _depth(e: FileEntry) -> tuple[int, int]:
    return (len(e.path.parts), len(str(e.path)))


def recommend_group(
    group: DuplicateGroup,
    protected_folders: Iterable[str] = (),
    root: Path | None = None,
) -> tuple[Recommendation, ...]:
    """Verdicts for one group: every protected file is Keep; otherwise exactly one Keep."""
    protected_folders = tuple(protected_folders)
    files = list(group.files)
    protected = [f for f in files if is_protected(f.path, protected_folders)]

    rules: list[tuple[str, Callable[[FileEntry], object]]] = [
        ("in a preferred folder", lambda e: not is_protected(e.path, protected_folders)),
        ("not in a disposable-looking folder", lambda e: in_disposable_folder(e.path, root)),
        ("name doesn't look like a copy", lambda e: looks_like_copy(e.path.name)),
        ("oldest modification time", lambda e: e.mtime_ns),
        ("shorter path", _depth),
    ]
    candidates = files
    reason = "alphabetical tie-break"
    for label, key in rules:
        best = min(key(e) for e in candidates)  # type: ignore[type-var]
        narrowed = [e for e in candidates if key(e) == best]
        if len(narrowed) < len(candidates):
            candidates, reason = narrowed, label
            break
    winner = min(candidates, key=lambda e: str(e.path))
    if protected:
        keep = {f.path for f in protected}
        return _verdicts(files, keep, "in a preferred folder", winner.path)
    return _verdicts(files, {winner.path}, reason, winner.path)


def _verdicts(
    files: list[FileEntry], keep: set[Path], reason: str, reference: Path
) -> tuple[Recommendation, ...]:
    out = []
    for f in files:
        if f.path in keep:
            out.append(Recommendation(f.path, Verdict.KEEP, f"Kept: {reason}"))
        else:
            out.append(Recommendation(f.path, Verdict.DELETE, f"Duplicate of {reference}"))
    return tuple(out)


def recommend_all(
    groups: Iterable[DuplicateGroup],
    protected_folders: Iterable[str] = (),
    root: Path | None = None,
) -> tuple[DuplicateGroup, ...]:
    protected_folders = tuple(protected_folders)
    return tuple(
        replace(g, recommendations=recommend_group(g, protected_folders, root)) for g in groups
    )


# -- bulk helpers ---------------------------------------------------------------------------


def _rebuild(
    group: DuplicateGroup,
    preferred: list[FileEntry],
    protected_folders: Iterable[str],
    reason: str,
    root: Path | None,
) -> tuple[Recommendation, ...]:
    """Keep the best of ``preferred`` (by the default ranking) plus every protected file."""
    if not preferred:
        return group.recommendations or recommend_group(group, protected_folders, root)
    sub = replace(group, files=tuple(preferred))
    best = next(r.path for r in recommend_group(sub, (), root) if r.verdict is Verdict.KEEP)
    keep = {best} | {f.path for f in group.files if is_protected(f.path, protected_folders)}
    return _verdicts(list(group.files), keep, reason, best)


def keep_newest(
    group: DuplicateGroup, protected_folders: Iterable[str] = (), root: Path | None = None
) -> tuple[Recommendation, ...]:
    newest = max(f.mtime_ns for f in group.files)
    chosen = [f for f in group.files if f.mtime_ns == newest]
    return _rebuild(group, chosen, protected_folders, "newest modification time", root)


def keep_in_folder(
    group: DuplicateGroup,
    folder: str | PurePath,
    protected_folders: Iterable[str] = (),
    root: Path | None = None,
) -> tuple[Recommendation, ...]:
    chosen = [f for f in group.files if is_under(f.path, folder)]
    return _rebuild(group, chosen, protected_folders, f"in folder {folder}", root)


def suggested_deletions(groups: Iterable[DuplicateGroup]) -> set[Path]:
    """'Select all suggested': every path currently marked Delete."""
    return {r.path for g in groups for r in g.recommendations if r.verdict is Verdict.DELETE}
