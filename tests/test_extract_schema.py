"""InvoiceDraft bounds, totals, and the three committed few-shot examples."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from p2p.extract.prompt import EXAMPLE_NAMES, example_text
from p2p.extract.schema import InvoiceDraft

_EXAMPLES = Path(__file__).resolve().parent.parent / "p2p" / "extract" / "examples"


def _draft(**fields: object) -> InvoiceDraft:
    payload: dict[str, object] = {
        "supplier_name": "Northwind Fasteners",
        "po_ref": "PO-1001",
        "number": "1001-A",
        "date": "2026-11-08",
        "currency": "USD",
        "total": 125000,
        "lines": [
            {
                "line_no": 1,
                "description": "M8 hex bolts, zinc",
                "qty": 100,
                "unit_price": 1250,
                "tax": 0,
            }
        ],
        "confidence": {
            "supplier_name": 0.99,
            "po_ref": 0.99,
            "number": 0.99,
            "date": 0.99,
            "currency": 0.99,
            "total": 0.99,
            "lines": 0.99,
        },
        "unreadable": [],
    }
    payload.update(fields)
    return InvoiceDraft.model_validate(payload)


def test_a_clean_draft_validates() -> None:
    draft = _draft()
    assert draft.total == 125000
    assert draft.confidence.overall() == pytest.approx(0.99)
    assert draft.unreadable == []


def test_lines_that_do_not_sum_to_total_fail_validation() -> None:
    with pytest.raises(ValidationError, match="lines do not sum to the stated total"):
        _draft(total=124000)


def test_a_credit_line_reconciles_when_the_total_nets() -> None:
    draft = _draft(
        total=485000,
        lines=[
            {
                "line_no": 1,
                "description": "Gearbox assembly, 4:1",
                "qty": 10,
                "unit_price": 50000,
                "tax": 0,
            },
            {
                "line_no": 2,
                "description": "Credit: freight overcharge",
                "qty": 1,
                "unit_price": -15000,
                "tax": 0,
            },
        ],
    )
    assert draft.lines[1].extension == -15000
    assert draft.total == 485000


def test_an_empty_po_ref_is_missing() -> None:
    assert _draft(po_ref="").po_ref is None
    assert _draft(po_ref="   ").po_ref is None


def test_a_bad_date_or_currency_is_refused() -> None:
    with pytest.raises(ValidationError, match="YYYY-MM-DD"):
        _draft(date="11/08/2026")
    with pytest.raises(ValidationError, match="three-letter code"):
        _draft(currency="U$D")


def test_confidence_is_bounded() -> None:
    with pytest.raises(ValidationError):
        _draft(
            confidence={
                "supplier_name": 1.2,
                "po_ref": 0.9,
                "number": 0.9,
                "date": 0.9,
                "currency": 0.9,
                "total": 0.9,
                "lines": 0.9,
            }
        )


def test_extra_fields_are_forbidden() -> None:
    payload = _draft().model_dump()
    payload["approve"] = True
    with pytest.raises(ValidationError):
        InvoiceDraft.model_validate(payload)


def test_few_shot_examples_are_files_not_inline_strings() -> None:
    for name in EXAMPLE_NAMES:
        assert (_EXAMPLES / f"{name}.txt").is_file()
        assert (_EXAMPLES / f"{name}.json").is_file()
        InvoiceDraft.model_validate_json(example_text(name, "json"))


def test_the_missing_po_example_has_no_po_ref() -> None:
    draft = InvoiceDraft.model_validate_json(example_text("missing-po", "json"))
    assert draft.po_ref is None
    assert "purchase order is referenced" in example_text("missing-po", "txt")


def test_the_credit_line_example_has_a_negative_line() -> None:
    draft = InvoiceDraft.model_validate_json(example_text("credit-line", "json"))
    assert any(line.extension < 0 for line in draft.lines)
    assert draft.total == sum(line.extension for line in draft.lines)
