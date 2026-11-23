"""`p2p demo [--live]`. Replay is the default; `--live` switches to Bedrock."""

from __future__ import annotations

import argparse
import sys

from p2p.core.errors import P2PError
from p2p.demo.story import run_demo


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register `p2p demo` on the root parser."""
    demo = subparsers.add_parser(
        "demo",
        help="Tell the whole story on recorded replies. --live switches to Bedrock.",
    )
    demo.add_argument(
        "--live",
        action="store_true",
        help="Call Bedrock instead of replaying committed recordings.",
    )
    demo.set_defaults(handler=_handle_demo)


def _handle_demo(args: argparse.Namespace) -> int:
    try:
        return run_demo(live=args.live)
    except P2PError as failure:
        print(f"{failure.category.value}: {failure}", file=sys.stderr)
        return 1
