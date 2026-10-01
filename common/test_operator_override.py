"""An operator override releases a held reading only on current decisions the Recensor applied."""

from __future__ import annotations

import pytest

from common.contracts.errors import FatalAccounting
from common.page_review import (
    NOT_OVERRIDABLE,
    operator_override,
    override_refusal,
    reading_holds_allowed,
    require_establishable,
)

UNIT_BASIS = "a" * 64
PAGE_BASIS = "b" * 64


def _row(codes=("duplicate-region", "merged-detection"), unit_class="reading") -> dict:
    return {
        "act_id": "act-1",
        "act_key": "p1:1",
        "page_id": "page-1",
        "class": unit_class,
        "disposition": "held" if codes else "read",
        "perlectio_ref": {"relative_path": "4_perlector/x.json", "sha256": "c" * 64},
        "hold_codes": list(codes),
    }


def _summary(scope: str, subject: str, decision: str, basis: str, digest: str) -> dict:
    return {
        "scope": scope,
        "subject_id": subject,
        "decision": decision,
        "state": "current",
        "basis_digest": basis,
        "decision_hash": digest,
        "record_sha256": digest,
    }


RELEASE = _summary("unit", "act-1", "release", UNIT_BASIS, "1" * 64)
NO_MISSED = _summary("page", "page-1", "no-missed-act", PAGE_BASIS, "2" * 64)


def _review(cleared_unit, cleared_page, decisions) -> dict:
    return {
        "outcome": "accepted",
        "payload": {
            "release": None,
            "operator_review": {
                "basis_digest": UNIT_BASIS,
                "page_basis_digest": PAGE_BASIS,
                "cleared": {"unit": list(cleared_unit), "page": list(cleared_page)},
                "decisions": list(decisions),
            },
        },
    }


def test_current_applied_decisions_clearing_every_code_override_them():
    review = _review(["duplicate-region"], ["merged-detection"], [RELEASE, NO_MISSED])
    applied = {RELEASE["decision_hash"], NO_MISSED["decision_hash"]}
    override = require_establishable(_row(), review, applied)
    assert override["codes"] == ["duplicate-region", "merged-detection"]
    assert [d["decision"] for d in override["decisions"]] == ["release", "no-missed-act"]


def test_a_decision_the_review_decisions_record_did_not_apply_overrides_nothing():
    review = _review(["duplicate-region"], ["merged-detection"], [RELEASE, NO_MISSED])
    with pytest.raises(FatalAccounting, match="no current no-missed-act"):
        operator_override(_row(), review, {RELEASE["decision_hash"]})


def test_a_decision_bound_to_another_basis_overrides_nothing():
    stale = {**RELEASE, "basis_digest": "f" * 64}
    review = _review(["duplicate-region"], ["merged-detection"], [stale, NO_MISSED])
    with pytest.raises(FatalAccounting, match="no current release"):
        operator_override(_row(), review)


def test_a_review_that_does_not_clear_every_code_is_no_override_and_is_refused():
    review = _review(["duplicate-region"], [], [RELEASE])
    assert operator_override(_row(), review) is None
    with pytest.raises(FatalAccounting, match="may not resurrect a held reading"):
        require_establishable(_row(), review)


@pytest.mark.parametrize("code", sorted(NOT_OVERRIDABLE))
def test_a_reading_the_export_cannot_carry_is_never_overridden(code):
    row = _row((code,))
    assert override_refusal(row) is not None
    review = _review([code], [], [RELEASE])
    with pytest.raises(FatalAccounting, match="is refused"):
        operator_override(row, review)


def test_an_override_allows_exactly_the_reading_holds_it_cleared():
    override = {"codes": ["duplicate-region", "merged-detection"], "decisions": []}
    held = {
        "outcome": "held",
        "payload": {"holds": ["duplicate-region"], "page_holds": ["merged-detection"]},
    }
    assert reading_holds_allowed(held, override)
    assert not reading_holds_allowed(held, None)
    more = {"outcome": "held", "payload": {"holds": ["reading-incomplete"], "page_holds": []}}
    assert not reading_holds_allowed(more, override)
