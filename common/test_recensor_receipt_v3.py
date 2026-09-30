"""The v3 Recensor receipt: a page-read run's units and configured witnesses, beside v2."""

from __future__ import annotations

import copy

import pytest

from common.contracts.canonical import self_hash
from common.contracts.errors import SchemaRefusal
from common.contracts.outcomes import OutcomeClass, classify
from common.contracts.stages import ATTESTATORES, RECENSOR
from common.recensor_receipt import (
    RECENSOR_PARTITION_RECEIPT_SCHEMA_V3,
    RECENSOR_READING_RECEIPT_SCOPE,
    build_recensor_reading_receipt,
    validate_recensor_partition_receipt,
)

DIGEST = "a" * 64


def _ref(name: str) -> dict[str, str]:
    return {"relative_path": f"r/4_perlector/artifacts/page-reading/{name}.json", "sha256": DIGEST}


def _page(ordinal: int) -> dict:
    return {"page_ordinal": ordinal, "reading_ref": _ref(f"p{ordinal}")}


def _item(
    act_id: str,
    key: str,
    *,
    disposition: str = "read",
    outcome: str = "accepted",
    reads: int = 3,
    release: str | None = None,
    truncated: int = 0,
):
    by_outcome = {"read": reads, "failed": 3 - reads}
    by_class = {klass.value: 0 for klass in OutcomeClass}
    for name, count in by_outcome.items():
        by_class[classify(ATTESTATORES, name).value] += count
    return {
        "act_id": act_id,
        "act_key": key,
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
        "release_reason": release,
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
        page_reading_refs=[_page(1), _page(2)],
        items=items,
        continuation_links=list(links),
    )


TWO_READ = (("act_a", "p1:1"), ("act_b", "p2:1"))


def test_a_v3_receipt_names_its_page_readings_and_each_units_page_disposition():
    receipt = _receipt([_item("act_b", "p1:1"), _item("act_a", "p2:1")])
    assert receipt["schema"] == RECENSOR_PARTITION_RECEIPT_SCHEMA_V3
    assert receipt["scope"] == RECENSOR_READING_RECEIPT_SCOPE
    assert [
        (row["page_ordinal"], row["reading_ref"]["relative_path"][-7:])
        for row in receipt["page_reading_refs"]
    ] == [(1, "p1.json"), (2, "p2.json")]
    assert [item["act_id"] for item in receipt["items"]] == ["act_a", "act_b"]
    assert receipt["expected_unit_count"] == 2 and "proposal_seal_ref" not in receipt
    assert "expected_act_count" not in receipt and receipt["continuation_links"] == []
    assert receipt["recensor_status"] == "complete"


def test_a_v3_receipt_judges_the_witness_floor_on_page_reads():
    receipt = _receipt([_item("act_a", "p1:1", reads=2), _item("act_b", "p2:1")])
    assert receipt["recensor_status"] == "partial"
    assert receipt["reasons"] == ["unit act_a is under-witnessed (2 page reads of a floor of 3)"]


def test_a_truncated_page_reading_is_not_counted_toward_the_floor():
    receipt = _receipt([_item("act_a", "p1:1", truncated=1), _item("act_b", "p2:1")])
    assert receipt["reasons"] == ["unit act_a is under-witnessed (2 page reads of a floor of 3)"]
    forged = copy.deepcopy(receipt)
    forged["items"][0]["coverage"]["under_witnessed"] = False
    forged["reasons"] = []
    forged["recensor_status"] = "complete"
    forged["self_hash"] = self_hash({k: v for k, v in forged.items() if k != "self_hash"})
    with pytest.raises(SchemaRefusal, match="under_witnessed"):
        validate_recensor_partition_receipt(forged)


def test_a_one_sided_page_break_keeps_the_run_partial():
    items = [_item(*unit) for unit in TWO_READ]
    receipt = _receipt(items, [_link(2, "held-for-review"), _link(1)])
    assert [link["subject_id"] for link in receipt["continuation_links"]] == [
        "page-break:1:2",
        "page-break:2:3",
    ]
    assert receipt["recensor_status"] == "partial"
    assert receipt["reasons"] == [
        "page-break:2:3 is held-for-review: only one side says the text runs across the page break"
    ]
    assert _receipt(items, [_link(1)])["recensor_status"] == "complete"


def test_a_held_unit_its_review_released_is_resolved_and_the_receipt_can_be_complete():
    released = _item(
        "act_a", "p2:blank", disposition="held", outcome="confirmed-blank", release="blank paper"
    )
    receipt = _receipt([_item("act_b", "p1:1"), released])
    assert receipt["recensor_status"] == "complete"
    assert receipt["reasons"] == []


def test_a_held_unit_with_no_completed_review_keeps_the_receipt_partial():
    held = _item("act_c", "p2:1", disposition="held", outcome="held-for-review")
    receipt = _receipt([_item("act_b", "p1:1"), held])
    assert receipt["recensor_status"] == "partial"
    assert receipt["reasons"][0] == "unit act_c was held by its page reading and is not released"


@pytest.mark.parametrize(
    "item",
    [
        _item("act_a", "p2:1", disposition="held"),
        _item("act_a", "p2:1", disposition="held", release="  "),
        _item("act_a", "p2:1", release="nothing held it"),
        _item("act_a", "p2:1", disposition="held", outcome="held-for-review", release="x"),
    ],
    ids=["no-reason", "blank-reason", "read-unit-released", "unreleased-with-reason"],
)
def test_a_release_reason_is_given_exactly_when_a_held_unit_is_completed(item):
    with pytest.raises(SchemaRefusal, match="release"):
        _receipt([_item("act_b", "p1:1"), item])


def test_a_v3_receipt_with_a_page_without_a_unit_is_refused():
    for items in (
        [_item("act_a", "p1:1")],
        [_item("act_a", "p1:1"), _item("act_b", "p1:2")],
        [],
    ):
        with pytest.raises(SchemaRefusal, match="every sealed page is at least one unit"):
            _receipt(items)


def test_a_v3_receipt_with_a_unit_on_a_page_it_does_not_name_is_refused():
    items = [_item("act_a", "p1:1"), _item("act_b", "p2:1"), _item("act_c", "p3:1")]
    with pytest.raises(SchemaRefusal, match=r"units on page\(s\) \[3\], which name no sealed"):
        _receipt(items)


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        (lambda r: r["items"][0].update(page_disposition="maybe"), "names page_disposition"),
        (lambda r: r["items"][0].update(designator_outcome="proposed"), "wrong closed schema"),
        (lambda r: r["items"][0].pop("release_reason"), "wrong closed schema"),
        (
            lambda r: r["items"][0]["coverage"].update(page_granularity_only=0),
            "attaches no witness to an act",
        ),
        (lambda r: r.update(page_reading_refs=[]), "names no page reading"),
        (lambda r: r.update(page_reading_refs=[_page(1), _page(1)]), "strictly increasing"),
        (
            lambda r: r.update(
                page_reading_refs=[_page(1), {**_page(2), "reading_ref": _ref("p1")}]
            ),
            "names one page reading twice",
        ),
        (lambda r: r.update(page_reading_refs=[_page(2), _page(1)]), "strictly increasing"),
        (
            lambda r: r.update(page_reading_refs=[_ref("p1"), _ref("p2")]),
            "is not {page_ordinal, reading_ref}",
        ),
        (
            lambda r: r.update(page_reading_refs=[{**_page(1), "page_ordinal": 0}, _page(2)]),
            "with a positive page ordinal",
        ),
        (
            lambda r: r.update(page_reading_refs=[_page(1), _page(2), _page(3)]),
            r"no unit on sealed page\(s\) \[3\]",
        ),
        (lambda r: r["items"][1].update(act_key=r["items"][0]["act_key"]), "one act key twice"),
        (lambda r: r["items"][0].update(act_key="1:1"), "names act key '1:1'"),
        (lambda r: r["items"][0].update(act_key="p1:0"), "names act key 'p1:0'"),
        (lambda r: r["items"][0].update(act_key="p1:refused"), "names act key 'p1:refused'"),
        (lambda r: r.update(proposal_seal_ref=_ref("seal")), "wrong closed schema"),
        (
            lambda r: r.update(scope="proposal-acts-and-configured-witnesses"),
            "invalid run or denominator facts",
        ),
        (
            lambda r: r.update(expected_act_count=r.pop("expected_unit_count")),
            "wrong closed schema",
        ),
        (lambda r: r.update(expected_unit_count=3), "invalid run or denominator facts"),
        (lambda r: r.pop("continuation_links"), "wrong closed schema"),
        (
            lambda r: r.update(continuation_links=[_link(1), _link(1)]),
            "not one per page break",
        ),
        (
            lambda r: r.update(continuation_links=[{**_link(1), "subject_id": "page-break:1:3"}]),
            "names continuation link 'page-break:1:3'",
        ),
        (lambda r: r.update(continuation_links=[_link(1, "joined")]), "is 'joined', not one of"),
        (
            lambda r: r.update(continuation_links=[_link(1, "held-for-review")]),
            "status does not derive from its items",
        ),
    ],
    ids=[
        "disposition",
        "designator-outcome",
        "no-release-field",
        "act-granularity",
        "no-page",
        "ordinal-twice",
        "reading-twice",
        "out-of-order",
        "not-keyed",
        "ordinal-zero",
        "page-without-unit",
        "act-key-twice",
        "act-key-no-page",
        "act-key-zero",
        "act-key-refused-page",
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
def test_a_malformed_v3_receipt_is_refused(change, refusal):
    forged = copy.deepcopy(_receipt([_item("act_a", "p1:1"), _item("act_b", "p2:1")]))
    change(forged)
    forged["self_hash"] = self_hash({k: v for k, v in forged.items() if k != "self_hash"})
    with pytest.raises(SchemaRefusal, match=refusal):
        validate_recensor_partition_receipt(forged)
