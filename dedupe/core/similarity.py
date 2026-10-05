"""Matching perceptual hashes: candidate pairs, SSIM verification and leader clustering.

Needs numpy (the ``similar`` extra); the pipeline imports this module only when the feature is on.

* ``find_candidates`` compares every pair by brute force in numpy, in blocks, so memory stays
  bounded and cancellation is checked between blocks. A pair is a candidate when, for one of the
  8 rotations/mirror images of the first image, both its pHash and dHash are within ``bound`` bits
  of the second image's.
* ``verify`` accepts a pair outright when both distances are within the user's threshold; pairs in
  the grey zone up to ``bound`` must also pass an SSIM check on the 64x64 working copies.
* ``leader_clusters`` forms groups so that every member matches its group's leader. Connected
  components would chain A~B and B~C into one group even when A and C look nothing alike.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from dedupe.core.models import MAX_SIMILARITY_THRESHOLD, CancelToken
from dedupe.core.perceptual import VARIANTS, PerceptualHash, variant

HASH_BITS = 64
GREY_ZONE_FLOOR = 12  # candidates are searched at least this far out, even for a strict threshold
SSIM_MIN = 0.90
SSIM_WINDOW = 7

ROW_BLOCK = 32
COL_BLOCK = 4096

Thumbnails = Callable[[int], "npt.NDArray[np.uint8] | None"]


def candidate_bound(threshold: int) -> int:
    return min(MAX_SIMILARITY_THRESHOLD, max(threshold, GREY_ZONE_FLOOR))


@dataclass(frozen=True, slots=True)
class Candidate:
    i: int  # index of the first image (smaller than j)
    j: int
    variant: int  # which rotation/mirror of image i matches image j best
    phash: int  # pHash distance at that variant
    dhash: int


@dataclass(frozen=True, slots=True)
class Accepted:
    i: int
    j: int
    variant: int
    distance: int  # pHash distance
    similarity: float  # 0..1: SSIM, or derived from the distance when SSIM was not needed


# -- candidates -----------------------------------------------------------------------------


def _arrays(
    hashes: Sequence[PerceptualHash],
) -> tuple[npt.NDArray[np.uint64], npt.NDArray[np.uint64], npt.NDArray[np.uint64]]:
    n = len(hashes)
    var_p = np.empty((n, VARIANTS), dtype=np.uint64)
    var_d = np.empty((n, VARIANTS), dtype=np.uint64)
    plain = np.empty((2, n), dtype=np.uint64)
    for idx, h in enumerate(hashes):
        for k, (p, d) in enumerate(h.variants):
            var_p[idx, k] = p
            var_d[idx, k] = d
        plain[0, idx], plain[1, idx] = h.phash, h.dhash
    return var_p, var_d, plain


def find_candidates(
    hashes: Sequence[PerceptualHash],
    bound: int,
    cancel: CancelToken | None = None,
    on_rows: Callable[[int, int], None] | None = None,
) -> list[Candidate]:
    """Every pair (i < j) with a variant of i within ``bound`` bits of j in both hashes.

    All hashes must be usable. ``on_rows(done, total)`` is called after each block of rows."""
    n = len(hashes)
    if n < 2:
        return []
    var_p, var_d, plain = _arrays(hashes)
    found_i: list[npt.NDArray[np.intp]] = []
    found_j: list[npt.NDArray[np.intp]] = []
    for r0 in range(0, n - 1, ROW_BLOCK):
        r1 = min(r0 + ROW_BLOCK, n)
        rows = np.arange(r0, r1)
        for c0 in range(r0, n, COL_BLOCK):
            if cancel is not None:
                cancel.raise_if_cancelled()
            c1 = min(c0 + COL_BLOCK, n)
            xp = np.bitwise_count(var_p[r0:r1, :, None] ^ plain[0, None, None, c0:c1])
            ok = xp <= bound
            xd = np.bitwise_count(var_d[r0:r1, :, None] ^ plain[1, None, None, c0:c1])
            ok &= xd <= bound
            hit = ok.any(axis=1)  # (rows, cols): some variant matches both hashes
            hit &= np.arange(c0, c1)[None, :] > rows[:, None]  # each pair once, i < j
            ri, cj = np.nonzero(hit)
            if ri.size:
                found_i.append(ri + r0)
                found_j.append(cj + c0)
        if on_rows is not None:
            on_rows(r1, n)
    if not found_i:
        return []
    ii, jj = np.concatenate(found_i), np.concatenate(found_j)
    return _best_variants(ii, jj, var_p, var_d, plain)


def _best_variants(
    ii: npt.NDArray[np.intp],
    jj: npt.NDArray[np.intp],
    var_p: npt.NDArray[np.uint64],
    var_d: npt.NDArray[np.uint64],
    plain: npt.NDArray[np.uint64],
) -> list[Candidate]:
    """For each pair, the variant with the smallest worse-of-the-two distance."""
    dp = np.bitwise_count(var_p[ii] ^ plain[0, jj][:, None]).astype(np.int64)
    dd = np.bitwise_count(var_d[ii] ^ plain[1, jj][:, None]).astype(np.int64)
    k = np.arange(VARIANTS)[None, :]
    key = (np.maximum(dp, dd) << 12) | ((dp + dd) << 4) | k  # ties: sum, then variant number
    best = np.argmin(key, axis=1)
    rows = np.arange(len(ii))
    return [
        Candidate(int(i), int(j), int(v), int(p), int(d))
        for i, j, v, p, d in zip(ii, jj, best, dp[rows, best], dd[rows, best], strict=True)
    ]


# -- verification ---------------------------------------------------------------------------


def _box_mean(a: npt.NDArray[np.float64], win: int) -> npt.NDArray[np.float64]:
    c = np.pad(np.cumsum(np.cumsum(a, axis=0), axis=1), ((1, 0), (1, 0)))
    total = c[win:, win:] - c[:-win, win:] - c[win:, :-win] + c[:-win, :-win]
    return total / (win * win)


def ssim(a: npt.NDArray[np.uint8], b: npt.NDArray[np.uint8], win: int = SSIM_WINDOW) -> float:
    """Mean structural similarity of two equally sized grayscale images (1.0 = identical)."""
    x, y = a.astype(np.float64), b.astype(np.float64)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    ux, uy = _box_mean(x, win), _box_mean(y, win)
    norm = win * win / (win * win - 1)  # sample covariance, as in the reference implementation
    vx = norm * (_box_mean(x * x, win) - ux * ux)
    vy = norm * (_box_mean(y * y, win) - uy * uy)
    vxy = norm * (_box_mean(x * y, win) - ux * uy)
    score = ((2 * ux * uy + c1) * (2 * vxy + c2)) / ((ux * ux + uy * uy + c1) * (vx + vy + c2))
    return float(score.mean())


def verify(cand: Candidate, threshold: int, thumbnails: Thumbnails) -> Accepted | None:
    """Accept a candidate, or None. Within the threshold on both hashes: accepted without
    SSIM. In the grey zone: the aligned working copies must reach SSIM_MIN."""
    if cand.phash <= threshold and cand.dhash <= threshold:
        similarity = 1 - cand.phash / HASH_BITS
        return Accepted(cand.i, cand.j, cand.variant, cand.phash, similarity)
    first, second = thumbnails(cand.i), thumbnails(cand.j)
    if first is None or second is None:  # an image became unreadable: cannot confirm
        return None
    score = ssim(variant(first, cand.variant), second)
    if score < SSIM_MIN:
        return None
    return Accepted(cand.i, cand.j, cand.variant, cand.phash, score)


# -- clustering -----------------------------------------------------------------------------


def leader_clusters(
    order: Sequence[int], accepted: Mapping[tuple[int, int], Accepted]
) -> list[tuple[int, list[tuple[int, Accepted]]]]:
    """Groups as (leader, [(member, pair-with-leader)]), best leader first.

    ``order`` ranks the images best first. Each image joins the best-ranked leader it has an
    accepted pair with, or becomes a new leader. Every member therefore matches its leader."""
    neighbours: dict[int, list[int]] = {}
    for i, j in accepted:
        neighbours.setdefault(i, []).append(j)
        neighbours.setdefault(j, []).append(i)
    rank = {idx: r for r, idx in enumerate(order)}
    groups: dict[int, list[tuple[int, Accepted]]] = {}
    for idx in order:
        leaders = [n for n in neighbours.get(idx, ()) if n in groups]
        if leaders:
            leader = min(leaders, key=rank.__getitem__)
            groups[leader].append((idx, accepted[(min(idx, leader), max(idx, leader))]))
        else:
            groups[idx] = []
    return [(leader, members) for leader, members in groups.items() if members]
