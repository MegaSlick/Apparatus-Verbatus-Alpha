"""The reconstruction answer grammar: strict at the answer level, never repaired."""

import json

import pytest

from common.reconstruction_answer import (
    ANSWER_INVALID,
    FINDING_CODES,
    MALFORMED,
    PARSED,
    parse_reconstruction_answer,
)

CALL = {
    "page_ordinal": 4,
    "subjects": ["p4:2", "p4:3"],
    "chains": [["p3:9", "p4:1"]],
    "context": ["p3:9", "p5:1"],
}
NO_CHAINS = {**CALL, "chains": []}
DEPARTURE = {"diplomatic": "Jan", "reconstruction": "Jean", "reason": "r"}
FINDING = {"code": "out-of-sequence", "reason": "dated 1888 among acts of 1666"}


def act(key, findings=(), departures=()):
    return {"act": key, "findings": list(findings), "departures": list(departures)}


def with_findings(*findings):
    return answer(acts=[act("p4:2", findings), act("p4:3")])


def answer(**changes):
    value = {
        "acts": [act("p4:2", [FINDING], [DEPARTURE]), act("p4:3")],
        "joins": [{"acts": ["p3:9", "p4:1"], "continues": True, "departures": []}],
    }
    value.update(changes)
    return value


def parse(value):
    return parse_reconstruction_answer(json.dumps(value), CALL)


def test_a_planned_answer_parses_as_given():
    state, parsed, problems = parse(answer())
    assert (state, parsed, problems) == (PARSED, answer(), [])


def test_an_empty_call_takes_an_empty_answer():
    state, _, _ = parse_reconstruction_answer(
        '{"acts": [], "joins": []}',
        {"page_ordinal": 1, "subjects": [], "chains": [], "context": []},
    )
    assert state == PARSED


def test_a_departure_s_content_is_left_to_apply():
    bad = answer(acts=[act("p4:2", departures=["nonsense"]), act("p4:3")])
    assert parse(bad)[0] == PARSED


def test_the_finding_codes_are_closed():
    assert FINDING_CODES == {
        "cut-at-page-break",
        "incomplete",
        "out-of-sequence",
        "inconsistent",
        "other",
    }


@pytest.mark.parametrize("code", sorted(FINDING_CODES))
def test_every_finding_code_parses_with_or_without_a_reason(code):
    value = with_findings(
        {"code": code}, {"code": code, "reason": "why"}, {"code": code, "reason": ""}
    )
    assert parse(value)[0] == PARSED


def test_with_no_chain_planned_joins_must_be_empty():
    value = answer(joins=[])
    assert parse_reconstruction_answer(json.dumps(value), NO_CHAINS)[0] == PARSED
    state, parsed, problems = parse_reconstruction_answer(json.dumps(answer()), NO_CHAINS)
    assert (state, parsed) == (ANSWER_INVALID, None)
    assert [problem["code"] for problem in problems] == ["joins-not-planned"]


@pytest.mark.parametrize(
    "finding",
    [
        {"code": "date-wrong"},
        {"code": "Out-of-sequence"},
        {"code": ["other"]},
        {"code": None},
        {"reason": "no code"},
        {"code": "other", "reason": 3},
        {"code": "other", "reason": None},
        {"code": "other", "note": ""},
        "other",
    ],
)
def test_an_unknown_or_malformed_finding_is_invalid(finding):
    state, parsed, problems = parse(with_findings(FINDING, finding))
    assert (state, parsed) == (ANSWER_INVALID, None)
    assert [problem["code"] for problem in problems] == ["finding-invalid"]


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '```json\n{"acts": [], "joins": []}\n```',
        '{"acts": [], "acts": [], "joins": []}',
        '{"acts": [], "joins": []} trailing',
        '{"acts": [NaN], "joins": []}',
        '{"acts": ["\\ud800"], "joins": []}',
        "[" * 70 + "]" * 70,
        None,
    ],
)
def test_a_reply_that_is_not_one_bare_json_value_is_malformed(raw):
    state, parsed, problems = parse_reconstruction_answer(raw, CALL)
    assert (state, parsed) == (MALFORMED, None) and problems


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"acts": []},
        {**answer(), "extra": []},
        answer(acts={}),
        answer(joins=None),
        answer(acts=[act("p4:2")]),
        answer(acts=[act("p4:3"), act("p4:2")]),
        answer(acts=[*answer()["acts"], act("p4:4")]),
        answer(acts=[{"act": "p4:2", "findings": []}, act("p4:3")]),
        answer(acts=[{"act": "p4:2", "departures": []}, act("p4:3")]),
        answer(acts=[{**act("p4:2"), "departures": {}}, act("p4:3")]),
        answer(acts=[{**act("p4:2"), "findings": {}}, act("p4:3")]),
        answer(acts=[{**act("p4:2"), "findings": None}, act("p4:3")]),
        answer(acts=[{**act("p4:2"), "note": ""}, act("p4:3")]),
        answer(acts=[{**act("p4:2"), "basis": []}, act("p4:3")]),
        answer(joins=[]),
        answer(joins=[{"acts": ["p4:1", "p3:9"], "continues": True, "departures": []}]),
        answer(joins=[{"acts": ["p3:9", "p4:1"], "continues": "yes", "departures": []}]),
        answer(joins=[{"acts": ["p3:9", "p4:1"], "departures": []}]),
        answer(joins=[{"acts": ["p3:9", "p4:1"], "continues": True, "departures": None}]),
    ],
)
def test_an_answer_off_the_grammar_or_the_plan_is_invalid_and_never_repaired(value):
    state, parsed, problems = parse(value)
    assert (state, parsed) == (ANSWER_INVALID, None) and problems
