"""Detect hidden and temporary files/folders (spec section 6)."""

from __future__ import annotations

import fnmatch
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dedupe.core.models import (
    CancelledError,
    CancelToken,
    Progress,
    ProgressCallback,
    SkippedEntry,
    Stage,
)

CAT_DOT_FILE = "Hidden file"
CAT_DOT_FOLDER = "Hidden folder"
CAT_TILDE = "Tilde file"
CAT_OFFICE_LOCK = "Office lock file"
CAT_EDITOR_BACKUP = "Editor backup"
CAT_SWAP = "Editor swap file"
CAT_OS_META = "OS metadata"
CAT_TEMP = "Temporary file"

# Essential configuration: shown, never preselected, extra confirmation to delete.
PROTECTED_NAMES = frozenset(
    {
        ".bashrc", ".bash_profile", ".bash_logout", ".bash_history", ".profile", ".zshrc",
        ".zprofile", ".zshenv", ".zlogin", ".zsh_history", ".ssh", ".config", ".gnupg", ".local",
        ".git", ".gitconfig", ".gitignore", ".gitattributes", ".gitmodules", ".vimrc", ".vim",
        ".npmrc", ".env", ".mozilla", ".thunderbird", ".docker", ".kube", ".aws", ".netrc",
        ".xinitrc", ".Xauthority", ".xprofile", ".inputrc", ".tmux.conf", ".pki", ".password-store",
        ".cargo", ".rustup", ".conda", ".pam_environment",
    }
)  # fmt: skip
OS_META_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
PRESELECT_NAMES = frozenset({".DS_Store", "Thumbs.db"})


@dataclass(frozen=True, slots=True)
class HiddenItem:
    path: Path
    is_dir: bool
    size: int
    category: str
    protected: bool = False
    home_toplevel_dot: bool = False
    preselect: bool = False


@dataclass(frozen=True, slots=True)
class HiddenResult:
    items: tuple[HiddenItem, ...]
    skipped: tuple[SkippedEntry, ...] = ()
    cancelled: bool = False


def classify(name: str, is_dir: bool = False, temp_patterns: bool = True) -> str | None:
    """Category for a name, or None if it is not hidden/temp."""
    if name.startswith("~$"):
        return CAT_OFFICE_LOCK
    if name.startswith("~"):
        return CAT_TILDE
    if name.endswith("~"):
        return CAT_EDITOR_BACKUP
    if temp_patterns and not is_dir:
        if name in OS_META_NAMES:
            return CAT_OS_META
        if fnmatch.fnmatch(name, "*.swp") or fnmatch.fnmatch(name, "*.swo"):
            return CAT_SWAP
        if fnmatch.fnmatch(name, "*.tmp"):
            return CAT_TEMP
    if name.startswith("."):
        return CAT_DOT_FOLDER if is_dir else CAT_DOT_FILE
    return None


def is_protected(path: Path) -> bool:
    return any(part in PROTECTED_NAMES for part in path.parts)


def is_home_toplevel_dot(path: Path) -> bool:
    """True for a top-level dot entry of the home directory, or anything inside one."""
    try:
        rel = path.relative_to(Path.home())
    except ValueError:
        return False
    return bool(rel.parts) and rel.parts[0].startswith(".")


def open_files() -> set[str]:
    """Paths currently held open by any process we may inspect (via /proc/*/fd)."""
    held: set[str] = set()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return held
    for pid in pids:
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                held.add(os.readlink(f"{fd_dir}/{fd}"))
            except OSError:
                continue
    return held


def _preselect(name: str, category: str, path: Path, held: Callable[[], set[str]]) -> bool:
    if category in (CAT_OFFICE_LOCK, CAT_EDITOR_BACKUP) or name in PRESELECT_NAMES:
        return True
    if category == CAT_SWAP and name.endswith(".swp"):
        return str(path) not in held()
    return False


def tree_size(path: Path, cancel: CancelToken) -> int:
    """Recursive size of a directory (symlinks counted as links, never followed)."""
    total = 0
    stack = [str(path)]
    while stack:
        cancel.raise_if_cancelled()
        try:
            with os.scandir(stack.pop()) as it:
                for de in it:
                    try:
                        st = de.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(de.path)
                    else:
                        total += st.st_size
        except OSError:
            continue
    return total


def scan_hidden(
    root: Path,
    cancel: CancelToken | None = None,
    progress: ProgressCallback | None = None,
    temp_patterns: bool = True,
    cross_filesystems: bool = False,
    open_files_fn: Callable[[], set[str]] = open_files,
) -> HiddenResult:
    cancel = cancel or CancelToken()
    root = Path(os.path.abspath(root))
    items: list[HiddenItem] = []
    skipped: list[SkippedEntry] = []
    held_cache: set[str] | None = None

    def held() -> set[str]:
        nonlocal held_cache
        if held_cache is None:
            held_cache = open_files_fn()
        return held_cache

    try:
        root_dev = os.stat(root).st_dev
    except OSError as e:
        return HiddenResult((), (SkippedEntry(str(root), e.strerror or "error"),))

    stack = [str(root)]
    seen = 0
    try:
        while stack:
            cancel.raise_if_cancelled()
            directory = stack.pop()
            try:
                it = os.scandir(directory)
            except OSError as e:
                skipped.append(SkippedEntry(directory, _reason(e)))
                continue
            with it:
                for de in it:
                    cancel.raise_if_cancelled()
                    seen += 1
                    if progress is not None and seen % 200 == 0:
                        progress(Progress(Stage.HIDDEN, seen, 0, de.path))
                    try:
                        st = de.stat(follow_symlinks=False)
                    except OSError as e:
                        skipped.append(SkippedEntry(de.path, _reason(e)))
                        continue
                    is_dir = stat.S_ISDIR(st.st_mode)
                    category = classify(de.name, is_dir, temp_patterns)
                    path = Path(de.path)
                    if category is None:
                        if is_dir and (cross_filesystems or st.st_dev == root_dev):
                            stack.append(de.path)
                        continue
                    size = tree_size(path, cancel) if is_dir else st.st_size
                    protected = is_protected(path)
                    home_dot = is_home_toplevel_dot(path)
                    items.append(
                        HiddenItem(
                            path=path,
                            is_dir=is_dir,
                            size=size,
                            category=category,
                            protected=protected,
                            home_toplevel_dot=home_dot,
                            preselect=not protected
                            and not home_dot
                            and _preselect(de.name, category, path, held),
                        )
                    )
    except CancelledError:
        return HiddenResult(tuple(items), tuple(skipped), cancelled=True)
    if progress is not None:
        progress(Progress(Stage.HIDDEN, seen, seen, ""))
    items.sort(key=lambda i: str(i.path))
    return HiddenResult(tuple(items), tuple(skipped))


def _reason(e: OSError) -> str:
    if isinstance(e, PermissionError):
        return "permission denied"
    if isinstance(e, FileNotFoundError):
        return "vanished"
    return e.strerror or type(e).__name__
