"""Page edges and break chains: the pure core every page-break rule derives from."""

from common.page_edges import (
    act_entries_by_page,
    agreed_breaks,
    break_chains,
    break_sides,
    first_attempt_entries,
    page_edges,
)


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


def test_only_the_first_reading_attempt_counts():
    entries = [entry(1, 1), entry(1, 1, attempt=2)]
    assert first_attempt_entries(entries) == [entries[0]]


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


def test_break_sides_name_every_break_touching_the_pages_with_null_sides():
    sides = break_sides([entry(1, 1), entry(2, 1)], 1, 2)
    assert [(left, a and a["act_key"], b and b["act_key"]) for left, a, b in sides] == [
        (0, None, "p1:1"),
        (1, "p1:1", "p2:1"),
        (2, "p2:1", None),
    ]
