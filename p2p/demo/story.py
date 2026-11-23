"""The demo narrative: ingest, repair, match, review, approve, batch, dispute, escalation.

Runs on a database in a fresh `mkdtemp()` directory and ignores the configured
path. The directory is removed when the run finishes.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

from agent_container.core.provider import ModelProvider

from p2p.batch.run import run_batch, seeded_folder
from p2p.batch.summary import format_summary
from p2p.core.access import Principal, Role
from p2p.core.erp import Decision, Erp
from p2p.core.errors import InternalFailure, P2PError
from p2p.demo.config import Config
from p2p.demo.fixtures import (
    demo_fixture,
    extract_example,
    extract_recording,
    match_fixture,
    seeded_document,
)
from p2p.demo.provider import live_provider, replay_from
from p2p.disputes.commands import format_step, run_step_with
from p2p.extract.ingest import format_outcome, run_ingest
from p2p.match.commands import format_apply, format_match, format_review, run_match

CLERK = Principal("clerk@demo", Role.CLERK, "1000")
APPROVER = Principal("approver@demo", Role.APPROVER, "1000")
DISPUTES = Principal("disputes@demo", Role.DISPUTES, "1000")
CASE_ID = "DC-0001"


class _Sources:
    """One provider per scene on replay; one shared Bedrock provider on `--live`."""

    def __init__(self, live: ModelProvider | None) -> None:
        self._live = live

    def _load(self, path: Path) -> ModelProvider:
        if self._live is not None:
            return self._live
        return replay_from(path)

    def ingest_repair(self) -> ModelProvider:
        return self._load(extract_recording("repaired.json"))

    def match_clean(self) -> ModelProvider:
        return self._load(match_fixture("clean.json"))

    def ingest_quantity(self) -> ModelProvider:
        return self._load(demo_fixture("extract-quantity.json"))

    def match_quantity(self) -> ModelProvider:
        return self._load(match_fixture("quantity.json"))

    def batch(self) -> ModelProvider:
        return self._load(demo_fixture("batch.json"))

    def dispute(self) -> ModelProvider:
        return self._load(demo_fixture("dispute.json"))


def run_demo(*, live: bool = False, env: Mapping[str, str] | None = None) -> int:
    """Tell the whole story. Always builds a private database; never touches config."""
    config = Config.from_env(env)
    sources = _Sources(live_provider(config) if live else None)
    home = tempfile.mkdtemp(prefix="p2p-demo-")
    try:
        database = Path(home) / "erp.sqlite3"
        with Erp.open(database) as erp:
            _tell(erp, sources, sys.stdout)
    except P2PError:
        raise
    finally:
        shutil.rmtree(home)
    mode = "live" if live else "replay, no network"
    print(f"demo ok ({mode})")
    return 0


def _tell(erp: Erp, sources: _Sources, out: TextIO) -> None:
    _section(out, "ingest")
    repaired = run_ingest(
        erp,
        CLERK,
        extract_example("clean.txt").read_text(encoding="utf-8"),
        sources.ingest_repair(),
    )
    print(format_outcome(repaired), file=out)

    _section(out, "match")
    matched = run_match(erp, CLERK, "INV-1001", sources.match_clean())
    print(format_match(matched), file=out)
    if matched.change_key is None:
        raise InternalFailure("the clean match did not stage a posting")

    _section(out, "review")
    print(format_review(erp.get_staged(APPROVER, matched.change_key)), file=out)

    _section(out, "approve")
    applied = erp.apply_change(APPROVER, matched.change_key, Decision.applied())
    print(format_apply(applied), file=out)

    _section(out, "variance")
    run_ingest(
        erp,
        CLERK,
        seeded_document("02-1002-A.txt").read_text(encoding="utf-8"),
        sources.ingest_quantity(),
    )
    variance = run_match(erp, CLERK, "INV-1002", sources.match_quantity())
    print(format_match(variance), file=out)

    _section(out, "batch")
    summary = run_batch(erp, CLERK, seeded_folder(), sources.batch())
    print(format_summary(summary), file=out)

    _section(out, "dispute")
    dispute = sources.dispute()
    print(format_step(run_step_with(erp, DISPUTES, CASE_ID, dispute)), file=out)
    print(format_step(run_step_with(erp, DISPUTES, CASE_ID, dispute)), file=out)
    print(format_step(run_step_with(erp, DISPUTES, CASE_ID, dispute)), file=out)

    _section(out, "escalation")
    print(format_step(run_step_with(erp, DISPUTES, CASE_ID, dispute)), file=out)


def _section(out: TextIO, name: str) -> None:
    print(f"== {name} ==", file=out)
