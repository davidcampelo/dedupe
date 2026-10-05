"""Command-line interface (never deletes anything)."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from dedupe import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dedupe", description="Find duplicate and hidden files.")
    parser.add_argument("--version", action="version", version=f"dedupe {__version__}")
    parser.add_subparsers(dest="command")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
    return 0
