"""Deleting duplicates: plan first (refuse unsafe selections), then execute.

Safety design (this is the only module allowed to remove files; see test_deletion_audit):

* ``plan_actions`` never touches the disk. It REFUSES (raises ``PlanRefused``) any selection
  that would remove every copy of a group, touches a protected path, names a file that is not
  in a group, or asks for hard links across filesystems.
* ``execute`` re-verifies each file immediately before acting (still a regular file with the
  same size, mtime, inode and device as at scan time) and that at least one *kept* copy is
  still unchanged. Changed files are skipped and reported, never acted on.
* Trash failures are reported per file. There is NEVER a fallback to permanent deletion.
* A dry run goes through the same plan and verification and skips only the mutation.
* The action log is opened before the first mutation; if it cannot be, nothing is touched.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import IO

import send2trash

from dedupe.core import paths
from dedupe.core.hidden import HiddenItem
from dedupe.core.hidden import is_protected as is_protected_name
from dedupe.core.models import (
    CancelToken,
    DeleteMode,
    DuplicateGroup,
    FileEntry,
    Progress,
    ProgressCallback,
    Stage,
    Verdict,
)
from dedupe.core.recommender import is_protected

TrashFn = Callable[[str], None]


class PlanRefused(Exception):
    """The selection is unsafe. ``reasons`` lists every problem found; nothing was changed."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class ActionError(Exception):
    """Execution could not start safely (e.g. the action log cannot be opened)."""


class Status(StrEnum):
    DONE = "done"
    DRY_RUN = "would act"
    CHANGED = "changed since scan"
    KEEPER_CHANGED = "kept copy missing or changed"
    FAILED = "failed"
    NOT_RUN = "not run (cancelled)"
    ALREADY_LINKED = "already hard-linked"


@dataclass(frozen=True, slots=True)
class PlannedItem:
    entry: FileEntry
    group_hash: str
    keepers: tuple[FileEntry, ...]  # group members not selected for removal
    link_target: FileEntry | None = None  # hardlink mode: the copy that stays
    requires_keeper: bool = True  # False only for hidden/temp files, which have no twin
    is_dir: bool = False


@dataclass(frozen=True, slots=True)
class ActionPlan:
    mode: DeleteMode
    items: tuple[PlannedItem, ...]
    hardlink_possible: bool
    hardlink_reason: str = ""

    @property
    def total_size(self) -> int:
        return sum(i.entry.size for i in self.items)


@dataclass(frozen=True, slots=True)
class ActionResult:
    path: Path
    size: int
    status: Status
    detail: str = ""


EXAMPLES_PER_STATUS = 10


@dataclass(frozen=True, slots=True)
class ActionSummary:
    """Totals are computed once, off the GUI thread, so showing a summary is O(1)."""

    mode: DeleteMode
    dry_run: bool
    results: tuple[ActionResult, ...]
    warnings: tuple[str, ...] = ()
    counts: dict[Status, int] = field(default_factory=dict)
    freed: int = 0
    examples: dict[Status, tuple[ActionResult, ...]] = field(default_factory=dict)
    gone: frozenset[Path] = frozenset()  # paths that no longer exist as separate files

    @classmethod
    def build(
        cls,
        mode: DeleteMode,
        dry_run: bool,
        results: list[ActionResult],
        warnings: list[str],
    ) -> ActionSummary:
        counts: dict[Status, int] = {}
        examples: dict[Status, list[ActionResult]] = {}
        ok = Status.DRY_RUN if dry_run else Status.DONE
        freed = 0
        gone: set[Path] = set()
        for r in results:
            counts[r.status] = counts.get(r.status, 0) + 1
            if r.status is ok:
                freed += r.size
            if r.status in (Status.DONE, Status.ALREADY_LINKED):
                gone.add(r.path)
            if r.status is not ok:
                bucket = examples.setdefault(r.status, [])
                if len(bucket) < EXAMPLES_PER_STATUS:
                    bucket.append(r)
        return cls(
            mode,
            dry_run,
            tuple(results),
            tuple(warnings),
            counts,
            freed,
            {k: tuple(v) for k, v in examples.items()},
            frozenset(gone),
        )

    def count(self, status: Status) -> int:
        return self.counts.get(status, 0)

    @property
    def done(self) -> int:
        return self.count(Status.DRY_RUN if self.dry_run else Status.DONE)


def groups_after(
    groups: Iterable[DuplicateGroup], removed: set[Path] | frozenset[Path]
) -> list[DuplicateGroup]:
    """The duplicate groups that remain once ``removed`` files are gone. A group left with
    fewer than two copies is no longer a duplicate group."""
    remaining: list[DuplicateGroup] = []
    for n, g in enumerate(groups):
        _cooperate(n, 512)
        if removed.isdisjoint(f.path for f in g.files):
            remaining.append(g)
            continue
        files = tuple(f for f in g.files if f.path not in removed)
        if len(files) >= 2:
            recs = tuple(r for r in g.recommendations if r.path not in removed)
            remaining.append(replace(g, files=files, recommendations=recs))
    return remaining


# -- planning guards (each is exercised by a mutation test) ---------------------------------


def _cooperate(n: int, every: int = 256) -> None:
    """Let other threads (a GUI event loop) take the GIL during long pure-Python loops."""
    if n % every == 0:
        time.sleep(0.0001)  # a real (tiny) sleep: sleep(0) can be re-won by this thread


def _check_known_paths(
    selection: set[Path], index: dict[Path, DuplicateGroup], reasons: list[str]
) -> None:
    unknown = []
    for n, p in enumerate(selection):
        _cooperate(n)
        if p not in index:
            unknown.append(f"{p}: not part of any duplicate group")
    reasons.extend(sorted(unknown))


def _check_last_copy(
    selected_by_group: dict[str, set[Path]], groups: dict[str, DuplicateGroup], reasons: list[str]
) -> None:
    for n, (key, selected) in enumerate(selected_by_group.items()):
        _cooperate(n, 512)
        g = groups[key]
        if {f.path for f in g.files} <= selected:
            reasons.append(
                f"every copy of {g.files[0].path.name} ({g.hash[:12]}) is selected; "
                "at least one copy must be kept"
            )


def _check_protected(selection: set[Path], protected: tuple[str, ...], reasons: list[str]) -> None:
    if protected:
        hits = []
        for n, p in enumerate(selection):
            _cooperate(n)
            if is_protected(p, protected):
                hits.append(f"{p}: is in a protected folder")
        reasons.extend(sorted(hits))


def _check_hardlink_feasible(items: list[PlannedItem]) -> str:
    """Empty string if every selected file can be replaced by a link to its target."""
    for n, item in enumerate(items):
        _cooperate(n, 512)
        target = item.link_target
        if target is None:
            return f"{item.entry.path}: no kept copy to link to"
        if target.device != item.entry.device:
            return f"{item.entry.path}: on a different filesystem than {target.path}"
    return ""


def plan_actions(
    groups: Iterable[DuplicateGroup],
    selection: Iterable[Path],
    mode: DeleteMode = DeleteMode.TRASH,
    protected_folders: Iterable[str] = (),
) -> ActionPlan:
    groups = list(groups)
    selected = selection if isinstance(selection, set) else {Path(p) for p in selection}
    protected = tuple(protected_folders)
    index: dict[Path, DuplicateGroup] = {}
    by_key: dict[str, DuplicateGroup] = {}
    for n, g in enumerate(groups):
        _cooperate(n, 64)
        by_key[_key(g)] = g
        for f in g.files:
            index[f.path] = g

    reasons: list[str] = []
    _check_known_paths(selected, index, reasons)
    _check_protected(selected, protected, reasons)
    selected_by_group: dict[str, set[Path]] = {}
    for n, p in enumerate(selected):
        _cooperate(n)
        if p in index:
            selected_by_group.setdefault(_key(index[p]), set()).add(p)
    _check_last_copy(selected_by_group, by_key, reasons)
    if reasons:
        raise PlanRefused(reasons)

    items: list[PlannedItem] = []
    for key, paths_ in sorted(selected_by_group.items()):
        g = by_key[key]
        keepers = tuple(f for f in g.files if f.path not in paths_)
        target = _best_keeper(g, keepers)
        _cooperate(len(items), 64)
        for f in sorted((f for f in g.files if f.path in paths_), key=lambda f: str(f.path)):
            items.append(PlannedItem(f, g.hash, keepers, target))
    reason = _check_hardlink_feasible(items) if items else "nothing selected"
    plan = ActionPlan(mode, tuple(items), hardlink_possible=not reason, hardlink_reason=reason)
    if mode is DeleteMode.HARDLINK and not plan.hardlink_possible:
        raise PlanRefused([f"hard links are not possible: {reason}"])
    return plan


def plan_hidden_actions(
    items: Iterable[HiddenItem],
    mode: DeleteMode = DeleteMode.TRASH,
    allow_protected: Iterable[Path] = (),
) -> ActionPlan:
    """Plan removal of hidden/temp files and folders (no duplicate groups, no kept copy).

    Refused outright: hard-link mode, the filesystem root, the home folder itself, and any item
    on the protected list unless the caller names it in ``allow_protected`` (the GUI does that
    only after the user's extra confirmation)."""
    items = list(items)
    allowed = {Path(p) for p in allow_protected}
    home = Path.home()
    reasons: list[str] = []
    if mode is DeleteMode.HARDLINK:
        reasons.append("hard links do not apply to hidden and temporary files")
    seen: set[Path] = set()
    for item in items:
        p = item.path
        if p in seen:
            continue
        seen.add(p)
        if p == Path(p.anchor) or p == home or p in home.parents:
            reasons.append(f"{p}: refusing to remove the root or home folder")
        elif (item.protected or is_protected_name(p)) and p not in allowed:
            reasons.append(f"{p}: is on the protected list and was not explicitly confirmed")
    if reasons:
        raise PlanRefused(sorted(reasons))
    planned = [
        PlannedItem(
            FileEntry(i.path, i.size, i.mtime_ns, i.inode, i.device, i.mode),
            "",
            (),
            None,
            requires_keeper=False,
            is_dir=i.is_dir,
        )
        for i in sorted({i.path: i for i in items}.values(), key=lambda i: str(i.path))
    ]
    return ActionPlan(mode, tuple(planned), False, "hard links do not apply to hidden files")


def _key(g: DuplicateGroup) -> str:
    return g.hash or "|".join(sorted(str(f.path) for f in g.files))


def _best_keeper(g: DuplicateGroup, keepers: tuple[FileEntry, ...]) -> FileEntry | None:
    keep_paths = {r.path for r in g.recommendations if r.verdict is Verdict.KEEP}
    for f in keepers:
        if f.path in keep_paths:
            return f
    return keepers[0] if keepers else None


# -- execution guards -----------------------------------------------------------------------


def _verify_unchanged(entry: FileEntry) -> str | None:
    """None if the file is still exactly what the scan saw, else a reason."""
    try:
        st = os.lstat(entry.path)
    except FileNotFoundError:
        return "no longer exists"
    except OSError as e:
        return e.strerror or "cannot stat"
    if entry.mode:
        if stat.S_IFMT(st.st_mode) != stat.S_IFMT(entry.mode):
            return "its type (file, folder, link) changed"
    elif not stat.S_ISREG(st.st_mode):
        return "no longer a regular file"
    # A folder's size is the sum of its contents at scan time, so only its identity and
    # modification time can be compared.
    size_matches = stat.S_ISDIR(st.st_mode) or st.st_size == entry.size
    if not size_matches or (st.st_mtime_ns, st.st_ino, st.st_dev) != (
        entry.mtime_ns,
        entry.inode,
        entry.device,
    ):
        return "size, modification time or identity differs from the scan"
    return None


def _verify_keeper(keepers: tuple[FileEntry, ...]) -> FileEntry | None:
    """A kept copy that still exists unchanged (the proof the content survives), or None."""
    for k in keepers:
        if _verify_unchanged(k) is None:
            return k
    return None


# -- log ------------------------------------------------------------------------------------


class ActionLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[str] | None = None

    def open(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8")  # noqa: SIM115
        except OSError as e:
            raise ActionError(f"cannot open the action log {self.path}: {e.strerror}") from e

    def write(self, record: dict[str, object]) -> None:
        assert self._fh is not None
        record = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), **record}
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# -- execution ------------------------------------------------------------------------------


def execute(
    plan: ActionPlan,
    dry_run: bool = False,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    log_path: Path | None = None,
    trash: TrashFn | None = None,
) -> ActionSummary:
    cancel = cancel or CancelToken()
    trash_fn: TrashFn = trash or send2trash.send2trash
    log = ActionLog(log_path or paths.action_log_file())
    if not dry_run:
        log.open()  # before the first mutation: no log, no deletion
    results: list[ActionResult] = []
    warnings: list[str] = []
    total = len(plan.items)
    try:
        for n, item in enumerate(plan.items):
            _cooperate(n, 64)
            if progress is not None:
                progress(Progress(Stage.ACTION, n, total, str(item.entry.path)))
            if cancel.cancelled:  # checked between files, after the callback had its say
                results.extend(
                    ActionResult(i.entry.path, i.entry.size, Status.NOT_RUN) for i in plan.items[n:]
                )
                break
            result = _execute_one(plan.mode, item, dry_run, trash_fn)
            results.append(result)
            if not dry_run:
                try:
                    log.write(_log_record(plan.mode, item, result))
                except OSError as e:
                    warnings.append(f"could not write the action log: {e.strerror}")
        if progress is not None:
            progress(Progress(Stage.ACTION, len(results), total, ""))
    finally:
        log.close()
    return ActionSummary.build(plan.mode, dry_run, results, warnings)


def _execute_one(
    mode: DeleteMode, item: PlannedItem, dry_run: bool, trash: TrashFn
) -> ActionResult:
    entry = item.entry
    problem = _verify_unchanged(entry)
    if problem:
        return ActionResult(entry.path, entry.size, Status.CHANGED, problem)
    keeper: FileEntry | None = None
    if item.requires_keeper:
        keeper = _verify_keeper(
            (item.link_target,)
            if mode is DeleteMode.HARDLINK and item.link_target
            else item.keepers
        )
        if keeper is None:
            return ActionResult(entry.path, entry.size, Status.KEEPER_CHANGED)
    if mode is DeleteMode.HARDLINK:
        if keeper is None:
            return ActionResult(
                entry.path, entry.size, Status.FAILED, "hard links need a kept copy"
            )
        if keeper.device != entry.device:
            return ActionResult(entry.path, entry.size, Status.FAILED, "different filesystem")
        if keeper.inode == entry.inode:
            return ActionResult(entry.path, entry.size, Status.ALREADY_LINKED)
    if dry_run:
        return ActionResult(entry.path, entry.size, Status.DRY_RUN)
    try:
        if mode is DeleteMode.TRASH:
            trash(str(entry.path))  # on failure: reported, never retried as a permanent delete
        elif mode is DeleteMode.PERMANENT:
            if item.is_dir:
                shutil.rmtree(entry.path)  # never follows symlinks; a failure is reported per item
            else:
                os.unlink(entry.path)
        else:
            assert keeper is not None
            _replace_with_hardlink(keeper.path, entry.path)
    except (OSError, send2trash.TrashPermissionError) as e:
        return ActionResult(entry.path, entry.size, Status.FAILED, _describe(e))
    return ActionResult(
        entry.path, entry.size, Status.DONE, f"kept {keeper.path}" if keeper is not None else ""
    )


def _replace_with_hardlink(keep: Path, dup: Path) -> None:
    """Link to a temp name beside the duplicate, then atomically rename over it. A failure at
    any point leaves the duplicate in place and removes the temp link."""
    tmp = dup.with_name(f".{dup.name}.dedupe-{uuid.uuid4().hex[:8]}.tmp")
    try:
        os.link(keep, tmp)
        os.replace(tmp, dup)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)  # only still present if the replace did not happen


def _describe(e: Exception) -> str:
    return getattr(e, "strerror", None) or str(e) or type(e).__name__


def _log_record(mode: DeleteMode, item: PlannedItem, result: ActionResult) -> dict[str, object]:
    record: dict[str, object] = {
        "action": mode.value,
        "kind": "folder" if item.is_dir else "file",
        "path": str(item.entry.path),
        "size": item.entry.size,
        "hash": item.group_hash,
        "status": result.status.value,
    }
    if result.detail:
        record["detail"] = result.detail
    if mode is DeleteMode.HARDLINK and item.link_target is not None:
        record["linked_to"] = str(item.link_target.path)
    return record
