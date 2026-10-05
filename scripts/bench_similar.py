#!/usr/bin/env python
"""Benchmark the similar-images pass on generated images (needs the `similar` extra).

Builds N smooth "photos" (a quarter of them with resized / recompressed near-copies), then reports
the first scan, the cached rescan, the candidate search and the verify step, plus the longest
stall of a 10 ms heartbeat thread while hashing runs (the GIL pressure a GUI would feel; the
GUI-stall gate is 100 ms).  `--synthetic N` times only the candidate search on N random hashes.

Usage: python scripts/bench_similar.py [--images N] [--synthetic N] [--keep]
"""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image

from dedupe.core import similarity
from dedupe.core.cache import HashCache
from dedupe.core.models import CancelToken, ScanOptions
from dedupe.core.perceptual import VARIANTS, PerceptualHash
from dedupe.core.pipeline import run_scan


def build_images(root: Path, n: int) -> None:
    rng = np.random.default_rng(42)
    root.mkdir(parents=True)
    originals = n - n // 4
    for i in range(originals):
        coarse = rng.integers(0, 256, (9, 12, 3), dtype=np.uint8)
        img = Image.fromarray(coarse).resize((320, 240), Image.Resampling.BICUBIC)
        folder = root / f"d{i % 100:02d}"
        folder.mkdir(exist_ok=True)
        img.save(folder / f"p{i}.jpg", quality=90)
        if i < n // 4:  # a near-copy: smaller and more compressed
            small = img.resize((200, 150), Image.Resampling.LANCZOS)
            small.save(folder / f"p{i}-small.jpg", quality=55)


class Heartbeat:
    """Longest time a sleeping thread took to wake: how long other threads held the GIL."""

    def __init__(self) -> None:
        self.worst = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            start = time.perf_counter()
            time.sleep(0.01)
            self.worst = max(self.worst, time.perf_counter() - start - 0.01)

    def __enter__(self) -> Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()


def timed_scan(label: str, root: Path, options: ScanOptions, cache: HashCache) -> None:
    start = time.perf_counter()
    with Heartbeat() as beat:
        result = run_scan(root, options, store=cache)
    elapsed = time.perf_counter() - start
    print(
        f"{label:<8} {elapsed:7.1f}s  files={result.files_scanned} "
        f"similar_groups={len(result.similar_groups)}  longest stall {beat.worst * 1000:.0f} ms"
    )


def synthetic_candidates(n: int) -> None:
    rng = np.random.default_rng(1)
    raw = rng.integers(0, 1 << 63, size=(n, 2), dtype=np.int64)
    hashes = [PerceptualHash(int(p), int(d), ((int(p), int(d)),) * VARIANTS, 1, 1) for p, d in raw]
    start = time.perf_counter()
    found = similarity.find_candidates(hashes, 12, CancelToken())
    print(
        f"candidates for {n} random hashes: {time.perf_counter() - start:.1f}s ({len(found)} pairs)"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", type=int, default=10_000)
    ap.add_argument("--synthetic", type=int, default=0, help="only time the candidate search")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    if args.synthetic:
        synthetic_candidates(args.synthetic)
        return

    base = Path(tempfile.mkdtemp(prefix="dedupe-bench-similar-"))
    os.environ["XDG_CACHE_HOME"] = str(base / "cache")
    try:
        start = time.perf_counter()
        build_images(base / "pics", args.images)
        print(f"built {args.images} images in {time.perf_counter() - start:.1f}s")
        options = ScanOptions(exclude=(), similar_images=True)
        for label in ("first", "rescan"):
            cache = HashCache()
            timed_scan(label, base / "pics", options, cache)
            cache.close()
        plain = ScanOptions(exclude=())
        cache = HashCache()
        timed_scan("no-sim", base / "pics", plain, cache)
        cache.close()
    finally:
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
