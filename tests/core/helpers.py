from __future__ import annotations

from pathlib import Path

from dedupe.core.grouper import GroupingOutcome, group_duplicates
from dedupe.core.hasher import HashService
from dedupe.core.models import CancelToken, ScanOptions
from dedupe.core.scanner import scan_files


def group(root: Path, paranoid: bool = False, **opts: object) -> GroupingOutcome:
    cancel = CancelToken()
    entries, _ = scan_files(root, ScanOptions(exclude=(), **opts), cancel)  # type: ignore[arg-type]
    return group_duplicates(entries, HashService(cancel), cancel, paranoid=paranoid)


def group_names(outcome: GroupingOutcome) -> list[set[str]]:
    return sorted(({f.path.name for f in g.files} for g in outcome.groups), key=sorted)
