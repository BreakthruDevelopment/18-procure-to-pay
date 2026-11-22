"""Command-line entry point. Subcommands arrive in later slices; for now `--version`."""

import argparse
from collections.abc import Sequence

from p2p import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="p2p", description="Procure-to-pay.")
    parser.add_argument("--version", action="version", version=f"p2p {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    print(f"p2p {__version__}: no command given; see --help")
    return 0
