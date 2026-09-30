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

Entry = Mapping[str, Any]


def first_attempt_entries(entries: Iterable[Entry]) -> list[Entry]:
    """The entries of each page's first reading attempt: the reading a page's edges are of."""
    return [entry for entry in entries if entry["reading_attempt"] == FIRST_READING_ATTEMPT]


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


def break_sides(
    entries: Iterable[Entry], first_ordinal: int, last_ordinal: int
) -> list[tuple[int, Entry | None, Entry | None]]:
    """`(left ordinal, last act of left, first act of left + 1)` for every break touching
    pages `first_ordinal..last_ordinal`, including the one before the first page and the
    one after the last; a side with no act entry is `None`."""
    edges = page_edges(entries)
    sides = []
    for left in range(first_ordinal - 1, last_ordinal + 1):
        last = edges[left][1] if left in edges else None
        first = edges[left + 1][0] if left + 1 in edges else None
        sides.append((left, last, first))
    return sides


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
