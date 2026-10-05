"""The similar-images chain, run after the exact-duplicate pass inside ``run_scan``.

    ImageFilter -> Hardlink -> CollapseExact -> PerceptualHash -> Candidates -> Verify -> Cluster

It is a chain of its own because the exact chain starts with ``SizeStage`` and similar images
almost never have the same size. Files that are byte-identical (or hard links of each other)
stand for one image here, so no set of files appears on both the Duplicates and the Similar tab.

Needs the ``similar`` extra; the pipeline imports this module only when the feature is on.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import numpy.typing as npt

from dedupe.core import perceptual, similarity
from dedupe.core.cooperate import cooperate
from dedupe.core.hasher import HashService
from dedupe.core.imaging import similarity_extensions
from dedupe.core.models import (
    CancelToken,
    DuplicateGroup,
    FileEntry,
    Progress,
    ProgressCallback,
    SimilarGroup,
    SimilarMember,
    SkippedEntry,
    Stage,
    Verdict,
)
from dedupe.core.perceptual import PerceptualHash
from dedupe.core.recommender import quality_key, recommend_similar_all

PRESETS = {"strict": 4, "normal": 8, "loose": 12}


@dataclass(frozen=True, slots=True)
class SimilarOutcome:
    groups: tuple[SimilarGroup, ...]
    skipped: tuple[SkippedEntry, ...]


def image_filter(entries: Iterable[FileEntry]) -> list[FileEntry]:
    """Raster images Pillow can decode here, by extension."""
    exts = similarity_extensions()
    return [e for e in entries if e.size > 0 and e.path.suffix.lower() in exts]


def collapse_exact(
    images: Iterable[FileEntry], exact_groups: Iterable[DuplicateGroup]
) -> tuple[dict[Path, FileEntry], dict[Path, list[Path]]]:
    """One representative per image: hard links of a file and byte-identical copies stand for
    one file, the others are listed as its aliases. Returns (representatives, aliases)."""
    reps: dict[Path, FileEntry] = {}
    aliases: dict[Path, list[Path]] = defaultdict(list)
    by_identity: dict[tuple[int, int], Path] = {}
    for e in sorted(images, key=lambda e: str(e.path)):  # canonical: input order never matters
        first = by_identity.setdefault(e.identity, e.path)
        if first == e.path:
            reps[e.path] = e
        else:
            aliases[first].append(e.path)
    for g in exact_groups:
        present = sorted((f.path for f in g.files if f.path in reps), key=str)
        if len(present) < 2:
            continue
        keep_paths = {r.path for r in g.recommendations if r.verdict is Verdict.KEEP}
        keep = next((p for p in present if p in keep_paths), present[0])
        for p in present:
            if p != keep:
                del reps[p]
                aliases[keep] += [p, *aliases.pop(p, [])]
    return reps, aliases


def _group_id(paths: Iterable[Path]) -> str:
    joined = "\n".join(sorted(str(p) for p in paths))
    return hashlib.blake2b(joined.encode(), digest_size=8).hexdigest()


def find_similar(
    entries: Sequence[FileEntry],
    exact_groups: Iterable[DuplicateGroup],
    hashes: HashService,
    threshold: int,
    cancel: CancelToken,
    progress: ProgressCallback | None = None,
    protected_folders: Iterable[str] = (),
    root: Path | None = None,
) -> SimilarOutcome:
    failed_before = len(hashes.failed)
    reps, aliases = collapse_exact(image_filter(entries), exact_groups)
    hashed = hashes.perceptual(reps.values())
    skipped = tuple(hashes.failed[failed_before:])
    cancel.raise_if_cancelled()

    # Canonical order (by path) so the result never depends on the order files were found in.
    paths = sorted(p for p, h in hashed.items() if h.usable)
    if len(paths) < 2:
        return SimilarOutcome((), skipped)
    usable: list[PerceptualHash] = [hashed[p] for p in paths]

    def on_rows(done: int, total: int) -> None:
        if progress is not None:
            progress(Progress(Stage.SIMILAR, done, total, ""))

    bound = similarity.candidate_bound(threshold)
    candidates = similarity.find_candidates(usable, bound, cancel, on_rows)

    thumbs: dict[int, npt.NDArray[np.uint8] | None] = {}

    def thumbnail(idx: int) -> npt.NDArray[np.uint8] | None:
        # Decoded again only for images in a grey-zone pair; they are not kept for the rest.
        if idx not in thumbs:
            try:
                thumbs[idx] = perceptual.working_copy(paths[idx])
            except OSError:
                thumbs[idx] = None
        return thumbs[idx]

    accepted: dict[tuple[int, int], similarity.Accepted] = {}
    for n, cand in enumerate(candidates):
        cancel.raise_if_cancelled()
        cooperate(n, 64)
        result = similarity.verify(cand, threshold, thumbnail)
        if result is not None:
            accepted[(cand.i, cand.j)] = result

    members = [
        SimilarMember(
            reps[p], h.width, h.height, 0, 1.0, tuple(sorted(aliases.get(p, ()), key=str))
        )
        for p, h in zip(paths, usable, strict=True)
    ]
    order = sorted(range(len(members)), key=lambda i: quality_key(members[i], root))
    groups: list[SimilarGroup] = []
    for n, (leader, rest) in enumerate(similarity.leader_clusters(order, accepted)):
        cooperate(n, 64)
        cancel.raise_if_cancelled()
        group_members = [members[leader]]
        for idx, pair in rest:
            m = members[idx]
            group_members.append(
                SimilarMember(m.entry, m.width, m.height, pair.distance, pair.similarity, m.aliases)
            )
        groups.append(
            SimilarGroup(_group_id(m.entry.path for m in group_members), tuple(group_members))
        )
    rated = recommend_similar_all(groups, protected_folders, root)
    return SimilarOutcome(tuple(sorted(rated, key=lambda g: (-g.reclaimable, g.id))), skipped)
