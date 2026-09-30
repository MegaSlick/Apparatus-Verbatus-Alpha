"""The v4 Recensor receipt: each page's first reading, re-ask and last accounting bound."""

from __future__ import annotations

import copy

import pytest

from common.contracts.canonical import self_hash
from common.contracts.errors import SchemaRefusal
from common.recensor_receipt import (
    RECENSOR_PARTITION_RECEIPT_SCHEMA_V4,
    build_recensor_reading_receipt,
    validate_recensor_partition_receipt,
)
from common.test_recensor_receipt_v3 import DIGEST, _item, _ref

REASK = {
    "named": ["A3", "B3", "L7"],
    "cleared": ["A3", "L7"],
    "set_aside": [],
    "unread": ["B3"],
    "duplicate": [3],
}


def _page(ordinal: int, reask: dict | None = None) -> dict:
    return {
        "page_ordinal": ordinal,
        "reading_ref": _ref(f"p{ordinal}"),
        "reask_ref": None if reask is None else _ref(f"p{ordinal}-reask"),
        "accounting_ref": _ref(f"a{ordinal}"),
        "reask": reask,
    }


def _receipt() -> dict:
    return build_recensor_reading_receipt(
        run_id="r",
        config_digest=DIGEST,
        pages=[_page(1, REASK), _page(2)],
        items=[_item("act_a", "p1:1"), _item("act_b", "p2:1")],
    )


def test_a_v4_receipt_binds_each_pages_readings_last_accounting_and_re_ask():
    receipt = _receipt()
    assert receipt["schema"] == RECENSOR_PARTITION_RECEIPT_SCHEMA_V4
    assert "page_reading_refs" not in receipt
    first, second = receipt["pages"]
    assert first["reask"] == REASK and first["reask_ref"]["relative_path"].endswith("reask.json")
    assert (second["reask_ref"], second["reask"]) == (None, None)
    assert receipt["recensor_status"] == "complete"


def _forged(change) -> dict:
    receipt = copy.deepcopy(_receipt())
    change(receipt)
    receipt["self_hash"] = self_hash({k: v for k, v in receipt.items() if k != "self_hash"})
    return receipt


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        (lambda r: r["pages"][0].update(reask=None), "names a re-ask without what it did"),
        (lambda r: r["pages"][1].update(reask=REASK), "names a re-ask without what it did"),
        (lambda r: r["pages"][0].pop("accounting_ref"), "page reading is not"),
        (lambda r: r["pages"][0]["reask"].pop("duplicate"), "re-ask is not"),
        (lambda r: r["pages"][0]["reask"].update(named=[]), "names no id, or one id twice"),
        (lambda r: r["pages"][0]["reask"].update(named=["A3", "A3", "L7"]), "one id twice"),
        (lambda r: r["pages"][0]["reask"].update(unread=[]), "does not split its named ids"),
        (lambda r: r["pages"][0]["reask"].update(cleared=["L7", "A3"]), "in the order named"),
        (
            lambda r: r["pages"][0]["reask"].update(set_aside=["A3"]),
            "does not split its named ids",
        ),
        (lambda r: r["pages"][0]["reask"].update(duplicate=[3, 3]), "increasing entry numbers"),
        (lambda r: r["pages"][0]["reask"].update(duplicate=[0]), "increasing entry numbers"),
        (lambda r: r.update(page_reading_refs=[]), "wrong closed schema"),
    ],
    ids=[
        "re-ask-without-outcome",
        "outcome-without-re-ask",
        "no-accounting",
        "reask-fields",
        "named-empty",
        "named-twice",
        "not-a-split",
        "out-of-order",
        "id-in-two-parts",
        "duplicate-twice",
        "duplicate-zero",
        "v3-field",
    ],
)
def test_a_malformed_v4_page_is_refused(change, refusal):
    with pytest.raises(SchemaRefusal, match=refusal):
        validate_recensor_partition_receipt(_forged(change))
