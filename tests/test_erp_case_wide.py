"""Two case-wide reads that no event window can answer.

`list_case_events` returns the newest rows and clamps `limit` to `MAX_PAGE`, so a caller that
derived these from events was right only while a case stayed under a hundred of them. Each test
here is written to fail if the accessor ever goes back to being window-bounded: the fixtures put
well over `MAX_PAGE` events between the fact and the question.
"""

from __future__ import annotations

import pytest

from p2p.core.access import Principal, Role
from p2p.core.erp import MAX_PAGE, Erp
from p2p.core.errors import AccessDenied

_WELL_PAST_THE_WINDOW = MAX_PAGE + 20


def _bury(erp: Erp, principal: Principal, case_id: str) -> None:
    """Enough unrelated events to push everything before them out of any page."""
    for n in range(_WELL_PAST_THE_WINDOW):
        erp.append_case_event(principal, case_id, kind="note", payload={"n": n})


def test_a_question_is_remembered_past_the_event_window(erp: Erp, disputes: Principal) -> None:
    assert erp.case_has_question(disputes, "DC-0001") is False
    erp.append_case_event(disputes, "DC-0001", kind="question", payload={"text": "how many?"})
    assert erp.case_has_question(disputes, "DC-0001") is True
    _bury(erp, disputes, "DC-0001")
    # The window cannot see the question any more; the case still can.
    window = erp.list_case_events(disputes, "DC-0001", limit=MAX_PAGE)
    assert all(event.kind != "question" for event in window)
    assert erp.case_has_question(disputes, "DC-0001") is True


def test_read_digests_are_remembered_past_the_event_window(erp: Erp, disputes: Principal) -> None:
    assert erp.case_read_digests(disputes, "DC-0001") == frozenset()
    erp.append_case_event(
        disputes, "DC-0001", kind="read", payload={"tool": "get_invoice", "digest": "abc123"}
    )
    erp.append_case_event(
        disputes, "DC-0001", kind="read", payload={"tool": "get_invoice", "digest": "abc123"}
    )
    erp.append_case_event(
        disputes, "DC-0001", kind="read", payload={"tool": "find_supplier", "digest": "def456"}
    )
    # Distinct, so the set is the size of what was learned, not of how often it was read.
    assert erp.case_read_digests(disputes, "DC-0001") == frozenset(
        {("get_invoice", "abc123"), ("find_supplier", "def456")}
    )
    _bury(erp, disputes, "DC-0001")
    window = erp.list_case_events(disputes, "DC-0001", limit=MAX_PAGE)
    assert all(event.kind != "read" for event in window)
    assert ("get_invoice", "abc123") in erp.case_read_digests(disputes, "DC-0001")


def test_a_read_row_missing_either_key_is_not_in_the_set(erp: Erp, disputes: Principal) -> None:
    """Two such rows would otherwise collide as one pair of nulls and hide a real read."""
    erp.append_case_event(disputes, "DC-0001", kind="read", payload={"tool": "get_invoice"})
    erp.append_case_event(disputes, "DC-0001", kind="read", payload={"digest": "abc123"})
    erp.append_case_event(disputes, "DC-0001", kind="read", payload={})
    assert erp.case_read_digests(disputes, "DC-0001") == frozenset()
    erp.append_case_event(
        disputes, "DC-0001", kind="read", payload={"tool": "get_invoice", "digest": "abc123"}
    )
    assert erp.case_read_digests(disputes, "DC-0001") == frozenset({("get_invoice", "abc123")})


def test_neither_accessor_is_reachable_by_a_clerk_or_a_service_principal(
    erp: Erp, clerk: Principal, service: Principal
) -> None:
    for principal in (clerk, service):
        with pytest.raises(AccessDenied):
            erp.case_has_question(principal, "DC-0001")
        with pytest.raises(AccessDenied):
            erp.case_read_digests(principal, "DC-0001")


def test_another_companys_case_is_refused_exactly_like_one_that_does_not_exist(
    erp: Erp, disputes: Principal
) -> None:
    """A different message for each would tell a disputes agent which case ids are real."""
    elsewhere = Principal("disputes@other", Role.DISPUTES, "2000")
    missing = "DC-9999"
    for accessor in (erp.case_has_question, erp.case_read_digests):
        with pytest.raises(AccessDenied) as absent:
            accessor(disputes, missing)
        with pytest.raises(AccessDenied) as foreign:
            accessor(elsewhere, "DC-0001")
        assert str(absent.value) == str(foreign.value)
        assert missing not in str(absent.value)
        assert "DC-0001" not in str(foreign.value)


def test_a_colleagues_case_is_refused_the_same_way(erp: Erp, disputes: Principal) -> None:
    """The assignee predicate is in the query, so an unassigned case is simply not visible."""
    colleague = Principal("other-disputes@demo", Role.DISPUTES, "1000")
    with pytest.raises(AccessDenied):
        erp.case_has_question(colleague, "DC-0001")
    with pytest.raises(AccessDenied):
        erp.case_read_digests(colleague, "DC-0001")


def test_an_approver_may_read_both_in_its_own_company_code(
    erp: Erp, disputes: Principal, approver: Principal
) -> None:
    erp.append_case_event(disputes, "DC-0001", kind="question", payload={"text": "how many?"})
    assert erp.case_has_question(approver, "DC-0001") is True
    assert erp.case_read_digests(approver, "DC-0001") == frozenset()


def test_neither_accessor_answers_from_another_case(erp: Erp, disputes: Principal) -> None:
    """`_case_row` authorizes the case that was named; the event query is then the only thing
    keeping another case's rows out of the answer, so `case_id = ?` is an access predicate and
    not SQL detail. Without it, a case that never asked a question reports that it did — the
    very defect these accessors exist to remove, arriving from the other side — and a digest
    set comes back full of pairs the case never read, including from a case the assignee
    predicate had just refused this principal.
    """
    other = erp.open_case(disputes, supplier_id="SUP-1006", invoice_id="INV-1011")
    erp.append_case_event(disputes, "DC-0001", kind="question", payload={"text": "how many?"})
    erp.append_case_event(
        disputes, "DC-0001", kind="read", payload={"tool": "get_invoice", "digest": "abc123"}
    )
    assert erp.case_has_question(disputes, other.id) is False
    assert erp.case_read_digests(disputes, other.id) == frozenset()
    assert erp.case_has_question(disputes, "DC-0001") is True
    assert erp.case_read_digests(disputes, "DC-0001") == frozenset({("get_invoice", "abc123")})


def test_a_non_read_event_carrying_the_same_keys_is_not_a_read(
    erp: Erp, disputes: Principal
) -> None:
    """`case_event.kind` is free text and the disputes desk writes eight of them, building its
    `step` payload beside its `read` payload. A future payload that gained `tool` and `digest`
    must not quietly join the digest set."""
    for kind in ("step", "note", "escalated", "refused", "staged", "question"):
        erp.append_case_event(
            disputes,
            "DC-0001",
            kind=kind,
            payload={"tool": "get_invoice", "digest": f"not-a-read-{kind}"},
        )
    assert erp.case_read_digests(disputes, "DC-0001") == frozenset()
    erp.append_case_event(
        disputes, "DC-0001", kind="read", payload={"tool": "get_invoice", "digest": "abc123"}
    )
    assert erp.case_read_digests(disputes, "DC-0001") == frozenset({("get_invoice", "abc123")})
