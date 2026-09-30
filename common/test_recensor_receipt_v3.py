"""The v3 Recensor receipt: a page-read run's units and configured witnesses, beside v2."""

from __future__ import annotations

import copy

import pytest

from common.contracts.canonical import self_hash
from common.contracts.errors import SchemaRefusal
from common.contracts.outcomes import OutcomeClass, classify
from common.contracts.stages import ATTESTATORES, RECENSOR
from common.recensor_receipt import (
    EMPTY_READING_DENOMINATOR_REASON,
    RECENSOR_PARTITION_RECEIPT_SCHEMA_V3,
    RECENSOR_READING_RECEIPT_SCOPE,
    build_recensor_reading_receipt,
    validate_recensor_partition_receipt,
)

DIGEST = "a" * 64


def _ref(name: str) -> dict[str, str]:
    return {"relative_path": f"r/4_perlector/artifacts/page-reading/{name}.json", "sha256": DIGEST}


def _item(
    act_id: str,
    *,
    disposition: str = "read",
    outcome: str = "accepted",
    reads: int = 3,
    truncated: int = 0,
):
    by_outcome = {"read": reads, "failed": 3 - reads}
    by_class = {klass.value: 0 for klass in OutcomeClass}
    for name, count in by_outcome.items():
        by_class[classify(ATTESTATORES, name).value] += count
    return {
        "act_id": act_id,
        "act_key": "p1:1",
        "page_disposition": disposition,
        "review_ref": {"relative_path": f"r/5_recensor/{act_id}.json", "sha256": DIGEST},
        "review_outcome": outcome,
        "partition_class": classify(RECENSOR, outcome).value,
        "coverage": {
            "configured": 3,
            "floor": 3,
            "by_outcome": by_outcome,
            "by_class": by_class,
            "under_witnessed": reads - truncated < 3,
            "unresolved_chairs": 0,
            "health_unrecorded": 0,
            "shortfalls": {"failed": 3 - reads, "truncated": truncated, "unaligned": 0},
        },
    }


def _link(left: int, outcome: str = "accepted") -> dict:
    subject = f"page-break:{left}:{left + 1}"
    return {
        "subject_id": subject,
        "link_ref": {"relative_path": f"r/5_recensor/{subject}.json", "sha256": DIGEST},
        "outcome": outcome,
    }


def _receipt(items, links=()):
    return build_recensor_reading_receipt(
        run_id="r",
        config_digest=DIGEST,
        page_reading_refs=[_ref("p1"), _ref("p2")],
        items=items,
        continuation_links=list(links),
    )


def test_a_v3_receipt_names_its_page_readings_and_each_units_page_disposition():
    receipt = _receipt([_item("act_b"), _item("act_a", disposition="held")])
    assert receipt["schema"] == RECENSOR_PARTITION_RECEIPT_SCHEMA_V3
    assert receipt["scope"] == RECENSOR_READING_RECEIPT_SCOPE
    assert [ref["relative_path"][-7:] for ref in receipt["page_reading_refs"]] == [
        "p1.json",
        "p2.json",
    ]
    assert [item["act_id"] for item in receipt["items"]] == ["act_a", "act_b"]
    assert receipt["expected_unit_count"] == 2 and "proposal_seal_ref" not in receipt
    assert "expected_act_count" not in receipt and receipt["continuation_links"] == []
    assert receipt["recensor_status"] == "complete"


def test_a_v3_receipt_judges_the_witness_floor_on_page_reads():
    receipt = _receipt([_item("act_a", reads=2)])
    assert receipt["recensor_status"] == "partial"
    assert receipt["reasons"] == ["unit act_a is under-witnessed (2 page reads of a floor of 3)"]


def test_a_truncated_page_reading_is_not_counted_toward_the_floor():
    receipt = _receipt([_item("act_a", truncated=1)])
    assert receipt["reasons"] == ["unit act_a is under-witnessed (2 page reads of a floor of 3)"]
    forged = copy.deepcopy(receipt)
    forged["items"][0]["coverage"]["under_witnessed"] = False
    forged["reasons"] = []
    forged["recensor_status"] = "complete"
    forged["self_hash"] = self_hash({k: v for k, v in forged.items() if k != "self_hash"})
    with pytest.raises(SchemaRefusal, match="under_witnessed"):
        validate_recensor_partition_receipt(forged)


def test_a_one_sided_page_break_keeps_the_run_partial():
    receipt = _receipt([_item("act_a")], [_link(2, "held-for-review"), _link(1)])
    assert [link["subject_id"] for link in receipt["continuation_links"]] == [
        "page-break:1:2",
        "page-break:2:3",
    ]
    assert receipt["recensor_status"] == "partial"
    assert receipt["reasons"] == [
        "page-break:2:3 is held-for-review: only one side says the text runs across the page break"
    ]
    assert _receipt([_item("act_a")], [_link(1)])["recensor_status"] == "complete"


def test_an_empty_v3_receipt_is_partial_by_name():
    receipt = _receipt([])
    assert receipt["reasons"] == [EMPTY_READING_DENOMINATOR_REASON]


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r["items"][0].update(page_disposition="maybe"),
        lambda r: r["items"][0].update(designator_outcome="proposed"),
        lambda r: r["items"][0]["coverage"].update(page_granularity_only=0),
        lambda r: r.update(page_reading_refs=[]),
        lambda r: r.update(page_reading_refs=[_ref("p1"), _ref("p1")]),
        lambda r: r.update(proposal_seal_ref=_ref("seal")),
        lambda r: r.update(scope="proposal-acts-and-configured-witnesses"),
        lambda r: r.update(expected_act_count=r.pop("expected_unit_count")),
        lambda r: r.update(expected_unit_count=2),
        lambda r: r.pop("continuation_links"),
        lambda r: r.update(continuation_links=[_link(1), _link(1)]),
        lambda r: r.update(continuation_links=[{**_link(1), "subject_id": "page-break:1:3"}]),
        lambda r: r.update(continuation_links=[_link(1, "joined")]),
        lambda r: r.update(continuation_links=[_link(1, "held-for-review")]),
    ],
    ids=[
        "disposition",
        "designator-outcome",
        "act-granularity",
        "no-page",
        "page-twice",
        "proposal-seal",
        "scope",
        "act-count",
        "unit-count",
        "no-links",
        "link-twice",
        "link-not-a-break",
        "link-outcome",
        "link-reason-unstated",
    ],
)
def test_a_malformed_v3_receipt_is_refused(change):
    forged = copy.deepcopy(_receipt([_item("act_a")]))
    change(forged)
    forged["self_hash"] = self_hash({k: v for k, v in forged.items() if k != "self_hash"})
    with pytest.raises(SchemaRefusal):
        validate_recensor_partition_receipt(forged)
