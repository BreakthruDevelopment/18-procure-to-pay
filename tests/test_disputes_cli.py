"""`p2p dispute open/step/show` on the command line."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from p2p.cli import main
from p2p.core.erp import Erp


@pytest.fixture
def built_db(db_path: Path) -> Path:
    with Erp.open(db_path):
        pass
    return db_path


def _flags(db_path: Path) -> list[str]:
    return [
        "--database",
        str(db_path),
        "--subject",
        "disputes@demo",
        "--role",
        "disputes",
        "--company-code",
        "1000",
    ]


def _replay(path: Path, **fields: object) -> Path:
    payload = {
        "next_action": "ask_question",
        "tool_choice": None,
        "draft_reply": "Need a fact before acting.",
        "question": "What quantity was received against this invoice?",
        **fields,
    }
    path.write_text(
        json.dumps(
            {
                "replies": [
                    {
                        "stop_reason": "tool_use",
                        "text": "",
                        "tool_uses": [{"id": "t1", "name": "dispute_step", "input": payload}],
                    }
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_dispute_show_prints_the_seeded_case(
    built_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["dispute", "show", *_flags(built_db), "DC-0001"])
    assert code == 0
    out = capsys.readouterr().out
    assert "id DC-0001" in out
    assert "state open" in out
    assert "supplier SUP-1003" in out
    assert "invoice INV-1013" in out


def test_dispute_open_then_show(built_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["dispute", "open", *_flags(built_db), "--supplier", "SUP-1003", "--invoice", "INV-1013"]
    )
    assert code == 0
    opened = capsys.readouterr().out
    assert opened.startswith("opened DC-0003")
    assert "state open" in opened
    code = main(["dispute", "show", *_flags(built_db), "DC-0003"])
    assert code == 0
    shown = capsys.readouterr().out
    assert "id DC-0003" in shown
    assert "1 opened" in shown


def test_dispute_step_prints_the_question(
    built_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    replay = _replay(tmp_path / "step.json")
    code = main(["dispute", "step", *_flags(built_db), "DC-0001", "--replay", str(replay)])
    assert code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == "case DC-0001"
    assert lines[1] == "state investigating"
    assert lines[2] == "action ask_question"
    assert lines[-1] == "What quantity was received against this invoice?"


def test_dispute_step_prints_the_change_key_last(
    built_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    replay = _replay(
        tmp_path / "credit.json",
        next_action="propose_credit",
        tool_choice="issue_credit_memo",
        draft_reply="Proposing the credit.",
        amount=400000,
        currency="USD",
        reason="supplier claim",
    )
    code = main(["dispute", "step", *_flags(built_db), "DC-0001", "--replay", str(replay)])
    assert code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[-1] == "crd_000001"
    assert "escalate the credit exceeds 2,500" in "\n".join(lines)


def test_a_clerk_cannot_open_a_case(built_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "dispute",
            "open",
            "--database",
            str(built_db),
            "--subject",
            "clerk@demo",
            "--role",
            "clerk",
            "--company-code",
            "1000",
            "--supplier",
            "SUP-1003",
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith("permission:")
    assert "SUP-1003" not in err


def test_another_agents_case_is_the_same_refusal_as_missing(
    built_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = main(["dispute", "show", *_flags(built_db), "DC-9999"])
    other = main(["dispute", "show", *_flags(built_db), "DC-0002"])
    assert missing == 1
    assert other == 1
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line]
    assert lines[0] == lines[1]
    assert "permission:" in lines[0]
    assert "DC-0002" not in err
    assert "DC-9999" not in err
