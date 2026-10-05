"""Sort order and filters for the duplicates list. Pure functions: they run in a job, so
sorting or filtering 100k files never happens on the GUI thread."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from dedupe.core.models import DuplicateGroup
from dedupe.gui.file_types import classify


class SortKey(StrEnum):
    RECLAIMABLE = "Reclaimable space"
    SIZE = "File size"
    COUNT = "Number of copies"
    NAME = "File name"
    PATH = "File path"


@dataclass(frozen=True, slots=True)
class ViewOptions:
    sort: SortKey = SortKey.RECLAIMABLE
    file_type: str | None = None  # a CATEGORIES value, or None for all
    text: str = ""  # case-insensitive substring of any copy's path

    @property
    def is_default(self) -> bool:
        return self == ViewOptions()


def _sort_key(sort: SortKey) -> Callable[[DuplicateGroup], tuple[Any, ...]]:
    if sort == SortKey.NAME:
        return lambda g: (min(f.path.name.lower() for f in g.files), g.hash)
    if sort == SortKey.PATH:
        return lambda g: (min(str(f.path).lower() for f in g.files), g.hash)
    if sort == SortKey.SIZE:
        return lambda g: (-g.size, -g.reclaimable, g.hash)
    if sort == SortKey.COUNT:
        return lambda g: (-len(g.files), -g.reclaimable, g.hash)
    return lambda g: (-g.reclaimable, g.hash)


def select_groups(groups: Iterable[DuplicateGroup], options: ViewOptions) -> list[DuplicateGroup]:
    """Groups matching the filters, in the requested order. Every group keeps all its files,
    so children always stay together under their group."""
    needle = options.text.strip().lower()
    wanted = options.file_type
    out: list[DuplicateGroup] = []
    for n, g in enumerate(groups):
        if n % 512 == 0:
            time.sleep(0.0001)  # let the GUI thread take the GIL
        if wanted is not None and not any(classify(f.path) == wanted for f in g.files):
            continue
        if needle and not any(needle in str(f.path).lower() for f in g.files):
            continue
        out.append(g)
    out.sort(key=_sort_key(options.sort))
    return out
