"""The reconstruction answer grammar: strict at the answer level, never repaired."""

import json

import pytest

from common.reconstruction_answer import (
    ANSWER_INVALID,
    GRAMMAR,
    MALFORMED,
    PARSED,
    parse_reconstruction_answer,
)

CALL = {"page_ordinal": 4, "subjects": ["p4:2", "p4:3"], "chains": [["p3:9", "p4:1"]]}
DEPARTURE = {"diplomatic": "Jan", "reconstruction": "Jean", "basis": ["p4:3"], "reason": "r"}


def answer(**changes):
    value = {
        "acts": [
            {"act": "p4:2", "departures": [DEPARTURE]},
            {"act": "p4:3", "departures": []},
        ],
        "joins": [{"acts": ["p3:9", "p4:1"], "continues": True, "departures": []}],
    }
    value.update(changes)
    return value


def parse(value):
    return parse_reconstruction_answer(json.dumps(value), CALL)


def test_the_grammar_is_named():
    assert GRAMMAR == "verbatus-reconstruction-answer.v1"


def test_a_planned_answer_parses_as_given():
    state, parsed, problems = parse(answer())
    assert (state, parsed, problems) == (PARSED, answer(), [])


def test_an_empty_call_takes_an_empty_answer():
    state, _, _ = parse_reconstruction_answer(
        '{"acts": [], "joins": []}', {"page_ordinal": 1, "subjects": [], "chains": []}
    )
    assert state == PARSED


def test_a_departure_s_content_is_left_to_apply():
    bad = answer(
        acts=[{"act": "p4:2", "departures": ["nonsense"]}, {"act": "p4:3", "departures": []}]
    )
    assert parse(bad)[0] == PARSED


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
        answer(acts=[{"act": "p4:2", "departures": []}]),
        answer(acts=[{"act": "p4:3", "departures": []}, {"act": "p4:2", "departures": []}]),
        answer(acts=[*answer()["acts"], {"act": "p4:4", "departures": []}]),
        answer(acts=[{"act": "p4:2"}, {"act": "p4:3", "departures": []}]),
        answer(acts=[{"act": "p4:2", "departures": {}}, {"act": "p4:3", "departures": []}]),
        answer(
            acts=[{"act": "p4:2", "departures": [], "note": ""}, {"act": "p4:3", "departures": []}]
        ),
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
