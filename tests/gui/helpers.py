from __future__ import annotations

from pathlib import Path

from dedupe.core.models import (
    DuplicateGroup,
    FileEntry,
    Recommendation,
    SimilarGroup,
    SimilarMember,
    Verdict,
)


def synthetic_groups(
    n_groups: int, copies: int = 3, size: int = 1000, start: int = 0
) -> tuple[DuplicateGroup, ...]:
    """In-memory groups (no disk). The first copy of each group is Keep, the rest Delete."""
    groups = []
    inode = start * 10
    for g in range(start, start + n_groups):
        files = []
        for c in range(copies):
            inode += 1
            files.append(
                FileEntry(
                    Path(f"/data/dir{g % 200}/g{g}/copy{c}.bin"),
                    size + g % 7,
                    1_600_000_000_000_000_000 + inode,
                    inode,
                    1,
                )
            )
        recs = tuple(
            Recommendation(
                f.path,
                Verdict.KEEP if i == 0 else Verdict.DELETE,
                "Kept: oldest" if i == 0 else f"Duplicate of {files[0].path}",
            )
            for i, f in enumerate(files)
        )
        groups.append(DuplicateGroup(f"{g:032x}", files[0].size, tuple(files), (), recs))
    return tuple(groups)


def synthetic_similar_groups(
    n_groups: int, members: int = 3, aliases_on_first: int = 0
) -> tuple[SimilarGroup, ...]:
    """In-memory similar groups (no disk). The first member is the highest resolution (Keep)."""
    groups = []
    inode = 0
    for g in range(n_groups):
        built = []
        recs = []
        for c in range(members):
            inode += 1
            entry = FileEntry(
                Path(f"/pics/dir{g % 200}/g{g}/img{c}.jpg"),
                50_000 - c * 7000,
                1_600_000_000_000_000_000 + inode,
                inode,
                1,
            )
            extra = tuple(
                Path(f"{entry.path}.copy{k}") for k in range(aliases_on_first if c == 0 else 0)
            )
            built.append(
                SimilarMember(entry, 4000 - c * 800, 3000 - c * 600, c * 2, 1.0 - c * 0.04, extra)
            )
            recs.append(
                Recommendation(
                    entry.path,
                    Verdict.KEEP if c == 0 else Verdict.DELETE,
                    "Kept: highest resolution" if c == 0 else f"Similar to {built[0].entry.path}",
                )
            )
        groups.append(SimilarGroup(f"{g:016x}", tuple(built), tuple(recs)))
    return tuple(groups)
