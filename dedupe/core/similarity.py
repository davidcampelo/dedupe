"""Matching perceptual hashes: candidate pairs, SSIM verification and leader clustering.

Needs numpy (the ``similar`` extra); the pipeline imports this module only when the feature is on.

* ``find_candidates`` compares every pair by brute force in numpy, in blocks, so memory stays
  bounded and cancellation is checked between blocks. A pair is a candidate when, for one of the
  8 rotations/mirror images of the first image, both its pHash and dHash are within ``bound`` bits
  of the second image's.
* ``scene_candidates`` adds pairs of shots taken seconds apart (EXIF capture time). A camera that
  moved a little between two shots of the same scene shifts the picture, and pHash and dHash are
  not shift tolerant: a 5% shift already costs 15-25 bits, as much as two unrelated photos.
* ``verify`` accepts a pair outright when both distances are within the user's threshold. Any
  other candidate must pass SSIM after aligning the two images by phase correlation
  (``aligned_ssim``), so a shifted shot of the same scene can still pass: shots taken seconds
  apart on their cached 32x32 sketches (no decoding; the capture time is evidence of its own, so
  the bar is lower), the others on the 64x64 working copies.
* ``leader_clusters`` forms groups so that every member matches its group's leader. Connected
  components would chain A~B and B~C into one group even when A and C look nothing alike.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

import numpy as np
import numpy.typing as npt

from dedupe.core.models import MAX_SIMILARITY_THRESHOLD, CancelToken
from dedupe.core.perceptual import VARIANTS, PerceptualHash, variant

HASH_BITS = 64
GREY_ZONE_FLOOR = 12  # candidates are searched at least this far out, even for a strict threshold
SSIM_MIN = 0.90
SCENE_SSIM_MIN = 0.80  # for shots taken seconds apart, compared on their 32x32 sketches
SSIM_WINDOW = 7
MAX_SHIFT = 0.16  # of the image side: how far the alignment may move an image (10 px of 64)
SHIFT_PEAKS = 3  # phase-correlation peaks tried; the true shift is not always the highest
SHIFT_RETRY = 0.6  # the other peaks are tried only when the best one scores at least this
SCENE_WINDOW = 60  # seconds between two shots that are compared whatever their hashes say
SCENE_NEIGHBOURS = 6  # each shot is compared with at most this many shots taken after it

ROW_BLOCK = 32
COL_BLOCK = 4096

Thumbnails = Callable[[int], "npt.NDArray[np.uint8] | None"]
Gray = npt.NDArray[np.uint8] | npt.NDArray[np.float64]


def candidate_bound(threshold: int) -> int:
    return min(MAX_SIMILARITY_THRESHOLD, max(threshold, GREY_ZONE_FLOOR))


@dataclass(frozen=True, slots=True)
class Candidate:
    i: int  # index of the first image (smaller than j)
    j: int
    variant: int  # which rotation/mirror of image i matches image j best
    phash: int  # pHash distance at that variant
    dhash: int
    by_time: bool = False  # taken seconds apart: judged on the sketches


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


def scene_candidates(
    hashes: Sequence[PerceptualHash],
    window: int = SCENE_WINDOW,
    neighbours: int = SCENE_NEIGHBOURS,
) -> list[Candidate]:
    """Pairs (i < j) of images with a sketch whose capture times are at most ``window`` seconds
    apart, each paired with at most ``neighbours`` later shots (a long burst stays linear).

    Compared as is (variant 0): EXIF orientation is already applied to both."""
    timed = sorted(
        (h.taken, idx) for idx, h in enumerate(hashes) if h.taken is not None and h.sketch
    )
    out: list[Candidate] = []
    for a, (t, i) in enumerate(timed):
        for u, j in timed[a + 1 : a + 1 + neighbours]:
            if u - t > window:
                break
            lo, hi = min(i, j), max(i, j)
            p = (hashes[lo].phash ^ hashes[hi].phash).bit_count()
            d = (hashes[lo].dhash ^ hashes[hi].dhash).bit_count()
            out.append(Candidate(lo, hi, 0, p, d, by_time=True))
    return sorted(out, key=lambda c: (c.i, c.j))


def merge_candidates(by_hash: Sequence[Candidate], by_time: Sequence[Candidate]) -> list[Candidate]:
    """One candidate per pair. A pair found both ways keeps the hash candidate's variant and
    distances, and is judged as taken seconds apart."""
    pairs = {(c.i, c.j): c for c in by_time}
    for c in by_hash:
        pairs[(c.i, c.j)] = replace(c, by_time=(c.i, c.j) in pairs)
    return [pairs[k] for k in sorted(pairs)]


# -- verification ---------------------------------------------------------------------------


def _box_mean(a: npt.NDArray[np.float64], win: int) -> npt.NDArray[np.float64]:
    """Mean over every win x win window of each image in a stack of shape (k, h, w)."""
    c = np.zeros((a.shape[0], a.shape[1] + 1, a.shape[2] + 1))
    np.cumsum(np.cumsum(a, axis=1), axis=2, out=c[:, 1:, 1:])
    total = c[:, win:, win:] - c[:, :-win, win:] - c[:, win:, :-win] + c[:, :-win, :-win]
    return total / (win * win)


def ssim(a: Gray, b: Gray, win: int = SSIM_WINDOW) -> float:
    """Mean structural similarity of two equally sized grayscale images (1.0 = identical)."""
    x, y = a.astype(np.float64), b.astype(np.float64)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    ux, uy, xx, yy, xy = _box_mean(np.stack((x, y, x * x, y * y, x * y)), win)
    norm = win * win / (win * win - 1)  # sample covariance, as in the reference implementation
    vx = norm * (xx - ux * ux)
    vy = norm * (yy - uy * uy)
    vxy = norm * (xy - ux * uy)
    score = ((2 * ux * uy + c1) * (2 * vxy + c2)) / ((ux * ux + uy * uy + c1) * (vx + vy + c2))
    return float(score.mean())


def _overlap(
    a: npt.NDArray[np.float64], b: npt.NDArray[np.float64], dx: int, dy: int
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """The parts of a and b that cover the same scene when a[r + dy, c + dx] matches b[r, c]."""
    h, w = a.shape
    rows_a, cols_a = slice(max(0, dy), h + min(0, dy)), slice(max(0, dx), w + min(0, dx))
    rows_b, cols_b = slice(max(0, -dy), h + min(0, -dy)), slice(max(0, -dx), w + min(0, -dx))
    return a[rows_a, cols_a], b[rows_b, cols_b]


@functools.cache
def _hann(shape: tuple[int, ...]) -> npt.NDArray[np.float64]:
    return np.outer(np.hanning(shape[0]), np.hanning(shape[1]))


def shifts(
    a: npt.NDArray[np.float64], b: npt.NDArray[np.float64], peaks: int = SHIFT_PEAKS
) -> list[tuple[int, int]]:
    """The likeliest (dx, dy) that align a onto b, best first: the highest peaks of the windowed
    phase correlation within MAX_SHIFT of the side. Phase correlation ignores brightness and
    contrast, so a sky-to-ground gradient cannot pull the estimate the way plain
    cross-correlation does."""
    window = _hann(a.shape)
    reach = round(MAX_SHIFT * a.shape[0])
    fa = np.fft.rfft2((a - a.mean()) * window)
    fb = np.fft.rfft2((b - b.mean()) * window)
    cross = fa * np.conj(fb)
    corr = np.fft.irfft2(cross / (np.abs(cross) + 1e-9), a.shape)
    side = 2 * reach + 1
    near = np.roll(corr, (reach, reach), axis=(0, 1))[:side, :side]  # dy, dx in +-reach
    best = np.argsort(near.ravel(), kind="stable")[::-1][:peaks]
    return [(int(k % side) - reach, int(k // side) - reach) for k in best]


def aligned_ssim(
    a: npt.NDArray[np.uint8], b: npt.NDArray[np.uint8], bar: float = SSIM_MIN
) -> float:
    """SSIM of the overlapping parts after the likeliest small shift.

    Most pairs are unrelated and score far below SSIM_MIN at the best peak, so only a near miss
    (a sub-pixel shift, a second peak, below ``bar``) pays for the other peaks and the unshifted
    SSIM; then the result is never below plain SSIM."""
    x, y = a.astype(np.float64), b.astype(np.float64)
    best, *others = shifts(x, y)
    score = ssim(*_overlap(x, y, *best))
    if SHIFT_RETRY <= score < bar:
        for dx, dy in {(0, 0), *others} - {best}:
            score = max(score, ssim(*_overlap(x, y, dx, dy)))
    return score


def verify(
    cand: Candidate, threshold: int, thumbnails: Thumbnails, sketches: Thumbnails | None = None
) -> Accepted | None:
    """Accept a candidate, or None. Within the threshold on both hashes: accepted without
    SSIM. Otherwise the images, turned by the pair's variant and shifted into line, must reach
    SCENE_SSIM_MIN on their sketches (taken seconds apart) or SSIM_MIN on the working copies."""
    if cand.phash <= threshold and cand.dhash <= threshold:
        similarity = 1 - cand.phash / HASH_BITS
        return Accepted(cand.i, cand.j, cand.variant, cand.phash, similarity)
    source, bar = (sketches, SCENE_SSIM_MIN) if cand.by_time else (thumbnails, SSIM_MIN)
    if source is None:
        return None
    first, second = source(cand.i), source(cand.j)
    if first is None or second is None:  # an image became unreadable: cannot confirm
        return None
    score = aligned_ssim(variant(first, cand.variant), second, bar)
    if score < bar:
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
