"""Page edges and the page breaks between them: which acts could continue across a page.

A page's edges are its first and last `act` entries in answer order (`n`); an
`other` entry never crosses a break. A break between page p and page p+1 has
two sides, the last `act` entry of p and the first of p+1, and each side's
answer flag says whether it believes an act crosses there. Pure and model-free,
so every stage that reasons about a break derives the same sides.

Entries are mappings carrying at least `page_ordinal`, `n`, `kind`,
`continues_from_previous_page` and `continues_to_next_page`; an entry whose `n`
is `None` stands for a page with no reading and has no edge.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

FIRST_READING_ATTEMPT: Final = 1
# The one re-ask of a page (`common.page_path.REASK_READING`): asked about ids
# alone, so its entries are never a page's edges.
REASK_ATTEMPT: Final = 2

Entry = Mapping[str, Any]


def first_attempt_entries(entries: Iterable[Entry]) -> list[Entry]:
    """The entries of each page's current whole-page reading: the reading a page's edges are of.

    That is the first reading, or an operator re-read (attempt 3 on) that
    superseded it; never the re-ask's.
    """
    return [
        entry
        for entry in entries
        if entry["reading_attempt"] is not None and entry["reading_attempt"] != REASK_ATTEMPT
    ]


def act_entries_by_page(entries: Iterable[Entry]) -> dict[int, list[Entry]]:
    """Each page's `act` entries in answer order: the only entries a page break can join."""
    by_page: dict[int, list[Entry]] = {}
    for entry in entries:
        if entry["n"] is not None and entry["kind"] == "act":
            by_page.setdefault(entry["page_ordinal"], []).append(entry)
    return {ordinal: sorted(acts, key=lambda act: act["n"]) for ordinal, acts in by_page.items()}


def page_edges(entries: Iterable[Entry]) -> dict[int, tuple[Entry, Entry]]:
    """`{page_ordinal: (first act entry, last act entry)}` for each page holding an act."""
    return {ordinal: (acts[0], acts[-1]) for ordinal, acts in act_entries_by_page(entries).items()}


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
