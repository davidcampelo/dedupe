from __future__ import annotations

import random
import time

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dedupe.core import similarity
from dedupe.core.models import CancelledError, CancelToken
from dedupe.core.perceptual import VARIANTS, PerceptualHash
from dedupe.core.similarity import (
    Accepted,
    Candidate,
    candidate_bound,
    find_candidates,
    leader_clusters,
    ssim,
    verify,
)

MASK64 = (1 << 64) - 1


def make_hash(variants: list[tuple[int, int]]) -> PerceptualHash:
    return PerceptualHash(variants[0][0], variants[0][1], tuple(variants), 10, 10)


def dist(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def reference_candidates(hashes: list[PerceptualHash], bound: int) -> list[Candidate]:
    """Pure-Python brute force: the specification of find_candidates."""
    out = []
    for i in range(len(hashes)):
        for j in range(i + 1, len(hashes)):
            scored = [
                (max(dp, dd), dp + dd, k, dp, dd)
                for k, (p, d) in enumerate(hashes[i].variants)
                if (dp := dist(p, hashes[j].phash)) <= bound
                and (dd := dist(d, hashes[j].dhash)) <= bound
            ]
            if scored:
                _, _, k, dp, dd = min(scored)
                out.append(Candidate(i, j, k, dp, dd))
    return out


noise = st.lists(st.integers(0, 63), max_size=8).map(lambda bits: sum(1 << b for b in set(bits)))


@st.composite
def hash_sets(draw: st.DrawFn) -> list[PerceptualHash]:
    """Hashes built from a small pool of values plus a few flipped bits, so that matches (and
    near misses) are common instead of astronomically rare."""
    pool = draw(st.lists(st.integers(0, MASK64), min_size=2, max_size=4))
    n = draw(st.integers(0, 25))
    return [
        make_hash(
            [
                (
                    draw(st.sampled_from(pool)) ^ draw(noise),
                    draw(st.sampled_from(pool)) ^ draw(noise),
                )
                for _ in range(VARIANTS)
            ]
        )
        for _ in range(n)
    ]


@settings(max_examples=60, deadline=None)
@given(hash_sets(), st.integers(0, 16))
def test_candidates_match_the_brute_force_reference(
    hashes: list[PerceptualHash], bound: int
) -> None:
    assert find_candidates(hashes, bound) == reference_candidates(hashes, bound)


def test_candidates_cross_block_boundaries() -> None:
    rng = random.Random(3)
    base = rng.getrandbits(64)
    hashes = [
        make_hash([(base ^ (1 << (i % 8)), base)] * VARIANTS)
        for i in range(similarity.ROW_BLOCK * 2 + 5)
    ]
    assert find_candidates(hashes, 4) == reference_candidates(hashes, 4)


def test_a_rotated_copy_is_matched_through_the_right_variant() -> None:
    p, d = 0x0123456789ABCDEF, 0xFEDCBA9876543210
    a = make_hash([(0, 0)] * 3 + [(p, d)] + [(0, 0)] * 4)  # variant 3 equals b's plain hash
    b = make_hash([(p, d)] + [(1, 1)] * 7)
    [c] = find_candidates([a, b], 4)
    assert (c.i, c.j, c.variant, c.phash, c.dhash) == (0, 1, 3, 0, 0)


def test_both_hashes_must_be_close() -> None:
    a = make_hash([(0, 0)] * VARIANTS)
    b = make_hash([(0, (1 << 40) - 1)] * VARIANTS)  # pHash equal, dHash 40 bits away
    assert find_candidates([a, b], 16) == []


def test_candidate_bound() -> None:
    assert [candidate_bound(t) for t in (0, 4, 8, 12, 14, 16)] == [12, 12, 12, 12, 14, 16]


def test_cancel_mid_run_returns_within_100_ms() -> None:
    rng = np.random.default_rng(0)
    raw = rng.integers(0, 1 << 63, size=(6000, 2), dtype=np.int64)
    hashes = [make_hash([(int(p), int(d))] * VARIANTS) for p, d in raw]
    token = CancelToken()
    stamp: list[float] = []

    def on_rows(done: int, total: int) -> None:
        if not stamp:
            stamp.append(time.perf_counter())
            token.cancel()

    with pytest.raises(CancelledError):
        find_candidates(hashes, 12, token, on_rows)
    assert time.perf_counter() - stamp[0] < 0.1


# -- ssim and verify ------------------------------------------------------------------------


def smooth(seed: int) -> np.ndarray:
    from PIL import Image

    rng = np.random.default_rng(seed)
    coarse = rng.integers(0, 256, (8, 8), dtype=np.uint8)
    return np.asarray(Image.fromarray(coarse).resize((64, 64), Image.Resampling.BICUBIC))


def test_ssim_of_identical_images_is_one() -> None:
    a = smooth(1)
    assert ssim(a, a) == pytest.approx(1.0)


def test_ssim_of_unrelated_images_is_low() -> None:
    assert ssim(smooth(1), smooth(2)) < 0.5


def test_ssim_of_a_slightly_noisy_copy_is_high() -> None:
    a = smooth(1)
    rng = np.random.default_rng(5)
    noisy = np.clip(a.astype(int) + rng.integers(-6, 7, a.shape), 0, 255).astype(np.uint8)
    assert ssim(a, noisy) > 0.95


def never(_: int) -> None:  # a thumbnail provider that must not be used
    raise AssertionError("SSIM was computed")


def test_pair_within_the_threshold_skips_ssim() -> None:
    got = verify(Candidate(0, 1, 0, 6, 8), 8, never)  # type: ignore[arg-type]
    assert got is not None and got.distance == 6
    assert got.similarity == pytest.approx(1 - 6 / 64)


def test_grey_zone_pair_of_the_same_photo_passes() -> None:
    a = smooth(4)
    rng = np.random.default_rng(1)
    copy = np.clip(a.astype(int) + rng.integers(-10, 11, a.shape), 0, 255).astype(np.uint8)
    thumbs = {0: a, 1: copy}
    got = verify(Candidate(0, 1, 0, 11, 12), 8, thumbs.get)
    assert got is not None and got.similarity >= similarity.SSIM_MIN


def test_grey_zone_pair_of_different_photos_fails() -> None:
    thumbs = {0: smooth(4), 1: smooth(9)}
    assert verify(Candidate(0, 1, 0, 11, 12), 8, thumbs.get) is None


def test_grey_zone_pair_is_compared_through_its_variant() -> None:
    a = smooth(4)
    thumbs = {0: a, 1: np.ascontiguousarray(np.rot90(a))}
    assert verify(Candidate(0, 1, 0, 11, 12), 8, thumbs.get) is None  # wrong alignment
    assert verify(Candidate(0, 1, 1, 11, 12), 8, thumbs.get) is not None  # variant 1 = rot90


def test_grey_zone_pair_with_an_unreadable_image_is_rejected() -> None:
    assert verify(Candidate(0, 1, 0, 11, 12), 8, {0: smooth(1)}.get) is None


# -- clustering -----------------------------------------------------------------------------


def acc(i: int, j: int) -> Accepted:
    return Accepted(min(i, j), max(i, j), 0, 1, 0.9)


def test_leader_clustering_does_not_chain() -> None:
    # 0~1 and 1~2 but not 0~2: connected components would merge all three.
    pairs = {(0, 1): acc(0, 1), (1, 2): acc(1, 2)}
    groups = leader_clusters([0, 1, 2], pairs)
    assert [(leader, [m for m, _ in members]) for leader, members in groups] == [(0, [1])]


def test_the_better_ranked_leader_wins() -> None:
    pairs = {(0, 2): acc(0, 2), (1, 2): acc(1, 2)}
    [(leader, members)] = leader_clusters([1, 0, 2], pairs)  # 1 ranks best
    assert leader == 1 and [m for m, _ in members] == [2]


pair_sets = st.integers(2, 14).flatmap(
    lambda n: st.tuples(
        st.just(n),
        st.sets(
            st.tuples(st.integers(0, n - 1), st.integers(0, n - 1)).filter(lambda t: t[0] < t[1])
        ),
        st.permutations(range(n)),
    )
)


@settings(max_examples=150, deadline=None)
@given(pair_sets)
def test_every_member_has_an_accepted_pair_with_its_leader(
    case: tuple[int, set[tuple[int, int]], list[int]],
) -> None:
    _, pairs, order = case
    accepted = {p: acc(*p) for p in pairs}
    groups = leader_clusters(order, accepted)
    seen: set[int] = set()
    for leader, members in groups:
        assert members
        for m, pair in members:
            assert (min(m, leader), max(m, leader)) in accepted
            assert pair is accepted[(min(m, leader), max(m, leader))]
            assert m not in seen and m != leader
            seen.add(m)
        assert leader not in seen
        seen.add(leader)


@settings(max_examples=100, deadline=None)
@given(pair_sets, st.randoms(use_true_random=False))
def test_clusters_depend_on_ranking_not_on_labels(
    case: tuple[int, set[tuple[int, int]], list[int]], rnd: random.Random
) -> None:
    n, pairs, order = case
    relabel = list(range(n))
    rnd.shuffle(relabel)
    renamed = {(min(relabel[i], relabel[j]), max(relabel[i], relabel[j])) for i, j in pairs}

    def shape(order_: list[int], pairs_: set[tuple[int, int]]) -> set[tuple[int, frozenset[int]]]:
        groups = leader_clusters(order_, {p: acc(*p) for p in pairs_})
        return {(g, frozenset(m for m, _ in members)) for g, members in groups}

    expected = {
        (relabel[g], frozenset(relabel[m] for m in members)) for g, members in shape(order, pairs)
    }
    assert shape([relabel[x] for x in order], renamed) == expected
