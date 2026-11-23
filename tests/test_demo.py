"""`p2p demo`, recorded fixtures, and the configured-database isolation rule."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from p2p.cli import main
from p2p.core.access import Principal, Role
from p2p.core.erp import Erp
from p2p.core.errors import ValidationFailed
from p2p.demo.dump import record_replies
from p2p.demo.fixtures import demo_fixture
from p2p.demo.health import handler, health_payload
from p2p.demo.redact import redact_recorded
from p2p.demo.replies import batch_replies, dispute_replies, health_reply, quantity_extract


def test_demo_prints_the_whole_story(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "== ingest ==" in out
    assert "repaired true" in out
    assert "invoice INV-1001" in out
    assert "verdict match" in out
    assert "pst_000001" in out
    assert "== review ==" in out
    assert "decision applied" in out
    assert "invoice_state posted" in out
    assert "verdict variance" in out
    assert "expected 38 actual 50" in out
    assert '"items": 12' in out
    assert '"ok": 9' in out
    assert '"failed": 1' in out
    assert '"skipped": 2' in out
    assert "escalate the credit exceeds 2,500" in out
    assert "crd_000001" in out
    assert "demo ok (replay, no network)" in out
    assert "AKIA" not in out


def test_two_demo_runs_print_the_same_story(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["demo"]) == 0
    first = capsys.readouterr().out
    assert main(["demo"]) == 0
    second = capsys.readouterr().out
    assert first == second


def test_demo_leaves_a_configured_database_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    configured = tmp_path / "real.sqlite3"
    with Erp.open(configured) as erp:
        clerk = Principal("clerk@demo", Role.CLERK, "1000")
        assert len(erp.list_open_invoices(clerk)) == 12
    before = configured.read_bytes()
    monkeypatch.setenv("P2P_DATABASE", str(configured))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    monkeypatch.chdir(tmp_path)
    stray = tmp_path / "erp.sqlite3"
    stray.write_bytes(b"NOT-A-DATABASE")

    assert main(["demo"]) == 0
    captured = capsys.readouterr()
    assert "demo ok (replay, no network)" in captured.out
    assert configured.read_bytes() == before
    assert stray.read_bytes() == b"NOT-A-DATABASE"
    leftovers = [
        path.name for path in (tmp_path / "tmp").iterdir() if path.name.startswith("p2p-demo-")
    ]
    assert leftovers == []


def test_live_without_a_profile_id_is_validation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("P2P_BEDROCK_PROFILE_ID", raising=False)
    assert main(["demo", "--live"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("validation:")
    assert "P2P_BEDROCK_PROFILE_ID" in err


def test_health_reports_version_and_a_passing_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_VERSION", raising=False)
    body = health_payload()
    assert body == {"status": "ok", "version": "0.1.0", "replay": "ok"}


def test_health_prefers_the_deployed_image_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_VERSION", "sha-deadbeef")
    body = health_payload()
    assert body["version"] == "sha-deadbeef"
    assert body["replay"] == "ok"


def test_health_opens_no_erp(monkeypatch: pytest.MonkeyPatch) -> None:
    from p2p.core.erp import Erp

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("health opened the ERP")

    monkeypatch.setattr(Erp, "open", boom)
    assert health_payload()["replay"] == "ok"


def test_health_reports_a_failed_round_trip_with_a_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure branch, which nothing exercised: `/health` is the whole live surface.

    Without this, the round-trip could be deleted outright — or its branch flipped to
    report `ok` — and the suite, lint and the post-deploy smoke would all still pass.
    """
    monkeypatch.delenv("APP_VERSION", raising=False)

    def boom() -> None:
        raise ValidationFailed("the health replay round-trip did not pass")

    monkeypatch.setattr("p2p.demo.health.replay_round_trip", boom)

    assert health_payload() == {"status": "error", "version": "0.1.0", "replay": "failed"}
    response = handler({}, None)
    assert response["statusCode"] == 500
    body = json.loads(str(response["body"]))
    assert body == {"status": "error", "version": "0.1.0", "replay": "failed"}


def test_health_answers_rather_than_raising_on_a_short_recording(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """App 17's replay errors are not `P2PError`, so this used to escape as a crash.

    A format-valid recording with no replies raises `ReplayExhausted`, which descends
    from `RuntimeError`. `/health` must still answer with its documented body.
    """
    empty = tmp_path / "health.json"
    empty.write_text('{"replies": []}\n', encoding="utf-8")

    def fixture(name: str) -> Path:
        del name
        return empty

    monkeypatch.setattr("p2p.demo.health.demo_fixture", fixture)

    body = health_payload()
    assert body["status"] == "error"
    assert body["replay"] == "failed"
    assert handler({}, None)["statusCode"] == 500


def test_lambda_handler_returns_the_health_json() -> None:
    response = handler({}, None)
    assert response["statusCode"] == 200
    body = json.loads(str(response["body"]))
    assert body["status"] == "ok"
    assert body["replay"] == "ok"
    assert "version" in body


def test_redact_strips_credential_shapes() -> None:
    access = "AKIA" + ("0" * 16)
    text = f"key {access} and aws_secret_access_key=placeholder-secret"
    cleaned = redact_recorded(text)
    assert access not in cleaned
    assert "placeholder-secret" not in cleaned
    assert "[redacted]" in cleaned


def test_committed_fixtures_match_the_recorder() -> None:
    cases = (
        ("health.json", (health_reply(),)),
        ("extract-quantity.json", (quantity_extract(),)),
        ("batch.json", batch_replies()),
        ("dispute.json", dispute_replies()),
    )
    for name, replies in cases:
        recorded = asyncio.run(record_replies(replies))
        on_disk = json.loads(demo_fixture(name).read_text(encoding="utf-8"))
        assert on_disk["replies"] == recorded["replies"]
