#!/usr/bin/env python
"""Benchmark the scan pipeline on a synthetic tree (default 100,000 files).

Reports cold-cache and warm-cache scan times and how fast a cancel takes effect.
Usage: python scripts/bench.py [--files N] [--keep]
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import tempfile
import threading
import time
from pathlib import Path

from dedupe.core.cache import HashCache
from dedupe.core.models import CancelToken, ScanOptions
from dedupe.core.pipeline import run_scan


def build_tree(root: Path, n: int) -> None:
    rng = random.Random(42)
    blobs = [rng.randbytes(rng.choice((200, 2_000, 20_000, 200_000))) for _ in range(n // 4 + 1)]
    for i in range(n):
        d = root / f"d{i % 500:03d}" / f"s{i % 7}"
        if i < 500 * 7:
            d.mkdir(parents=True, exist_ok=True)
        # ~half the files are copies of an earlier blob, so there are many duplicate groups
        data = (
            blobs[rng.randrange(len(blobs))]
            if i % 2
            else blobs[i % len(blobs)] + i.to_bytes(8, "little")
        )
        (d / f"f{i}.bin").write_bytes(data)


def timed(label: str, **kw: object) -> float:
    start = time.perf_counter()
    result = run_scan(**kw)  # type: ignore[arg-type]
    elapsed = time.perf_counter() - start
    print(
        f"{label:<12} {elapsed:7.2f}s  files={result.files_scanned} "
        f"groups={len(result.groups)} reclaimable={result.reclaimable / 1e6:.1f} MB"
    )
    return elapsed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=100_000)
    ap.add_argument("--keep", action="store_true", help="keep the generated tree")
    args = ap.parse_args()

    base = Path(tempfile.mkdtemp(prefix="dedupe-bench-"))
    os.environ["XDG_CACHE_HOME"] = str(base / "cache")
    root = base / "tree"
    try:
        t = time.perf_counter()
        build_tree(root, args.files)
        print(f"built {args.files} files in {time.perf_counter() - t:.1f}s")
        options = ScanOptions(exclude=())

        cache = HashCache()
        timed("cold", root=root, options=options, store=cache)
        cache.close()
        cache = HashCache()
        timed("warm", root=root, options=options, store=cache)
        cache.close()

        # cancel latency: fire the token 1s into an uncached scan, measure time to return
        token = CancelToken()
        threading.Timer(1.0, token.cancel).start()
        start = time.perf_counter()
        result = run_scan(root, options, cancel=token)
        end = time.perf_counter()
        if result.cancelled:
            print(f"cancel       latency {end - start - 1.0:.3f}s after the token fired")
        else:
            print("cancel       scan finished before the token fired; use more files")
    finally:
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
