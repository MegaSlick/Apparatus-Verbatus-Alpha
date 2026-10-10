"""The page re-ask on synthetic pages: which ids it names, and how both readings are accounted."""

from __future__ import annotations

import copy
import dataclasses
import json

import pytest

from common import page_accounting as accounting_module
from common.contracts.errors import ContractError, FatalAccounting
from common.page_accounting import (
    HOLD_CODES,
    NOT_MEASURED_CODES,
    RECORD_NOT_READ,
    UNACCOUNTED_WITNESS_UNIT,
    UNREAD_LINE,
)
from common.page_path import reask_act_plans
from common.page_reask import (
    RE_ASKABLE,
    named_ids,
    reask_budget,
    reask_outcome,
    reask_plan,
    render_reask,
)
from common.test_page_accounting import (
    POLICY,
    account,
    acts,
    band,
    bx,
    codes,
    page,
    record_text,
)

# Findings the accounting records without holding on them.
RECORDED_CODES = frozenset(
    {
        accounting_module.SHARED_LINE,
        accounting_module.SPLIT_DETECTION,
        accounting_module.WITNESS_READ_BLANK,
        accounting_module.TOO_FEW_DISTINCTIVE_PIECES,
        accounting_module.NO_RECORD_DETECTOR,
    }
)
ALL_CODES = sorted(HOLD_CODES | NOT_MEASURED_CODES | RECORDED_CODES)


def shown(case: dict) -> dict:
    """The case's feed with each id's box on the 0-1000 grid, as a page feed carries it."""
    feed = case["feed"]
    for witness in feed["witnesses"]:
        for unit in witness["units"]:
            box = unit["box_px"]
            unit["box_1000"] = None if box is None else [box["x"], box["y"], box["w"], box["h"]]
    for item in feed["surya"]["lines"] + feed["surya"]["blocks"]:
        box = item["box_px"]
        item["box_1000"] = [box["x"], box["y"], box["w"], box["h"]]
    return feed


def first_reading(case: dict) -> dict:
    """The case's reading as a first `page-reading` payload: parsed, read, finished on stop."""
    return {**case["reading"], "stop_reason": "stop", "disposition": "read"}


def plan(case: dict, record: dict | None = None, budget: int = 1) -> list[dict]:
    record = account(case) if record is None else record
    return reask_plan(first_reading(case), record, shown(case), budget=budget, policy=POLICY)


def missing_last(records: int = 3) -> dict:
    """A clean page whose reading leaves out its last record: its units, lines and record."""
    case = page(records)
    case["reading"]["answer"]["entries"] = acts(case)[:-1]
    del case["entry_truncation"][records]
    return case


# --- the codes ---------------------------------------------------------------------


def test_only_the_three_unread_findings_are_re_askable_and_each_holds():
    assert RE_ASKABLE == {UNACCOUNTED_WITNESS_UNIT, UNREAD_LINE, RECORD_NOT_READ}
    # Each holds the page; none is a measurement that could not be taken.
    assert RE_ASKABLE <= HOLD_CODES - NOT_MEASURED_CODES
    # What the re-ask itself finds is never asked about again.
    assert not any(code.startswith("reask-") for code in RE_ASKABLE)


@pytest.mark.parametrize("code", ALL_CODES)
def test_a_finding_names_its_id_only_when_its_code_is_re_askable(code):
    case = missing_last()
    record = account(case)
    record["rules"]["c"]["findings"] = [{"code": code, "id": "A3"}]
    for name in "abdefghij":
        record["rules"][name]["findings"] = []
    named = plan(case, record)
    if code in RE_ASKABLE:
        assert named == [{"id": "A3", "code": code, "box_1000": [100, 900, 800, 300]}]
    else:
        assert named == []


def test_a_reading_that_left_a_record_out_names_its_boxed_units_lines_and_record():
    named = plan(missing_last())
    assert [(item["id"], item["code"]) for item in named] == [
        ("A3", UNACCOUNTED_WITNESS_UNIT),
        ("B3", RECORD_NOT_READ),
        ("B3", UNACCOUNTED_WITNESS_UNIT),
        ("L7", UNREAD_LINE),
        ("L8", UNREAD_LINE),
        ("L9", UNREAD_LINE),
    ]
    # C3 is unaccounted for too, but has no box to point the re-ask at.
    assert "unaccounted-witness-unit" in account(missing_last())["holds"]
    assert named_ids(named) == ["A3", "B3", "L7", "L8", "L9"]


def test_a_clean_page_plans_no_re_ask():
    assert plan(page()) == []


def test_the_re_ask_is_off_at_a_budget_of_zero():
    assert plan(missing_last(), budget=0) == []
    assert plan(missing_last(), budget=1) != []
    policy = {"config_sha256": "0" * 64, "page_level_reread": 1}
    assert reask_budget(policy) == 1
    assert reask_budget({**policy, "page_level_reread": 0}) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"parse_state": "malformed"},
        {"disposition": "held"},
        {"finish_reason": None},
        {"finish_reason": "length"},
    ],
    ids=["malformed", "held", "no-finish-reason", "cut-off"],
)
def test_only_a_read_answer_finished_on_stop_is_re_asked(change):
    case = missing_last()
    record = account(case)
    reading = {**first_reading(case), **change}
    assert reask_plan(reading, record, shown(case), budget=1, policy=POLICY) == []


def test_a_reading_with_no_entry_is_re_asked_about_every_boxed_id():
    case = page(2)
    case["reading"]["answer"]["entries"] = []
    case["entry_truncation"] = {}
    ids = named_ids(plan(case))
    assert ids == ["A1", "A2", "B1", "B2", "L1", "L2", "L3", "L4", "L5", "L6"]


def test_a_unit_inside_a_region_its_entry_did_not_cite_is_held_not_re_asked():
    """Cited, but forgot to cite: B3 lies inside entry 3's region, so it is left out."""
    case = page(3)
    acts(case)[2]["cites"] = ["A3", "C3", "L7", "L8", "L9"]
    record = account(case)
    assert codes(record, "c") == [UNACCOUNTED_WITNESS_UNIT]
    assert plan(case, record) == []


def test_a_unit_inside_an_other_region_is_left_out_too():
    """B3 lies inside an `other` entry's region: its record is read as other, never re-asked."""
    case = page(3)
    acts(case)[2]["cites"] = ["A3", "C3", "L7", "L8", "L9"]
    acts(case)[2]["kind"] = "other"
    holds = account(case)["holds"]
    assert UNACCOUNTED_WITNESS_UNIT in holds and "record-read-as-other" in holds
    assert plan(case) == []


def test_hidden_witnesses_unboxed_units_flat_units_and_unshown_lines_are_never_named():
    case = missing_last()
    # A hidden witness keeps its units in the accounting, never in the feed.
    case["feed"]["witnesses"] = [w for w in case["feed"]["witnesses"] if w["letter"] != "A"]
    for item in acts(case):
        item["cites"] = [cite for cite in item["cites"] if not cite.startswith("A")]
    # A line the feed never showed is measured, with no id to name.
    case["feed"]["surya"]["lines"] = [
        line for line in case["feed"]["surya"]["lines"] if line["id"] != "L9"
    ]
    for line in case["detections"]["surya"]["lines"]:
        if line["id"] == "L9":
            line["id"] = None
    for item in acts(case):
        item["cites"] = [cite for cite in item["cites"] if cite != "L9"]
    record = account(case)
    assert {f["id"] for f in record["rules"]["c"]["findings"]} >= {"C3"}
    assert [f.get("id") for f in record["rules"]["d"]["findings"]] == ["L7", "L8", None]
    ids = named_ids(plan(case, record))
    assert ids == ["B3", "L7", "L8"]
    flat = missing_last()
    flat["feed"]["switches"]["witness_units"] = "flat"
    # Shown flat, no witness unit places anything, so only the lines are named.
    assert named_ids(plan(flat)) == ["L7", "L8", "L9"]


def test_what_the_re_ask_shows_is_the_first_entries_without_their_text():
    case = missing_last()
    named = plan(case)
    rendered = render_reask(case["reading"]["answer"], named)
    assert rendered["named"] == named
    assert rendered["prior_entries"] == [
        {"n": item["n"], "kind": "act", "label": "baptism", "cites": item["cites"]}
        for item in acts(case)
    ]
    assert all(item["text"] not in json.dumps(rendered) for item in acts(case))


# --- the accounting of both readings -------------------------------------------------


def reask_of(case: dict, answer, *, parse_state="parsed", finish_reason="stop", named=None):
    """The `reask` argument for a re-ask of `case` that answered `answer`."""
    ids = named_ids(plan(case)) if named is None else named
    return {
        "reading": {"parse_state": parse_state, "finish_reason": finish_reason, "answer": answer},
        "named": ids,
        "entry_truncation": {
            item["n"]: "complete" for item in (answer or {}).get("entries", []) if item["cites"]
        },
    }


def recovered(case: dict, text: str, cites=("A3", "B3", "L7", "L8", "L9"), **fields) -> dict:
    return {
        "entries": [
            {
                "n": 1,
                "kind": "act",
                "label": "baptism",
                "cites": list(cites),
                "text": text,
                "continues_from_previous_page": False,
                "continues_to_next_page": False,
                **fields,
            }
        ],
        "set_aside": [],
    }


def last_text(case: dict) -> str:
    return record_text(len(acts(case)))


def test_a_first_readings_accounting_does_not_apply_rule_j():
    record = account(page())
    assert record["answer_basis"] == "attempt-1"
    assert record["rules"]["j"] == {"status": "not-applicable", "findings": []}
    assert [entry["reading_attempt"] for entry in record["entries"]] == [1, 1, 1]


def test_a_recovered_record_clears_every_id_it_was_asked_about():
    case = missing_last()
    first = account(case)
    both = account(case, reask=reask_of(case, recovered(case, last_text(case))))
    assert both["answer_basis"] == "combined"
    assert both["rules"]["j"]["status"] == "pass"
    # Only Churro's unboxed unit, which no re-ask may name, is still unaccounted for.
    assert both["holds"] == [UNACCOUNTED_WITNESS_UNIT]
    assert [f["id"] for f in both["rules"]["c"]["findings"]] == ["C3"]
    # The first reading's entries stand exactly; the recovered one is numbered on.
    assert both["entries"][:2] == first["entries"]
    assert both["entries"][2] == {
        "n": 3,
        "reading_attempt": 2,
        "reading_n": 1,
        "kind": "act",
        "entry_kind": "act",
        "cited_ids": ["A3", "B3", "L7", "L8", "L9"],
        "union_box_px": band(2),
    }


def test_a_record_with_the_same_formula_and_other_names_is_not_a_duplicate():
    case = missing_last()
    both = account(case, reask=reask_of(case, recovered(case, last_text(case))))
    assert accounting_module.REASK_DUPLICATE not in codes(both, "j")


def test_a_re_ask_that_reads_a_first_entry_again_is_held_as_its_duplicate():
    case = missing_last()
    again = recovered(case, acts(case)[1]["text"])
    both = account(case, reask=reask_of(case, again))
    assert both["rules"]["j"]["findings"] == [
        {"code": "reask-duplicate", "n": 3, "reading_n": 1, "attempt_1_n": 2}
    ]
    assert "reask-duplicate" in both["holds"]


def test_a_duplicate_check_past_the_work_budget_is_not_measured_and_holds():
    case = missing_last()
    tight = dataclasses.replace(POLICY, max_alignment_steps=10)
    both = account(case, policy=tight, reask=reask_of(case, recovered(case, last_text(case))))
    assert "reask-duplicate-not-measured" in codes(both, "j")
    assert "reask-duplicate-not-measured" in both["holds"]


def test_a_named_id_the_re_ask_sets_aside_holds():
    case = missing_last()
    answer = {
        "entries": [],
        "set_aside": [
            {"id": identifier, "reason": "no entry"}
            for identifier in ("A3", "B3", "L7", "L8", "L9")
        ],
    }
    both = account(case, reask=reask_of(case, answer))
    assert [(f["code"], f["id"]) for f in both["rules"]["j"]["findings"]] == [
        ("reask-set-aside", identifier) for identifier in ("A3", "B3", "L7", "L8", "L9")
    ]
    assert "set-aside-record" in both["holds"] and "set-aside-substantial" in both["holds"]


def test_a_re_ask_entry_citing_nothing_is_held_unplaced():
    case = missing_last()
    both = account(case, reask=reask_of(case, recovered(case, last_text(case), cites=())))
    assert codes(both, "j")[0] == "reask-unplaced"
    assert both["rules"]["j"]["findings"][0] == {"code": "reask-unplaced", "n": 3, "reading_n": 1}
    assert "reading-unplaced" in both["holds"]


@pytest.mark.parametrize(
    ("answer", "state", "finish", "problems"),
    [
        (None, "malformed", "stop", []),
        (None, "cut-off", "length", []),
        (None, "refused-capacity", None, []),
        (None, "call-failed", None, []),
        ("recovered", "parsed", None, []),
        ("cites-unnamed", "parsed", "stop", ["unknown-id"]),
        ("continues", "parsed", "stop", ["reask-continuation"]),
    ],
    ids=[
        "malformed",
        "cut-off",
        "refused",
        "failed",
        "no-finish-reason",
        "non-named-cite",
        "continues",
    ],
)
def test_a_re_ask_that_is_not_a_valid_answer_is_unread_and_the_page_stands_on_its_first(
    answer, state, finish, problems
):
    case = missing_last()
    first = account(case)
    text = last_text(case)
    answers = {
        "recovered": recovered(case, text),
        "cites-unnamed": recovered(case, text, cites=("A3", "A1")),
        "continues": recovered(case, text, continues_to_next_page=True),
    }
    both = account(
        case,
        reask=reask_of(case, answers.get(answer), parse_state=state, finish_reason=finish),
    )
    [unread] = both["rules"]["j"]["findings"]
    assert unread["code"] == "reask-unread" and unread["problems"] == problems
    assert both["entries"] == first["entries"]
    assert set(both["holds"]) == set(first["holds"]) | {"reask-unread"}


def test_the_first_readings_last_entry_may_continue_with_a_recovered_entry_after_it():
    case = missing_last()
    acts(case)[-1]["continues_to_next_page"] = True
    both = account(case, reask=reask_of(case, recovered(case, last_text(case))))
    assert both["rules"]["a"]["status"] == "pass"
    assert both["holds"] == [UNACCOUNTED_WITNESS_UNIT]


def test_a_blank_first_reading_is_recovered_whole():
    case = page(2)
    case["reading"]["answer"]["entries"] = []
    case["entry_truncation"] = {}
    answer = {
        "entries": [
            {
                **recovered(case, record_text(k), cites=(f"A{k + 1}", f"B{k + 1}"))["entries"][0],
                "n": k + 1,
                "cites": [f"A{k + 1}", f"B{k + 1}", *(f"L{3 * k + row}" for row in (1, 2, 3))],
            }
            for k in range(2)
        ],
        "set_aside": [],
    }
    both = account(case, reask=reask_of(case, answer))
    assert both["rules"]["j"]["status"] == "pass"
    assert both["holds"] == [UNACCOUNTED_WITNESS_UNIT]
    assert [f["id"] for f in both["rules"]["c"]["findings"]] == ["C1", "C2"]


def test_a_re_ask_entry_on_a_first_entrys_region_is_a_duplicate_region():
    case = missing_last()
    again = recovered(case, last_text(case), cites=("A3", "B3", "L7", "L8", "L9"))
    moved = copy.deepcopy(case)
    for witness in moved["feed"]["witnesses"]:
        for unit in witness["units"]:
            if unit["id"] in ("A3", "B3"):
                unit["box_px"] = band(1)
    for record in moved["detections"]["records"]:
        if record["id"] == "B3":
            record["box_px"] = band(1)
    for line in moved["feed"]["surya"]["lines"] + moved["detections"]["surya"]["lines"]:
        if line["id"] in ("L7", "L8", "L9"):
            line["box_px"] = bx(100, 500, 900, 800)
    both = account(moved, reask=reask_of(moved, again, named=["A3", "B3", "L7", "L8", "L9"]))
    # Regions compare as ink: the recovered entry claims all of entry 2's.
    [duplicate] = [f for f in both["rules"]["h"]["findings"] if f["code"] == "duplicate-region"]
    assert duplicate["ns"] == [2, 3]
    assert duplicate["shared_px"] == duplicate["smaller_region_px"] > 0


def test_a_re_ask_is_accounted_only_over_a_parsed_first_reading_and_named_ids_it_shows():
    case = missing_last()
    unread = copy.deepcopy(case)
    unread["reading"] = {"parse_state": "malformed", "finish_reason": "stop", "answer": None}
    with pytest.raises(ContractError, match="first reading that parsed"):
        account(unread, reask=reask_of(case, recovered(case, last_text(case))))
    with pytest.raises(ContractError, match="does not place"):
        account(case, reask=reask_of(case, recovered(case, "x"), named=["C3"]))
    with pytest.raises(ContractError, match="names no id, or one id twice"):
        account(case, reask=reask_of(case, recovered(case, "x"), named=["A3", "A3"]))


@pytest.mark.parametrize("text", ["", "  [[?]] ", "--"])
def test_a_re_ask_entry_giving_no_text_holds_though_it_accounts_for_its_ids(text):
    case = missing_last()
    both = account(
        case, reask=reask_of(case, recovered(case, text, cites=("B3", "L7", "L8", "L9")))
    )
    assert {"code": "reask-no-text", "n": 3, "reading_n": 1} in both["rules"]["j"]["findings"]
    assert "reask-no-text" in both["holds"]


def _set_aside(*identifiers: str) -> list[dict]:
    return [{"id": identifier, "reason": "no entry here"} for identifier in identifiers]


@pytest.mark.parametrize(
    ("first_sets_aside", "answer", "named", "code"),
    [
        # The first reading cites A1; the re-ask sets it aside.
        ((), {"entries": [], "set_aside": _set_aside("A1")}, ["A1"], "cited-and-set-aside"),
        # The first reading sets A3 aside; the re-ask cites it.
        (("A3",), "recovered", ["A3", "B3", "L7", "L8", "L9"], "cited-and-set-aside"),
        # Both readings set L7 aside.
        (("L7",), {"entries": [], "set_aside": _set_aside("L7")}, ["L7"], "set-aside-twice"),
    ],
    ids=["read-then-set-aside", "set-aside-then-read", "set-aside-twice"],
)
def test_an_id_both_readings_account_for_holds_the_re_ask_whole(
    first_sets_aside, answer, named, code
):
    case = missing_last()
    case["reading"]["answer"]["set_aside"] = _set_aside(*first_sets_aside)
    first = account(case)
    if answer == "recovered":
        answer = recovered(case, last_text(case))
    both = account(case, reask=reask_of(case, answer, named=named))
    [unread] = both["rules"]["j"]["findings"]
    assert unread["code"] == "reask-unread" and unread["problems"] == [code]
    # The re-ask adds nothing, not even its set-asides; the page stands on its first reading.
    assert both["entries"] == first["entries"]
    assert both["units"] == first["units"]
    assert not accounting_module.reask_stood(both)


def test_a_re_ask_is_planned_only_from_a_first_readings_accounting():
    case = missing_last()
    both = account(case, reask=reask_of(case, recovered(case, last_text(case))))
    for budget in (0, 1):
        with pytest.raises(ContractError, match="only from a first reading's accounting"):
            reask_plan(first_reading(case), both, shown(case), budget=budget, policy=POLICY)
    assert plan(case)


def test_what_a_re_ask_did_splits_its_named_ids_by_the_last_accounting():
    case = missing_last()
    named = plan(case)
    ids = named_ids(named)
    cleared = account(case, reask=reask_of(case, recovered(case, last_text(case))))
    assert reask_outcome(named, cleared) == {
        "named": ids,
        "cleared": ids,
        "set_aside": [],
        "held": [],
        "unread": [],
        "duplicate": [],
    }
    aside = {"entries": [], "set_aside": _set_aside("A3", "B3", "L7", "L8", "L9")}
    assert reask_outcome(named, account(case, reask=reask_of(case, aside)))["set_aside"] == ids
    malformed = account(case, reask=reask_of(case, None, parse_state="malformed"))
    assert reask_outcome(named, malformed)["unread"] == ids


@pytest.mark.parametrize(
    ("text", "cites", "code", "held", "unread"),
    [
        ("  [[?]] ", ("B3", "L7", "L8", "L9"), "reask-no-text", ["B3", "L7", "L8", "L9"], ["A3"]),
        (None, (), "reask-unplaced", [], ["A3", "B3", "L7", "L8", "L9"]),
        (
            "second",
            ("A3", "B3", "L7", "L8", "L9"),
            "reask-duplicate",
            ["A3", "B3", "L7", "L8", "L9"],
            [],
        ),
    ],
    ids=["no-text", "unplaced", "duplicate"],
)
def test_an_id_only_a_held_re_ask_entry_accounts_for_is_never_cleared(
    text, cites, code, held, unread
):
    case = missing_last()
    named = plan(case)
    text = {None: last_text(case), "second": acts(case)[1]["text"]}.get(text, text)
    both = account(case, reask=reask_of(case, recovered(case, text, cites=cites)))
    assert code in codes(both, "j") and code in both["holds"]
    outcome = reask_outcome(named, both)
    assert (outcome["cleared"], outcome["held"], outcome["unread"]) == ([], held, unread)
    assert outcome["duplicate"] == ([3] if code == "reask-duplicate" else [])


def _entries(*keys: tuple[int, int, int]) -> list[dict]:
    return [{"reading_attempt": a, "reading_n": j, "n": n} for a, j, n in keys]


def test_a_re_asked_page_publishes_exactly_the_entries_its_accounting_counts():
    first, second = _entries((1, 1, 1)), _entries((2, 1, 2))
    stood = {"answer_basis": "combined", "rules": {"j": {"findings": []}}}
    unread = {"answer_basis": "combined", "rules": {"j": {"findings": [{"code": "reask-unread"}]}}}
    assert reask_act_plans({**stood, "entries": first + second}, first, second, "p") == [
        *first,
        *second,
    ]
    assert reask_act_plans({**unread, "entries": first}, first, second, "p") == first
    for accounting, reask in (
        # It counts the re-ask's entry, but the re-ask gave no entry to publish.
        ({**stood, "entries": first + second}, []),
        # It counts nothing of the re-ask it says stood.
        ({**stood, "entries": first}, second),
        # It numbers the recovered entry otherwise than its plan.
        ({**stood, "entries": first + _entries((2, 1, 3))}, second),
        # It counts an entry of a re-ask that did not stand.
        ({**unread, "entries": first + second}, second),
    ):
        with pytest.raises(FatalAccounting, match="counts other entries than its readings"):
            reask_act_plans(accounting, first, reask, "p")
