"""Page types and entry kinds: the answer grammar, the per-type checks and the rows export."""

from __future__ import annotations

import copy
import json

import pytest

from common import page_answer, page_path, page_types, truncation
from common.page_accounting import (
    DUPLICATE_REGION,
    NO_RECORD_ON_ACT_PAGE,
    ROWS_SHARE_UNIT,
    validate_answer,
)
from common.test_page_accounting import (
    POLICY,
    account,
    bx,
    codes,
    line_box,
    page,
)


def entries_answer(answer: dict, page_type: str, writing: str, kinds: dict | None = None) -> dict:
    """An `acts`-grammar answer restated in the `entries` grammar, kinds renamed by `kinds`."""
    kinds = kinds or {}
    return {
        "page_type": page_type,
        "writing": writing,
        "entries": [{**act, "kind": kinds.get(act["kind"], act["kind"])} for act in answer["acts"]],
        "set_aside": answer["set_aside"],
    }


# --- the vocabulary ----------------------------------------------------------------------


def test_the_vocabularies_are_the_lead_s_and_each_kind_has_an_act_class():
    assert page_types.PAGE_TYPES == (
        "register-acts",
        "index",
        "table",
        "ledger",
        "instrument",
        "prose",
        "blank",
    )
    assert page_types.ENTRY_KINDS == (
        "act",
        "index-row",
        "table-row",
        "ledger-entry",
        "instrument",
        "paragraph",
        "other",
    )
    classes = {kind: page_types.act_class(kind) for kind in page_types.ENTRY_KINDS}
    assert {kind for kind, cls in classes.items() if cls == "act"} == {"act", "instrument"}
    with pytest.raises(ValueError):
        page_types.act_class("baptism")


# --- the grammar -------------------------------------------------------------------------


def test_both_shapes_parse_and_each_keeps_its_own_kinds():
    old = page()["reading"]["answer"]
    new = entries_answer(old, "register-acts", "handwritten")
    for answer, grammar in ((old, "acts"), (new, "entries")):
        state, parsed, problems = page_answer.parse_page_answer(json.dumps(answer))
        assert (state, problems) == ("parsed", [])
        assert page_answer.answer_grammar(parsed) == grammar
        assert page_answer.answer_entry_list(parsed) == answer[grammar]
    assert page_answer.stated_page_type(new) == ("register-acts", "handwritten")
    assert page_answer.stated_page_type(old) == (None, None)
    # An index row in the `acts` shape is not its grammar; in the `entries` shape it is.
    mixed = copy.deepcopy(old)
    mixed["acts"][0]["kind"] = "index-row"
    assert page_answer.parse_page_answer(json.dumps(mixed))[0] == "malformed"
    rows = entries_answer(old, "index", "handwritten", {"act": "index-row"})
    assert page_answer.parse_page_answer(json.dumps(rows))[0] == "parsed"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"page_type": "register"}, "page-type-unknown"),
        ({"writing": "inked"}, "writing-unknown"),
        ({"page_type": None}, "page-type-unknown"),
    ],
)
def test_a_page_type_or_writing_outside_the_lists_is_malformed(change, code):
    answer = {**entries_answer(page()["reading"]["answer"], "index", "typed"), **change}
    state, _parsed, problems = page_answer.parse_page_answer(json.dumps(answer))
    assert state == "malformed" and [p["code"] for p in problems] == [code]


def test_a_re_ask_answers_entries_without_a_page_type_and_keys_mix_no_shape():
    answer = entries_answer(page()["reading"]["answer"], "index", "typed")
    del answer["page_type"], answer["writing"]
    assert page_answer.parse_page_answer(json.dumps(answer))[0] == "parsed"
    both = {**answer, "acts": []}
    state, _parsed, problems = page_answer.parse_page_answer(json.dumps(both))
    assert state == "malformed" and problems[0]["code"] == "top-fields"


def test_bare_keys_of_the_entries_shape_are_quoted_by_the_one_repair():
    text = '{page_type: "blank", writing: "handwritten", entries: [], set_aside: []}'
    state, answer, problems, repairs = page_answer.parse_page_answer_repaired(text)
    assert (state, problems, answer["page_type"]) == ("parsed", [], "blank")
    assert repairs[0]["code"] == "unquoted-keys-quoted" and repairs[0]["keys"] == 4


def test_an_instrument_is_a_page_edge_and_a_row_is_not():
    answer = entries_answer(page()["reading"]["answer"], "instrument", "mixed")
    entries = answer["entries"]
    entries[0]["kind"] = "other"
    entries[1]["kind"] = "instrument"
    entries[1]["continues_from_previous_page"] = True
    assert page_answer.stray_continuation_flags(entries) == {}
    entries[1]["kind"] = "index-row"
    assert page_answer.stray_continuation_flags(entries) == {1: ["continues_from_previous_page"]}


# --- the accounting: the act class, and what an unstated type keeps -------------------


def test_an_entries_answer_is_accounted_as_its_acts_answer_with_kinds_beside():
    case = page()
    old = account(case)
    typed = copy.deepcopy(case)
    typed["reading"]["answer"] = entries_answer(
        case["reading"]["answer"], "register-acts", "handwritten"
    )
    new = account(typed)
    assert new["rules"] == old["rules"] and new["holds"] == old["holds"] == []
    assert [e["entry_kind"] for e in new["entries"]] == ["act", "act", "act"]
    assert new["page_type"]["stated"] == "register-acts"
    assert new["page_type"]["grammar"] == "entries"
    assert new["page_type"]["applicability"]["i"]["applies"] is True
    assert old["page_type"]["stated"] is None and old["page_type"]["grammar"] == "acts"
    assert old["page_type"]["agreement"] == []
    assert new["page_type"]["agreement"] == [
        {
            "check": "table-evidence",
            "agrees": True,
            "detail": "a register-acts page; a Table block or unit was not found",
        },
        {
            "check": "detector-records",
            "agrees": True,
            "detail": "handwritten register acts; the record detector found 3",
        },
    ]
    assert new["page_type"]["kinds"] == {"agrees": True, "unexpected_kinds": []}


def no_records(case: dict) -> dict:
    """The record detector looked at the page and found nothing."""
    case["detections"]["records"] = []
    case["detections"]["record_census"]["detection_count"] = 0
    case["feed"]["witnesses"] = [w for w in case["feed"]["witnesses"] if w["letter"] != "B"]
    case["witnesses"] = list(case["feed"]["witnesses"])
    for act in case["reading"]["answer"]["acts"]:
        act["cites"] = [cite for cite in act["cites"] if not cite.startswith("B")]
    return case


def test_the_detector_rule_holds_only_on_handwritten_register_acts():
    case = no_records(page())
    # Unstated (the `acts` grammar): held, as before page types.
    unstated = account(case)
    assert NO_RECORD_ON_ACT_PAGE in unstated["holds"]
    for writing, held in (
        ("handwritten", True),
        ("mixed", True),
        ("typed", False),
        ("printed", False),
    ):
        typed = copy.deepcopy(case)
        typed["reading"]["answer"] = entries_answer(
            case["reading"]["answer"], "register-acts", writing
        )
        record = account(typed)
        assert (NO_RECORD_ON_ACT_PAGE in record["holds"]) is held
        # The rule is measured either way; where it does not apply it is recorded only.
        assert codes(record, "i") == [NO_RECORD_ON_ACT_PAGE]
        assert record["page_type"]["recorded_not_held"] == ([] if held else [NO_RECORD_ON_ACT_PAGE])
        assert record["page_type"]["applicability"]["i"]["applies"] is held


def test_index_rows_are_not_acts_so_the_act_detector_rule_does_not_fire():
    case = no_records(page())
    case["reading"]["answer"] = entries_answer(
        case["reading"]["answer"], "index", "handwritten", {"act": "index-row"}
    )
    record = account(case)
    assert [e["kind"] for e in record["entries"]] == ["other"] * 3
    assert [e["entry_kind"] for e in record["entries"]] == ["index-row"] * 3
    assert NO_RECORD_ON_ACT_PAGE not in codes(record, "i")
    assert record["page_type"]["applicability"]["i"]["applies"] is False


# --- rows of one table unit --------------------------------------------------------------


def index_page(rows: int = 3, *, own_lines: bool = False) -> dict:
    """An index page: witness A gives the whole table as one unit, Surya one line per row.

    Each row cites the table unit, and its own line when `own_lines`.
    """
    case = no_records(page(rows))
    table = bx(100, 100, 900, 100 + 400 * rows)
    witness_a = case["feed"]["witnesses"][0]
    witness_a["units"] = [
        {
            "id": "A1",
            "box_px": table,
            "text": " ".join(act["text"] for act in case["reading"]["answer"]["acts"]),
        }
    ]
    for k, act in enumerate(case["reading"]["answer"]["acts"]):
        act["cites"] = ["A1", f"C{k + 1}"] + ([f"L{3 * k + 1}"] if own_lines else [])
    return case


def test_rows_placed_only_by_a_shared_table_unit_are_recorded_not_duplicates():
    case = index_page()
    old = account(case)
    # In the `acts` grammar every pair of rows on the table unit is a duplicate claim.
    assert codes(old, "h").count(DUPLICATE_REGION) == 3
    typed = copy.deepcopy(case)
    typed["reading"]["answer"] = entries_answer(
        case["reading"]["answer"], "index", "handwritten", {"act": "index-row"}
    )
    new = account(typed)
    assert DUPLICATE_REGION not in codes(new, "h")
    assert DUPLICATE_REGION not in new["holds"]
    [shared] = [f for f in new["rules"]["h"]["findings"] if f["code"] == ROWS_SHARE_UNIT]
    assert shared == {"code": ROWS_SHARE_UNIT, "id": "A1", "ns": [1, 2, 3]}
    # The same rows read as acts in the new grammar keep the act semantics.
    as_acts = copy.deepcopy(case)
    as_acts["reading"]["answer"] = entries_answer(
        case["reading"]["answer"], "register-acts", "typed"
    )
    assert codes(account(as_acts), "h").count(DUPLICATE_REGION) == 3


def test_two_rows_naming_the_same_line_are_still_duplicates():
    case = index_page(own_lines=True)
    case["reading"]["answer"]["acts"][1]["cites"] = ["A1", "C2", "L1"]
    typed = copy.deepcopy(case)
    typed["reading"]["answer"] = entries_answer(
        case["reading"]["answer"], "index", "handwritten", {"act": "index-row"}
    )
    findings = [
        f for f in account(typed)["rules"]["h"]["findings"] if f["code"] == DUPLICATE_REGION
    ]
    assert [f["ns"] for f in findings] == [[1, 2]]


def test_rows_with_their_own_lines_are_placed_by_them():
    case = index_page(own_lines=True)
    case["reading"]["answer"] = entries_answer(
        case["reading"]["answer"], "index", "handwritten", {"act": "index-row"}
    )
    validated = validate_answer(case["reading"]["answer"], _candidates(case), policy=POLICY)
    assert [entry["region_boxes_px"] for entry in validated["entries"]] == [
        [line_box(k, 0)] for k in range(3)
    ]


def _candidates(case: dict) -> dict:
    from common.page_accounting import feed_candidates

    return feed_candidates(case["feed"], POLICY)


# --- the cross-check -------------------------------------------------------------------


def test_the_cross_check_sets_the_type_beside_table_labels_and_the_detector_count():
    feed = {
        "surya": {"blocks": [{"label": "Table"}, {"label": "Text"}]},
        "witnesses": [
            {"letter": "A", "chair": "attestator_1", "units": [{"label": "Table"}]},
            {"letter": "C", "chair": "attestator_3", "units": [{"label": None}]},
            {"letter": "B", "chair": "attestator_2", "units": []},
        ],
    }
    facts = page_types.type_facts(feed, 0)
    assert facts == {
        "surya_table_blocks": 1,
        "witness_table_units": {"attestator_1": 1, "attestator_3": 0},
        "detector_records": 0,
    }
    agree = {
        c["check"]: c["agrees"] for c in page_types.type_agreement("index", "handwritten", facts)
    }
    assert agree == {"table-evidence": True, "detector-records": True}
    acts = page_types.type_agreement("register-acts", "handwritten", facts)
    assert {c["check"]: c["agrees"] for c in acts} == {
        "table-evidence": False,
        "detector-records": False,
    }
    assert page_types.type_agreement(None, None, facts) == []
    unmeasured = page_types.type_facts({"surya": None, "witnesses": []}, None)
    assert {
        c["check"]: c["agrees"] for c in page_types.type_agreement("table", "typed", unmeasured)
    } == {
        "table-evidence": None,
        "detector-records": None,
    }
    assert page_types.kind_agreement("index", ["index-row", "other", "act"]) == {
        "agrees": False,
        "unexpected_kinds": ["act"],
    }


# --- the truncation length signal ------------------------------------------------------


POLICY_TRUNCATION = {"length_floor_characters_per_page": 400, "legible_page_pixels": 100_000}


def test_the_length_signal_is_not_judged_on_a_kind_it_was_not_calibrated_for():
    short_row = dict(
        region_pixels=2_000_000, page_pixels=8_000_000, truncation_policy=POLICY_TRUNCATION
    )
    act = truncation.classify("114 21 Guyotte, Charles", stop_reason="stop", **short_row)
    assert act["signals"]["length_suspicious"] is True and act["classification"] == "unknown"
    assert "length_exempt_kind" not in act["measure"]
    row = truncation.classify(
        "114 21 Guyotte, Charles", stop_reason="stop", length_exempt_kind="index-row", **short_row
    )
    assert row["signals"]["length_suspicious"] is None
    assert row["measure"]["length_judged"] is False
    assert row["measure"]["length_exempt_kind"] == "index-row"
    assert row["classification"] == "complete"
    assert [k for k in page_types.ENTRY_KINDS if page_types.length_signal_applies(k)] == [
        "act",
        "instrument",
    ]


# --- the Perlectio and the rows export ---------------------------------------------------


def test_a_perlectio_carries_the_entry_kind_only_when_the_answer_named_it():
    base = {field: None for field in page_path.PERLECTIO_FIELDS}
    assert page_path.is_perlectio_field_set(base)
    assert page_path.is_perlectio_field_set({**base, "kind": "other", "entry_kind": "index-row"})
    assert page_path.is_perlectio_field_set({**base, "kind": "act", "entry_kind": "instrument"})
    assert not page_path.is_perlectio_field_set({**base, "kind": "act", "entry_kind": "index-row"})
    assert not page_path.is_perlectio_field_set({**base, "kind": "other", "entry_kind": "row"})
    assert page_path.named_kind_fields({"entry_kind": None}) == {}
    assert page_path.named_kind_fields({"entry_kind": "paragraph"}) == {"entry_kind": "paragraph"}


def test_a_row_kind_entry_becomes_one_rows_jsonl_line_and_nothing_else_does():
    perlectio = {
        "page_id": "page-2",
        "page_ordinal": 2,
        "n": 7,
        "kind": "other",
        "entry_kind": "index-row",
        "label": "Guyotte",
        "text": "114 21 Guyotte, Charles",
        "uncertain_spans": [],
        "gaps": [],
        "holds": ["duplicate-region"],
        "page_holds": ["unread-ink"],
    }
    row = page_types.row_record(perlectio, page_type="index", act_key="p2:7", act_id="act_x")
    assert row == {
        "schema": "armarium-row.v1",
        "act_key": "p2:7",
        "act_id": "act_x",
        "page_id": "page-2",
        "page_ordinal": 2,
        "page_type": "index",
        "n": 7,
        "entry_kind": "index-row",
        "label": "Guyotte",
        "text": "114 21 Guyotte, Charles",
        "uncertain_spans": [],
        "gaps": [],
        "holds": ["duplicate-region", "unread-ink"],
    }
    reviewed = page_types.row_record(
        perlectio,
        page_type="index",
        act_key="p2:7",
        act_id="act_x",
        review={"outcome": "held-for-review", "hold_codes": ["unread-ink"]},
    )
    assert reviewed["review"] == {"outcome": "held-for-review", "hold_codes": ["unread-ink"]}
    for kind in ("act", "instrument", "paragraph", "other"):
        assert (
            page_types.row_record(
                {**perlectio, "entry_kind": kind}, page_type="index", act_key="k", act_id="i"
            )
            is None
        )
    old = {key: value for key, value in perlectio.items() if key != "entry_kind"}
    assert page_types.row_record(old, page_type=None, act_key="k", act_id="i") is None
    assert page_types.entry_kind_of(old) == "other"
    assert page_types.entry_kind_of(perlectio) == "index-row"
