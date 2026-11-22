"""Command-line entry point. Subcommands arrive per slice; `--version` is always here."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from p2p import __version__
from p2p.disputes.commands import add_parser as add_dispute_parser
from p2p.extract.ingest import add_parser as add_ingest_parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="p2p", description="Procure-to-pay.")
    parser.add_argument("--version", action="version", version=f"p2p {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    # Both, and in one place: 18.4 and 18.6 each rewrote this function for their own
    # subcommand, so a resolution keeping either side alone silently deletes the other's.
    add_dispute_parser(subparsers)
    add_ingest_parser(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        print(f"p2p {__version__}: no command given; see --help")
        return 0
    result: int = handler(args)
    return result
