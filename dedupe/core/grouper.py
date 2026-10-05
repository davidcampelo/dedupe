"""Staged duplicate grouping: size -> hardlink collapse -> partial -> full -> byte compare.

Each stage takes buckets of candidate files and returns smaller buckets; a bucket with a
single file can have no duplicates and is dropped. A similarity-based stage can be added
to the chain later (spec section 11).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from dedupe.core.hasher import CHUNK, HashService
from dedupe.core.models import (
    CancelToken,
    DuplicateGroup,
    FileEntry,
    Progress,
    ProgressCallback,
    SkippedEntry,
    Stage,
)

Bucket = list[FileEntry]


@dataclass
class Context:
    hashes: HashService
    cancel: CancelToken
    progress: ProgressCallback | None = None
    # (device, inode) -> extra paths that are hard links of the representative kept in the chain
    links: dict[tuple[int, int], list[Path]] = field(default_factory=dict)
    full_hashes: dict[Path, str] = field(default_factory=dict)


class GroupingStage(Protocol):
    name: str

    def apply(self, buckets: list[Bucket], ctx: Context) -> list[Bucket]: ...


def _drop_singletons(buckets: list[Bucket]) -> list[Bucket]:
    return [b for b in buckets if len(b) > 1]


class SizeStage:
    name = "size"

    def apply(self, buckets: list[Bucket], ctx: Context) -> list[Bucket]:
        by_size: dict[int, Bucket] = defaultdict(list)
        for bucket in buckets:
            for e in bucket:
                by_size[e.size].append(e)
        if ctx.progress:
            ctx.progress(Progress(Stage.SIZE, 1, 1))
        return _drop_singletons(list(by_size.values()))


class HardlinkStage:
    """Files sharing (device, inode) are one file. Keep one representative per identity and
    remember the other paths; deleting a hard link frees no space."""

    name = "hardlink"

    def apply(self, buckets: list[Bucket], ctx: Context) -> list[Bucket]:
        out: list[Bucket] = []
        for bucket in buckets:
            reps: dict[tuple[int, int], FileEntry] = {}
            for e in sorted(bucket, key=lambda x: str(x.path)):
                if e.identity in reps:
                    ctx.links.setdefault(e.identity, []).append(e.path)
                else:
                    reps[e.identity] = e
            out.append(list(reps.values()))
        return _drop_singletons(out)


class PartialHashStage:
    name = "partial"

    def apply(self, buckets: list[Bucket], ctx: Context) -> list[Bucket]:
        hashes = ctx.hashes.partial(e for b in buckets for e in b)
        return _split(buckets, lambda e: hashes.get(e.path))


class FullHashStage:
    name = "full"

    def apply(self, buckets: list[Bucket], ctx: Context) -> list[Bucket]:
        hashes = ctx.hashes.full(e for b in buckets for e in b)
        ctx.full_hashes.update(hashes)
        return _split(buckets, lambda e: hashes.get(e.path))


class ByteCompareStage:
    """Paranoid mode: confirm byte-for-byte equality inside each hash group."""

    name = "compare"

    def apply(self, buckets: list[Bucket], ctx: Context) -> list[Bucket]:
        out: list[Bucket] = []
        done = 0
        total = sum(e.size for b in buckets for e in b)
        for bucket in buckets:
            subgroups: list[Bucket] = []
            for e in bucket:
                for sg in subgroups:
                    try:
                        same = files_equal(sg[0].path, e.path, ctx.cancel)
                    except OSError as err:
                        ctx.hashes.failed.append(SkippedEntry(str(e.path), err.strerror or "error"))
                        break
                    if same:
                        sg.append(e)
                        break
                else:
                    subgroups.append([e])
                done += e.size
                if ctx.progress:
                    ctx.progress(Progress(Stage.COMPARE, done, total, str(e.path)))
            out.extend(subgroups)
        return _drop_singletons(out)


def _split(buckets: list[Bucket], key: Callable[[FileEntry], str | None]) -> list[Bucket]:
    out: list[Bucket] = []
    for bucket in buckets:
        sub: dict[str, Bucket] = defaultdict(list)
        for e in bucket:
            k = key(e)
            if k is not None:  # None: hashing failed, the file is already in `skipped`
                sub[k].append(e)
        out.extend(sub.values())
    return _drop_singletons(out)


def files_equal(a: Path, b: Path, cancel: CancelToken | None = None) -> bool:
    with open(a, "rb", buffering=0) as fa, open(b, "rb", buffering=0) as fb:
        while True:
            if cancel is not None:
                cancel.raise_if_cancelled()
            ca, cb = fa.read(CHUNK), fb.read(CHUNK)
            if ca != cb:
                return False
            if not ca:
                return True


@dataclass(frozen=True, slots=True)
class GroupingOutcome:
    groups: tuple[DuplicateGroup, ...]
    empty_files: tuple[FileEntry, ...]
    hardlink_sets: tuple[tuple[Path, ...], ...]
    skipped: tuple[SkippedEntry, ...]


def default_stages(paranoid: bool) -> list[GroupingStage]:
    stages: list[GroupingStage] = [
        SizeStage(),
        HardlinkStage(),
        PartialHashStage(),
        FullHashStage(),
    ]
    if paranoid:
        stages.append(ByteCompareStage())
    return stages


def group_duplicates(
    entries: list[FileEntry],
    hashes: HashService,
    cancel: CancelToken,
    progress: ProgressCallback | None = None,
    paranoid: bool = False,
    stages: list[GroupingStage] | None = None,
) -> GroupingOutcome:
    empty = tuple(e for e in entries if e.size == 0)
    candidates = [e for e in entries if e.size > 0]
    ctx = Context(hashes, cancel, progress)

    # Hard links with no other content-equal sibling still deserve a report: any identity
    # seen more than once is "already hard-linked".
    by_identity: dict[tuple[int, int], list[Path]] = defaultdict(list)
    for e in candidates:
        by_identity[e.identity].append(e.path)

    buckets: list[Bucket] = [candidates]
    for stage in stages or default_stages(paranoid):
        cancel.raise_if_cancelled()
        buckets = stage.apply(buckets, ctx)

    groups: list[DuplicateGroup] = []
    for bucket in buckets:
        bucket.sort(key=lambda e: str(e.path))
        digest = ctx.full_hashes.get(bucket[0].path, "")
        linked = tuple(p for e in bucket for p in ctx.links.get(e.identity, ()))
        groups.append(DuplicateGroup(digest, bucket[0].size, tuple(bucket), linked))
    groups.sort(key=lambda g: (-g.reclaimable, g.hash))

    # Only identities seen more than once matter; sorting every identity would hold the GIL
    # for ~150 ms on 100k files and freeze a GUI that is running this in a worker thread.
    multi = [(k, v) for k, v in by_identity.items() if len(v) > 1]
    hardlink_sets = tuple(tuple(sorted(paths)) for _, paths in sorted(multi))
    return GroupingOutcome(tuple(groups), empty, hardlink_sets, tuple(hashes.failed))
