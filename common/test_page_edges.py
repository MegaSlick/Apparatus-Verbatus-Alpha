"""Page edges and break chains: the pure core every page-break rule derives from."""

import json

from common.page_answer import parse_page_answer
from common.page_edges import (
    act_entries_by_page,
    agreed_breaks,
    break_chains,
    page_edges,
    whole_page_entries,
)
from common.page_review import page_breaks


def entry(page, n, *, kind="act", start=False, end=False, attempt=1):
    return {
        "act_key": f"p{page}:{n}",
        "page_ordinal": page,
        "n": n,
        "kind": kind,
        "continues_from_previous_page": start,
        "continues_to_next_page": end,
        "reading_attempt": attempt,
    }


def keys(chains):
    return [[piece["act_key"] for piece in chain] for chain in chains]


def test_a_page_s_edges_are_its_first_and_last_act_entries_skipping_other_entries():
    entries = [entry(1, 3), entry(1, 1, kind="other"), entry(1, 2), entry(1, 4, kind="other")]
    first, last = page_edges(entries)[1]
    assert (first["act_key"], last["act_key"]) == ("p1:2", "p1:3")


def test_a_page_with_no_reading_has_no_edge():
    assert act_entries_by_page([entry(1, None)]) == {}


def test_a_re_asks_entries_are_never_whole_page_entries():
    entries = [entry(1, 1), entry(1, 1, attempt=2)]
    assert whole_page_entries(entries) == [entries[0]]


def test_an_operator_re_read_is_a_whole_page_reading_whose_entries_are_edges():
    """A page a person had read again stands on that reading; its re-ask's entries never do."""
    entries = [entry(1, 1, attempt=3, end=True), entry(2, 1, attempt=2)]
    assert whole_page_entries(entries) == [entries[0]]


def test_a_break_is_agreed_only_when_both_sides_flag_it():
    both = [entry(1, 1, end=True), entry(2, 1, start=True)]
    one = [entry(1, 1, end=True), entry(2, 1)]
    other = [entry(1, 1), entry(2, 1, start=True)]
    assert [(a["act_key"], b["act_key"]) for a, b in agreed_breaks(both)] == [("p1:1", "p2:1")]
    assert agreed_breaks(one) == agreed_breaks(other) == []


def test_a_break_needs_the_next_page_to_be_read():
    assert agreed_breaks([entry(1, 1, end=True), entry(3, 1, start=True)]) == []


def test_an_act_across_three_pages_is_one_chain():
    entries = [
        entry(1, 1),
        entry(1, 2, end=True),
        entry(2, 1, start=True, end=True),
        entry(3, 1, start=True),
        entry(3, 2),
    ]
    assert keys(break_chains(entries)) == [["p1:2", "p2:1", "p3:1"]]


def test_a_middle_page_with_two_acts_splits_the_chains():
    entries = [
        entry(1, 1, end=True),
        entry(2, 1, start=True),
        entry(2, 2, end=True),
        entry(3, 1, start=True),
    ]
    assert keys(break_chains(entries)) == [["p1:1", "p2:1"], ["p2:2", "p3:1"]]


def test_an_other_entry_at_the_edge_does_not_hide_the_act_edge():
    entries = [
        entry(1, 1, end=True),
        entry(1, 2, kind="other"),
        entry(2, 1, kind="other"),
        entry(2, 2, start=True),
    ]
    assert keys(break_chains(entries)) == [["p1:1", "p2:2"]]


def _read_page(ordinal, raw):
    """A parsed page answer's entries as the rows the join reads."""
    state, answer, problems = parse_page_answer(raw)
    assert (state, problems) == ("parsed", [])
    return [
        {**entry(ordinal, act["n"], kind=act["kind"]), "act_id": f"a{ordinal}.{act['n']}", **act}
        for act in answer["entries"]
    ]


def _answer(*entries):
    return json.dumps(
        {
            "entries": [
                {"n": n, "kind": kind, "cites": [], "text": "x"}
                | {"continues_from_previous_page": start, "continues_to_next_page": end}
                for n, (kind, start, end) in enumerate(entries, 1)
            ],
            "set_aside": [],
        }
    )


def test_flags_the_answer_grammar_reads_reach_the_join_past_headings_and_page_numbers():
    """The grammar and the join share one edge, so a flag the grammar reads is the one joined."""
    page_1 = _read_page(
        1, _answer(("act", False, False), ("act", False, True), ("other", False, False))
    )
    page_2 = _read_page(2, _answer(("other", False, False), ("act", True, False)))
    assert keys(break_chains(page_1 + page_2)) == [["p1:2", "p2:2"]]


def test_a_flag_on_one_side_of_a_break_joins_nothing_and_is_recorded_unagreed():
    page_1 = _read_page(1, _answer(("act", False, True), ("other", False, False)))
    page_2 = _read_page(2, _answer(("other", False, False), ("act", False, False)))
    assert break_chains(page_1 + page_2) == []
    [(subject, link)] = page_breaks({1: "pg_1", 2: "pg_2"}, page_1 + page_2)
    assert subject == "page-break:1:2"
    assert (link["from_act_key"], link["to_act_key"], link["agreed"]) == ("p1:1", "p2:2", False)
