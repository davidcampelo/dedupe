"""Command-line interface. It never deletes anything."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from dedupe import __version__
from dedupe.core.cache import HashCache
from dedupe.core.models import CancelToken, Progress, ScanOptions, ScanResult
from dedupe.core.pipeline import run_scan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dedupe", description="Find duplicate and hidden files.")
    parser.add_argument("--version", action="version", version=f"dedupe {__version__}")
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", help="find duplicate files (never deletes)")
    scan.add_argument("path", type=Path)
    scan.add_argument("--min-size", type=int, default=1, metavar="N", help="bytes (default 1)")
    scan.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    scan.add_argument("--json", action="store_true", help="machine-readable output")
    scan.add_argument("--paranoid", action="store_true", help="byte-compare within each group")
    scan.add_argument("--follow-symlinks", action="store_true")
    scan.add_argument("--cross-filesystems", action="store_true")
    scan.add_argument("--no-cache", action="store_true", help="do not read or write the hash cache")
    scan.add_argument("--no-hidden", action="store_true", help="skip hidden files")
    cache = sub.add_parser("cache", help="manage the hash cache")
    cache_sub = cache.add_subparsers(dest="cache_command", required=True)
    cache_sub.add_parser("clear", help="forget every cached hash")
    return parser


def human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError  # pragma: no cover


def result_to_json(result: ScanResult) -> dict[str, Any]:
    return {
        "root": str(result.root),
        "files_scanned": result.files_scanned,
        "reclaimable": result.reclaimable,
        "cancelled": result.cancelled,
        "groups": [
            {
                "hash": g.hash,
                "size": g.size,
                "reclaimable": g.reclaimable,
                "files": [
                    {"path": str(f.path), "size": f.size, "mtime_ns": f.mtime_ns} for f in g.files
                ],
                "hardlinked": [str(p) for p in g.hardlinked],
            }
            for g in result.groups
        ],
        "empty_files": [str(e.path) for e in result.empty_files],
        "hardlink_sets": [[str(p) for p in s] for s in result.hardlink_sets],
        "skipped": [{"path": s.path, "reason": s.reason} for s in result.skipped],
    }


def print_table(result: ScanResult, out: Any = None) -> None:
    out = out or sys.stdout
    print(
        f"{result.files_scanned} files scanned, {len(result.groups)} duplicate groups, "
        f"{human(result.reclaimable)} reclaimable",
        file=out,
    )
    for g in result.groups:
        print(
            f"\n{g.hash[:12]}  {human(g.size)} x {len(g.files)}  ({human(g.reclaimable)})", file=out
        )
        for f in g.files:
            print(f"  {f.path}", file=out)
        for p in g.hardlinked:
            print(f"  {p}  (hard link)", file=out)
    if result.empty_files:
        print(f"\n{len(result.empty_files)} empty files", file=out)
    if result.hardlink_sets:
        print(f"{len(result.hardlink_sets)} sets of already-hard-linked paths", file=out)
    if result.skipped:
        print(f"\n{len(result.skipped)} skipped:", file=out)
        for s in result.skipped:
            print(f"  {s.path}: {s.reason}", file=out)


def _cmd_scan(args: argparse.Namespace) -> int:
    root: Path = args.path
    if not root.is_dir():
        print(f"dedupe: error: not a directory: {root}", file=sys.stderr)
        return 2
    base = ScanOptions()
    options = replace(
        base,
        min_size=args.min_size,
        exclude=base.exclude + tuple(args.exclude),
        paranoid=args.paranoid,
        follow_symlinks=args.follow_symlinks,
        cross_filesystems=args.cross_filesystems,
        include_hidden=not args.no_hidden,
        use_cache=not args.no_cache,
    )
    cancel = CancelToken()
    previous = signal.signal(signal.SIGINT, lambda *_: cancel.cancel())

    def on_progress(p: Progress) -> None:
        if sys.stderr.isatty():
            print(f"\r{p.stage.value}: {p.done}/{p.total}  ", end="", file=sys.stderr, flush=True)

    cache = HashCache() if options.use_cache else None
    try:
        result = run_scan(root, options, on_progress, cancel, cache)
    finally:
        if cache is not None:
            cache.close()
        signal.signal(signal.SIGINT, previous)
        if sys.stderr.isatty():
            print("\r" + " " * 40 + "\r", end="", file=sys.stderr)
    for warning in result.warnings:
        print(f"dedupe: warning: {warning}", file=sys.stderr)
    if result.cancelled:
        print("dedupe: cancelled", file=sys.stderr)
        return 130
    if args.json:
        json.dump(result_to_json(result), sys.stdout, indent=2)
        print()
    else:
        print_table(result)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            return _cmd_scan(args)
        if args.command == "cache":
            cache = HashCache()
            try:
                print(f"cleared {cache.clear()} cached hashes")
            finally:
                cache.close()
            return 0
        parser.print_help()
    except BrokenPipeError:  # e.g. `dedupe scan . | head`
        sys.stderr.close()
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
