"""Plain data types shared by the engine, the CLI and the GUI."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cached_property
from pathlib import Path

DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git",
    "node_modules",
    "__pycache__",
    ".cache",
    "/proc",
    "/sys",
    "/dev",
)


class Stage(StrEnum):
    WALK = "walk"
    SIZE = "size"
    PARTIAL = "partial hash"
    FULL = "full hash"
    COMPARE = "byte compare"
    RECOMMEND = "recommend"
    HIDDEN = "hidden scan"
    ACTION = "action"


class CancelledError(Exception):
    """Raised inside the engine when the CancelToken fires."""


class CancelToken:
    """Thread-safe cancellation flag checked by every long-running loop."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise CancelledError


@dataclass(frozen=True, slots=True)
class Progress:
    stage: Stage
    done: int = 0
    total: int = 0
    current_path: str = ""


ProgressCallback = Callable[[Progress], None]


@dataclass(frozen=True, slots=True)
class FileEntry:
    path: Path
    size: int
    mtime_ns: int
    inode: int
    device: int
    mode: int = 0

    @property
    def identity(self) -> tuple[int, int]:
        return (self.device, self.inode)


@dataclass(frozen=True, slots=True)
class SkippedEntry:
    path: str
    reason: str


@dataclass(frozen=True, slots=True)
class ScanOptions:
    min_size: int = 1
    include_hidden: bool = True
    follow_symlinks: bool = False
    cross_filesystems: bool = False
    exclude: tuple[str, ...] = DEFAULT_EXCLUDES
    paranoid: bool = False
    workers: int = 0  # 0 = min(4, cpu_count)
    use_cache: bool = True
    protected_folders: tuple[str, ...] = ()


class DeleteMode(StrEnum):
    TRASH = "trash"
    PERMANENT = "permanent"
    HARDLINK = "hardlink"


class Verdict(StrEnum):
    KEEP = "keep"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class Recommendation:
    path: Path
    verdict: Verdict
    reason: str


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    hash: str
    size: int
    files: tuple[FileEntry, ...]
    # Paths that are hard links of a file already in ``files`` (same device+inode).
    hardlinked: tuple[Path, ...] = ()
    recommendations: tuple[Recommendation, ...] = ()

    @property
    def reclaimable(self) -> int:
        return self.size * max(len(self.files) - 1, 0)


@dataclass(frozen=True)  # no slots: ``reclaimable`` is a cached_property
class ScanResult:
    root: Path
    groups: tuple[DuplicateGroup, ...] = ()
    empty_files: tuple[FileEntry, ...] = ()
    hardlink_sets: tuple[tuple[Path, ...], ...] = ()
    skipped: tuple[SkippedEntry, ...] = ()
    files_scanned: int = 0
    cancelled: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @cached_property
    def reclaimable(self) -> int:
        return sum(g.reclaimable for g in self.groups)
