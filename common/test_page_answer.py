"""The page answer grammar: what is read, what is held, and the one repair."""

from __future__ import annotations

import copy
import json

import pytest

from common import page_answer

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
    "set_aside": [{"id": "C9", "reason": "empty unit"}],
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
        ("[" * 100_000 + "]" * 100_000, "too-deep"),
        ('{"acts": [' + "[" * 64 + "]" * 64 + '], "set_aside": []}', "too-deep"),
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


# Every JSON type, including the unhashable ones and one nested deeper than repr likes.
_ANY_JSON = [None, True, 0, 1.5, "", "act", [], ["act"], {}, {"kind": "act"}, [[[[[]]]]]]
_DEEP = "[" * 900 + "]" * 900


@pytest.mark.parametrize("value", _ANY_JSON, ids=repr)
@pytest.mark.parametrize(
    "place",
    [
        *(("act", field) for field in sorted(page_answer._ACT_FIELDS)),
        ("set_aside", "id"),
        ("set_aside", "reason"),
        ("top", "acts"),
        ("top", "set_aside"),
    ],
    ids=lambda place: ".".join(place),
)
def test_a_value_of_any_json_type_anywhere_is_read_or_held_never_an_error(place, value):
    where, field = place

    def change(answer):
        target = {"act": answer["acts"][0], "set_aside": answer["set_aside"][0], "top": answer}
        target[where][field] = value

    state, answer, problems = page_answer.parse_page_answer(_with(change))
    assert state in page_answer.PARSE_STATES
    assert (answer is None) == bool(problems)


@pytest.mark.parametrize("field", ["n", "kind", "label", "cites", "text", "id"])
def test_a_deeply_nested_value_is_named_by_its_type_not_quoted(field):
    act = {**GOOD["acts"][0]}
    act.pop("label")
    raw = json.dumps({"acts": [act], "set_aside": [{"id": "C9", "reason": "r"}]})
    target = '"C9"' if field == "id" else json.dumps(act[field] if field in act else "x")
    if field == "label":
        raw = raw.replace('"text":', '"label": "x", "text":', 1)
    raw = raw.replace(f'"{field}": {target}', f'"{field}": {_DEEP}', 1)
    state, _answer, problems = page_answer.parse_page_answer(raw)
    assert state == "malformed"
    assert all(len(problem["detail"]) < 200 for problem in problems)


def test_a_non_string_kind_is_malformed_not_a_crash():
    for kind in (["act"], {"act": 1}):
        raw = _with(lambda a, kind=kind: a["acts"][0].update(kind=kind))
        state, answer, problems = page_answer.parse_page_answer(raw)
        assert (state, answer, _codes(problems)) == ("malformed", None, ["kind-unknown"])


_SURROGATE = "\\ud800"


@pytest.mark.parametrize(
    "path",
    [
        ("acts", 0, "kind"),
        ("acts", 0, "label"),
        ("acts", 0, "cites", 0),
        ("acts", 0, "text"),
        ("set_aside", 0, "id"),
        ("set_aside", 0, "reason"),
    ],
)
def test_a_lone_surrogate_in_any_string_field_is_malformed_not_a_later_crash(path):
    answer = copy.deepcopy(GOOD)
    target = answer
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = "MARK"
    raw = json.dumps(answer).replace("MARK", f"x{_SURROGATE}y")
    assert _SURROGATE in raw
    state, parsed, problems = page_answer.parse_page_answer(raw)
    assert (state, parsed, _codes(problems)) == ("malformed", None, ["lone-surrogate"])


def test_a_lone_surrogate_in_a_key_or_an_unknown_field_is_malformed():
    for raw in (
        json.dumps(GOOD)[:-1] + f', "{_SURROGATE}": 1}}',
        json.dumps(GOOD).replace('"n": 2', f'"n": 2, "note": "{_SURROGATE}"'),
    ):
        state, _parsed, problems = page_answer.parse_page_answer(raw)
        assert (state, _codes(problems)) == ("malformed", ["lone-surrogate"])


def test_a_surrogate_pair_is_one_character_and_is_read():
    raw = json.dumps(GOOD).replace('"12"', '"\\ud83d\\ude00"')
    state, parsed, _problems = page_answer.parse_page_answer(raw)
    assert state == "parsed" and parsed["acts"][1]["text"] == "\U0001f600"


def test_fence_detection_reads_a_long_near_fence_in_linear_time():
    import time

    near = "```" + " " * 200_000 + "``"
    started = time.perf_counter()
    state, _parsed, problems = page_answer.parse_page_answer(near)
    assert time.perf_counter() - started < 1.0
    assert (state, _codes(problems)) == ("malformed", ["not-json"])


def test_nesting_is_counted_by_its_own_limit_and_never_inside_a_string():
    """64 deep reaches the grammar on every interpreter; brackets in a string are text."""
    limit = page_answer.MAX_NESTING_DEPTH
    at_limit = "[" * limit + "]" * limit
    assert _codes(page_answer.parse_page_answer(at_limit)[2]) == ["not-object"]
    quoted = _with(lambda a: a["acts"][0].update(text="[" * 1_000 + '\\"{'))
    assert page_answer.parse_page_answer(quoted)[0] == "parsed"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (None, "not-text"),
        ("```\n[]\n```", "fenced-answer"),
        ('{"a": 1, "a": 2}', "duplicate-key"),
        ("[" * 65 + "]" * 65, "too-deep"),
        ('["\\udc00"]', "lone-surrogate"),
        ("[] []", "content-outside-object"),
    ],
)
def test_the_shared_decoder_refuses_what_the_page_grammar_refuses(raw, code):
    value, problems = page_answer.decode_json_reply(raw)
    assert value is None and [problem["code"] for problem in problems] == [code]
    assert page_answer.parse_page_answer(raw)[2] == problems


def test_the_shared_decoder_returns_any_one_bare_json_value():
    assert page_answer.decode_json_reply(' [1, {"a": null}] \n') == ([1, {"a": None}], [])


def _page(*entries) -> str:
    """A page answer of `(kind, continues_from_previous_page, continues_to_next_page)` entries."""
    acts = [
        {
            "n": n,
            "kind": kind,
            "cites": [],
            "text": "x",
            "continues_from_previous_page": start,
            "continues_to_next_page": end,
        }
        for n, (kind, start, end) in enumerate(entries, 1)
    ]
    return json.dumps({"acts": acts, "set_aside": []})


ACT, OTHER = ("act", False, False), ("other", False, False)
START, END, BOTH = ("act", True, False), ("act", False, True), ("act", True, True)


@pytest.mark.parametrize(
    "raw",
    [
        _page(END, OTHER),
        _page(OTHER, START),
        _page(OTHER, BOTH, OTHER),
        _page(OTHER, START, OTHER, ACT, OTHER, END, OTHER),
        _page(("other", True, False), ACT),
        _page(ACT, ("other", False, True)),
        _page(("other", True, True)),
    ],
    ids=[
        "page-number-last",
        "heading-first",
        "both-around-one-act",
        "notes-between-acts",
        "first-entry-other",
        "last-entry-other",
        "no-act-page-first-and-last",
    ],
)
def test_a_flag_on_the_page_s_first_or_last_act_or_entry_is_read(raw):
    """The act edge is what a break joins; a flag on a first or last `other` entry is
    read too, and joins nothing (the Recensor notes it)."""
    assert page_answer.parse_page_answer(raw)[::2] == ("parsed", [])


@pytest.mark.parametrize(
    "raw",
    [
        _page(ACT, START),
        _page(END, ACT),
        _page(OTHER, END, OTHER, ACT),
        _page(ACT, ("other", True, False), ACT),
        _page(ACT, ("other", False, True), ACT),
        _page(("other", False, True), OTHER),
    ],
    ids=[
        "start-on-second-act",
        "end-on-first-of-two",
        "end-before-last-act",
        "start-on-middle-other",
        "end-on-middle-other",
        "no-act-page-end-on-first-of-two",
    ],
)
def test_a_flag_anywhere_else_keeps_the_answer_and_is_named_on_its_entry(raw):
    """A stray flag is one entry's statement: the page keeps every entry, and the flag is
    named by entry for the Recensor to judge, never a reason to drop the page."""
    state, answer, problems = page_answer.parse_page_answer(raw)
    assert (state, answer, problems) == ("parsed", json.loads(raw), [])
    assert page_answer.stray_continuation_flags(answer["acts"])


def test_stray_flags_are_named_by_entry_and_flag():
    acts = json.loads(_page(ACT, START, ("act", True, True), ACT, END))["acts"]
    assert page_answer.stray_continuation_flags(acts) == {
        1: ["continues_from_previous_page"],
        2: ["continues_from_previous_page", "continues_to_next_page"],
    }
    edges_only = json.loads(_page(OTHER, START, ACT, END, OTHER))["acts"]
    assert page_answer.stray_continuation_flags(edges_only) == {}


BARE = (
    '{\nacts: [{n: 1, kind: "act", label: null, cites: ["A1"], '
    'text: "Le 3 mai, kind: x, n: 2 {acts: y}", continues_from_previous_page: false, '
    "continues_to_next_page: false}],\n set_aside: []}"
)


def test_bare_grammar_keys_are_quoted_and_the_repair_recorded():
    """The cold73 shape: `{\nacts: [` with every grammar key bare. Text inside a
    string that looks like a key is never touched."""
    assert page_answer.parse_page_answer(BARE)[2][0]["code"] == "not-json"
    state, answer, problems, repairs = page_answer.parse_page_answer_repaired(BARE)
    assert (state, problems) == ("parsed", [])
    assert answer["acts"][0]["text"] == "Le 3 mai, kind: x, n: 2 {acts: y}"
    assert [(repair["code"], repair["keys"]) for repair in repairs] == [
        ("unquoted-keys-quoted", 9)
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "{acts: [], set_aside: [], }",
        "{acts: [], set_aside: [], 'x': 1}",
        "{acts: [], colour: []}",
        "```json\n{acts: [], set_aside: []}\n```",
        "{acts: [], set_aside: []} trailing",
    ],
    ids=["trailing-comma", "single-quotes", "unknown-bare-key", "fenced", "trailing-prose"],
)
def test_the_repair_quotes_grammar_keys_and_nothing_else(raw):
    """Any other departure stays the reply's own problem, unrepaired and unrecorded."""
    state, answer, problems, repairs = page_answer.parse_page_answer_repaired(raw)
    assert (state, answer, repairs) == ("malformed", None, [])
    assert problems == page_answer.parse_page_answer(raw)[2]


def test_a_repaired_reply_still_meets_the_grammar_or_is_held():
    raw = '{acts: [{n: 2, kind: "act", cites: [], text: "x", continues_from_previous_page: false, continues_to_next_page: false}], set_aside: []}'
    state, answer, problems, repairs = page_answer.parse_page_answer_repaired(raw)
    assert (state, answer, _codes(problems)) == ("malformed", None, ["n-not-contiguous"])
    assert _codes(repairs) == ["unquoted-keys-quoted"]


def test_a_well_formed_reply_is_never_repaired():
    raw = json.dumps(GOOD)
    assert page_answer.parse_page_answer_repaired(raw) == ("parsed", GOOD, [], [])
