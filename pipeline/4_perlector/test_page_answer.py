"""The page answer grammar: what is read, what is held, and that nothing is repaired."""

from __future__ import annotations

import copy
import json

import page_answer
import pytest

GOOD = {
    "acts": [
        {
            "n": 1,
            "kind": "act",
            "label": "baptism",
            "cites": ["A2", "B3", "L10-L17"],
            "text": "Le vingt [[mai|may]] [[?]]",
            "continues_from_previous_page": True,
            "continues_to_next_page": False,
        },
        {
            "n": 2,
            "kind": "other",
            "cites": [],
            "text": "12",
            "continues_from_previous_page": False,
            "continues_to_next_page": True,
        },
    ],
    "set_aside": [{"id": "C9", "reason": "printed page number"}],
}


def _with(change) -> str:
    answer = copy.deepcopy(GOOD)
    change(answer)
    return json.dumps(answer, ensure_ascii=False)


def _codes(problems):
    return [problem["code"] for problem in problems]


def test_the_grammar_is_read_exactly_as_given():
    raw = json.dumps(GOOD, ensure_ascii=False)
    state, answer, problems = page_answer.parse_page_answer(raw)
    assert (state, answer, problems) == ("parsed", GOOD, [])


def test_surrounding_whitespace_is_not_a_deviation():
    assert page_answer.parse_page_answer("\n  " + json.dumps(GOOD) + "\n")[::2] == ("parsed", [])


@pytest.mark.parametrize("opening", ["```json\n", "```\n", "```json "])
def test_a_code_fenced_answer_is_malformed_and_named(opening):
    state, answer, problems = page_answer.parse_page_answer(f"{opening}{json.dumps(GOOD)}\n```\n")
    assert (state, answer, _codes(problems)) == ("malformed", None, ["fenced-answer"])


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("Here is the page:\n" + json.dumps(GOOD), "not-json"),
        (json.dumps(GOOD) + "\nI hope this helps.", "content-outside-object"),
        (json.dumps(GOOD) + json.dumps(GOOD), "content-outside-object"),
        ("```json\n" + json.dumps(GOOD) + "\n```\n```json\n{}\n```", "fenced-answer"),
        (json.dumps(GOOD)[:-5], "not-json"),
        ('{"acts": [], "acts": [], "set_aside": []}', "duplicate-key"),
        ('{"acts": [{"n": NaN}], "set_aside": []}', "not-json"),
        ("[" * 100_000 + "]" * 100_000, "not-json"),
        ("[]", "not-object"),
        ('{"acts": []}', "top-fields"),
        ('{"acts": [], "set_aside": [], "notes": ""}', "top-fields"),
        ('{"acts": {}, "set_aside": []}', "acts-not-list"),
        ('{"acts": [], "set_aside": {}}', "set-aside-not-list"),
        ('{"acts": [1], "set_aside": []}', "act-not-object"),
        (_with(lambda a: a["acts"][0].pop("text")), "act-field-missing"),
        (_with(lambda a: a["acts"][1].pop("continues_to_next_page")), "act-field-missing"),
        (_with(lambda a: a["acts"][0].update(confidence="high")), "act-field-unknown"),
        (_with(lambda a: a["acts"][1].update(n=3)), "n-not-contiguous"),
        (_with(lambda a: a["acts"].reverse()), "n-not-contiguous"),
        (_with(lambda a: a["acts"][0].update(n=1.0)), "n-not-integer"),
        (_with(lambda a: a["acts"][0].update(n=True)), "n-not-integer"),
        (_with(lambda a: a["acts"][0].update(kind="baptism")), "kind-unknown"),
        (_with(lambda a: a["acts"][0].update(label="x" * 81)), "label-invalid"),
        (_with(lambda a: a["acts"][0].update(label="  ")), "label-invalid"),
        (_with(lambda a: a["acts"][0].update(label=7)), "label-invalid"),
        (_with(lambda a: a["acts"][0].update(cites="A1-A4")), "cites-invalid"),
        (_with(lambda a: a["acts"][0].update(cites=["A1", 2])), "cites-invalid"),
        (_with(lambda a: a["acts"][0].update(text=None)), "text-invalid"),
        (_with(lambda a: a["acts"][0].update(continues_to_next_page="no")), "flag-invalid"),
        (
            _with(lambda a: a["acts"][0].update(continues_to_next_page=True)),
            "continuation-not-at-edge",
        ),
        (
            _with(lambda a: a["acts"][1].update(continues_from_previous_page=True)),
            "continuation-not-at-edge",
        ),
        (_with(lambda a: a["set_aside"].append({"id": "A1"})), "set-aside-invalid"),
        (_with(lambda a: a["set_aside"].append({"id": 4, "reason": "x"})), "set-aside-invalid"),
    ],
)
def test_anything_else_is_malformed_and_held_with_its_reason(raw, code):
    state, answer, problems = page_answer.parse_page_answer(raw)
    assert (state, answer) == ("malformed", None)
    assert code in _codes(problems)


@pytest.mark.parametrize(
    "change",
    [
        lambda a: a["acts"][0].pop("label"),
        lambda a: a["acts"][0].update(label=None),
        lambda a: a["acts"][0].update(label="x" * 80),
        lambda a: a.update(acts=[], set_aside=[]),
        lambda a: a["acts"][1].update(cites=["Z99", "L17-L10"]),
        lambda a: a["set_aside"].append({"id": "C9", "reason": ""}),
    ],
    ids=["no-label", "null-label", "80-characters", "empty-page", "ids-unchecked", "blank-reason"],
)
def test_what_the_accounting_judges_is_not_judged_here(change):
    raw = _with(change)
    state, answer, problems = page_answer.parse_page_answer(raw)
    assert (state, answer, problems) == ("parsed", json.loads(raw), [])


def test_one_act_may_carry_both_continuation_flags():
    raw = _with(lambda a: a.update(acts=[a["acts"][0] | {"continues_to_next_page": True}]))
    assert page_answer.parse_page_answer(raw)[0] == "parsed"


def test_every_problem_in_an_answer_is_reported_not_only_the_first():
    raw = _with(lambda a: (a["acts"][0].update(kind="entry"), a["acts"][1].update(text=3)))
    assert _codes(page_answer.parse_page_answer(raw)[2]) == ["kind-unknown", "text-invalid"]


def test_a_reply_that_is_not_text_is_malformed():
    assert page_answer.parse_page_answer(b"{}")[:2] == ("malformed", None)
