"""The reconstruction answer grammar: one JSON object, checked against its call's plan.

    state, answer, problems = parse_reconstruction_answer(raw_text, call)

`call` is one call of `common.reconstruction.reconstruction_plan`,
`{page_ordinal, subjects, chains, context}`. The grammar:

    {"acts":  [{"act": "p3:2",
                "findings": [{"code": "out-of-sequence", "reason": "..."}, ...],
                "departures": [...]}, ...],
     "joins": [{"acts": ["p3:9", "p4:1"], "continues": true, "departures": [...]}, ...]}

* the reply is one bare JSON value, decoded as the page answer's is
  (`common.page_answer.decode_json_reply`); anything else is `malformed`;
* the object's keys are exactly `acts` and `joins`, both lists; each `acts` item
  is exactly `{act, findings, departures}` and each join exactly `{acts,
  continues, departures}`, `continues` a boolean and `findings` and `departures`
  lists;
* a finding is `{code}` or `{code, reason}`, its code one of `FINDING_CODES` and
  its reason a string;
* the `act` values are the call's subjects and the join `acts` lists its
  chains, each in the plan's order, so `joins` is `[]` when no chain is planned.

A reply that decodes but breaks any of these is `answer-invalid`. The answer is
never repaired: a failed answer leaves the page's reconstructions not made and
the diplomatic readings stand. What each departure says is checked where it is
applied (`common.reconstruction.apply_departures`), so one bad departure leaves
only its own act's reconstruction not made.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from common.page_answer import decode_json_reply

PARSED: Final = "parsed"
MALFORMED: Final = "malformed"
ANSWER_INVALID: Final = "answer-invalid"
PARSE_STATES: Final = frozenset({PARSED, MALFORMED, ANSWER_INVALID})

FINDING_CODES: Final = frozenset(
    {"cut-at-page-break", "incomplete", "out-of-sequence", "inconsistent", "other"}
)

_TOP_FIELDS: Final = frozenset({"acts", "joins"})
_ACT_FIELDS: Final = frozenset({"act", "findings", "departures"})
_JOIN_FIELDS: Final = frozenset({"acts", "continues", "departures"})
_FINDING_REQUIRED: Final = frozenset({"code"})
_FINDING_OPTIONAL: Final = frozenset({"reason"})


def _problem(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}


def _is_finding(finding: Any) -> bool:
    return (
        isinstance(finding, dict)
        and _FINDING_REQUIRED <= set(finding) <= _FINDING_REQUIRED | _FINDING_OPTIONAL
        and isinstance(finding["code"], str)
        and finding["code"] in FINDING_CODES
        and isinstance(finding.get("reason", ""), str)
    )


def _answer_problems(answer: Any, call: Mapping[str, Any]) -> list[dict[str, str]]:
    """Every way a decoded value departs from the grammar and its call's plan."""
    if not isinstance(answer, dict) or set(answer) != _TOP_FIELDS:
        return [_problem("top-fields", "the answer is not an object of exactly {acts, joins}")]
    acts, joins = answer["acts"], answer["joins"]
    if not isinstance(acts, list) or not isinstance(joins, list):
        return [_problem("not-lists", "acts and joins are not both lists")]
    problems = []
    for index, item in enumerate(acts):
        if (
            not isinstance(item, dict)
            or set(item) != _ACT_FIELDS
            or not isinstance(item["findings"], list)
            or not isinstance(item["departures"], list)
        ):
            problems.append(
                _problem(
                    "act-invalid",
                    f"acts[{index}] is not {{act, findings: [...], departures: [...]}}",
                )
            )
        elif not all(_is_finding(finding) for finding in item["findings"]):
            problems.append(
                _problem(
                    "finding-invalid",
                    f"acts[{index}] has a finding that is not {{code, reason?}} with a code "
                    f"in {sorted(FINDING_CODES)}",
                )
            )
    for index, item in enumerate(joins):
        if (
            not isinstance(item, dict)
            or set(item) != _JOIN_FIELDS
            or not isinstance(item["continues"], bool)
            or not isinstance(item["departures"], list)
        ):
            problems.append(
                _problem(
                    "join-invalid",
                    f"joins[{index}] is not {{acts, continues: true|false, departures: [...]}}",
                )
            )
    if problems:
        return problems
    if [item["act"] for item in acts] != list(call["subjects"]):
        problems.append(
            _problem("acts-not-planned", "the acts are not the call's subjects in plan order")
        )
    if [item["acts"] for item in joins] != [list(chain) for chain in call["chains"]]:
        problems.append(
            _problem("joins-not-planned", "the joins are not the call's chains in plan order")
        )
    return problems


def parse_reconstruction_answer(
    raw_text: Any, call: Mapping[str, Any]
) -> tuple[str, dict[str, Any] | None, list[dict[str, str]]]:
    """`(state, answer | None, problems)` for one reconstruction call's reply text."""
    value, problems = decode_json_reply(raw_text)
    if problems:
        return MALFORMED, None, problems
    problems = _answer_problems(value, call)
    if problems:
        return ANSWER_INVALID, None, problems
    return PARSED, value, []
