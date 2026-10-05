from __future__ import annotations

from pathlib import Path

from dedupe.core.models import FileEntry, SimilarGroup, SimilarMember, Verdict
from dedupe.core.recommender import recommend_similar_group


def member(path: str, w: int, h: int, size: int, mtime: int = 1) -> SimilarMember:
    return SimilarMember(FileEntry(Path(path), size, mtime, hash(path) & 0xFFFF, 1), w, h, 3, 0.95)


def verdicts(group: SimilarGroup, protected: tuple[str, ...] = ()) -> dict[str, Verdict]:
    return {
        r.path.name: r.verdict for r in recommend_similar_group(group, protected, Path("/photos"))
    }


def test_highest_resolution_wins_even_when_the_file_is_smaller() -> None:
    g = SimilarGroup(
        "g", (member("/photos/a.jpg", 100, 100, 9000), member("/photos/b.jpg", 200, 200, 5000))
    )
    assert verdicts(g) == {"a.jpg": Verdict.DELETE, "b.jpg": Verdict.KEEP}


def test_equal_resolution_prefers_the_larger_file() -> None:
    g = SimilarGroup(
        "g", (member("/photos/a.jpg", 100, 100, 5000), member("/photos/b.jpg", 100, 100, 9000))
    )
    recs = recommend_similar_group(g)
    assert next(r for r in recs if r.verdict is Verdict.KEEP).reason == "Kept: larger file"
    assert verdicts(g)["b.jpg"] is Verdict.KEEP


def test_then_the_exact_duplicate_rules_apply() -> None:
    g = SimilarGroup(
        "g",
        (member("/photos/IMG (1).jpg", 100, 100, 5000), member("/photos/IMG.jpg", 100, 100, 5000)),
    )
    assert verdicts(g) == {"IMG (1).jpg": Verdict.DELETE, "IMG.jpg": Verdict.KEEP}


def test_protected_images_are_never_marked_delete() -> None:
    g = SimilarGroup(
        "g",
        (
            member("/photos/big.jpg", 400, 400, 9000),
            member("/vault/precious.jpg", 100, 100, 1000),
            member("/photos/other.jpg", 90, 90, 900),
        ),
    )
    assert verdicts(g, ("/vault",)) == {
        "big.jpg": Verdict.KEEP,
        "precious.jpg": Verdict.KEEP,
        "other.jpg": Verdict.DELETE,
    }


def test_reclaimable_is_the_size_of_every_non_keeper() -> None:
    g = SimilarGroup(
        "g", (member("/photos/a.jpg", 100, 100, 9000), member("/photos/b.jpg", 200, 200, 5000))
    )
    rated = SimilarGroup("g", g.members, recommend_similar_group(g))
    assert rated.reclaimable == 9000
