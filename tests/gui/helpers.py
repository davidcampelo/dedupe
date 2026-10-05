from __future__ import annotations

from pathlib import Path

from dedupe.core.models import DuplicateGroup, FileEntry, Recommendation, Verdict


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
