"""Command-line entry point. Subcommands arrive per slice; `--version` is always here."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from p2p import __version__
from p2p.batch.run import add_parser as add_batch_parser
from p2p.demo.command import add_parser as add_demo_parser
from p2p.disputes.commands import add_parser as add_dispute_parser
from p2p.extract.ingest import add_parser as add_ingest_parser
from p2p.match.commands import add_parser as add_match_parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="p2p", description="Procure-to-pay.")
    parser.add_argument("--version", action="version", version=f"p2p {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    # Every subcommand, and in one place: each slice rewrote this function for its own
    # command, so a resolution keeping one side alone silently deletes the others.
    add_batch_parser(subparsers)
    add_demo_parser(subparsers)
    add_dispute_parser(subparsers)
    add_ingest_parser(subparsers)
    add_match_parser(subparsers)
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
