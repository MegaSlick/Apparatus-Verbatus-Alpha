"""Page edges and the page breaks between them: which acts could continue across a page.

A page's edges are its first and last `act` entries in answer order (`n`); an
`other` entry never crosses a break. A break between page p and page p+1 has
two sides, the last `act` entry of p and the first of p+1, and each side's
answer flag says whether it believes an act crosses there. Pure and model-free,
so every stage that reasons about a break derives the same sides.

Entries are mappings carrying at least `page_ordinal`, `n`, `kind`,
`continues_from_previous_page` and `continues_to_next_page`; an entry whose `n`
is `None` stands for a page with no reading and has no edge. The page-wide
functions take records whose `kind` is already the act class (`act`); only
`edge_acts` reads an answer's own entries, where an `instrument` counts as an act.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from common.page_types import is_act_class

# The attempts a page reading is: the first reading, at most one re-ask (asked
# about ids alone, so its entries are never a page's edges), and from 3 on each
# operator re-read a person asked for (`common.page_path`).
FIRST_READING: Final = 1
REASK_READING: Final = 2
OPERATOR_REREAD_FIRST: Final = 3

Entry = Mapping[str, Any]


def whole_page_entries(entries: Iterable[Entry]) -> list[Entry]:
    """Every entry not read on a re-ask: the whole-page entries a page's edges are of.

    It drops the re-ask's entries (asked about ids alone) and rows that stand
    for no entry, and nothing else, so callers pass current rows only: the
    page-read denominator's, which count each page's current whole-page
    reading (its first, or the operator re-read that superseded it) and never a
    superseded one.
    """
    return [
        entry
        for entry in entries
        if entry["reading_attempt"] is not None and entry["reading_attempt"] != REASK_READING
    ]


def act_entries_by_page(entries: Iterable[Entry]) -> dict[int, list[Entry]]:
    """Each page's `act` entries in answer order: the only entries a page break can join."""
    by_page: dict[int, list[Entry]] = {}
    for entry in entries:
        if entry["n"] is not None and entry["kind"] == "act":
            by_page.setdefault(entry["page_ordinal"], []).append(entry)
    return {ordinal: sorted(acts, key=lambda act: act["n"]) for ordinal, acts in by_page.items()}


def edge_acts(page: Sequence[Mapping[str, Any]]) -> tuple[Any, Any] | None:
    """One page's continuation edges: `(first act entry, last act entry)`, or `None` with no act.

    `page` is one page's entries in answer order. Only the first `act` entry may
    say it continues from the page before and only the last that it runs onto the
    page after; a heading, page number or other `other` entry around or between
    them is never an edge, and a page with no `act` entry has none. An answer's
    `instrument` entry is of the act class (`common.page_types`) and is an edge
    like an `act`; every record after the answer carries the class itself, which
    is why `act_entries_by_page` and `page_edges` take records whose `kind` is the
    act class. The answer grammar and the page-break join both take a page's edges
    from here.
    """
    acts = [entry for entry in page if is_act_class(entry.get("kind"))]
    return (acts[0], acts[-1]) if acts else None


def page_edges(entries: Iterable[Entry]) -> dict[int, tuple[Entry, Entry]]:
    """`{page_ordinal: (first act entry, last act entry)}` for each page holding an act."""
    edges = {ordinal: edge_acts(acts) for ordinal, acts in act_entries_by_page(entries).items()}
    return {ordinal: edge for ordinal, edge in edges.items() if edge is not None}


def agreed_breaks(entries: Iterable[Entry]) -> list[tuple[Entry, Entry]]:
    """`(last act of p, first act of p+1)` for every break both sides flag, in page order."""
    edges = page_edges(entries)
    return [
        (edges[left][1], edges[left + 1][0])
        for left in sorted(edges)
        if left + 1 in edges
        and edges[left][1]["continues_to_next_page"] is True
        and edges[left + 1][0]["continues_from_previous_page"] is True
    ]


def break_chains(entries: Iterable[Entry]) -> list[list[Entry]]:
    """Each act that crosses one or more agreed breaks, as its pieces in page order.

    A page whose one act entry both takes an act over from the page before and hands
    it on to the page after is a middle piece, so an act across three pages is one
    chain of three pieces.
    """
    chains: list[list[Entry]] = []
    for last, first in agreed_breaks(entries):
        if chains and chains[-1][-1] is last:
            chains[-1].append(first)
        else:
            chains.append([last, first])
    return chains
