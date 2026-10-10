"""Which ids a page reading left unaccounted for, and so what its one re-ask names.

A page read whole (`common/page_path.py`) is accounted rule by rule
(`common/page_accounting.py`). Three findings say the reading left something on
the page it was shown unread: a witness unit it neither cited nor set aside
(`unaccounted-witness-unit`), a detected line outside every entry's region
(`unread-line`) and a detector record outside every region
(`record-not-read`). For those, and only those, the Perlector may be asked
once more, about exactly those ids. The re-ask adds entries; the first
reading stands as given and is never replaced or chosen against, since a
second call is a second draw and picking between draws is best-of-two.

Every other finding is never re-asked (`NEVER`): a malformed, cut-off or
failed answer is held, never re-rolled; a set-aside, merged, duplicated or
misread entry is the reading's own claim for a human to judge; a witness text
the reading does not hold selects on agreement with the witnesses; unread ink
has no id to name; and what was not measured stays not measured.

`reask_plan` is the one plan: a pure function of a page's sealed feed, first
reading and accounting, which stage 4 runs after the page's first accounting
and any reader that counts the page can run again, so the two cannot disagree
about which pages were re-asked. `render_reask` gives what
the re-ask shows beyond the first request, as data; its wording is
`common.page_prompt.page_reask_prompt`'s.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from common.contracts.errors import ContractError
from common.page_accounting import (
    ANSWER_BASIS_FIRST,
    HOLD_CODES,
    NOT_MEASURED_CODES,
    PARSED,
    REASK_DUPLICATE,
    REASK_SET_ASIDE,
    RECORD_NOT_READ,
    UNACCOUNTED_WITNESS_UNIT,
    UNREAD_LINE,
    PageAccountingPolicy,
    feed_candidates,
    feed_items,
    finished_on_stop,
    id_key,
    is_inside,
    validate_answer,
)
from common.page_edges import REASK_READING

# The findings a re-ask is asked about, and every other finding code.
RE_ASKABLE: Final = frozenset({UNACCOUNTED_WITNESS_UNIT, UNREAD_LINE, RECORD_NOT_READ})
NEVER: Final = (HOLD_CODES | NOT_MEASURED_CODES) - RE_ASKABLE


def reask_budget(recovery_policy: Mapping[str, Any]) -> int:
    """The sealed `[budget] page_level_reread`: 0 turns the re-ask off, 1 allows one.

    `recovery_policy` is `common.recovery.load_recovery_policy`'s record, which has
    already refused any other value.
    """
    return recovery_policy["page_level_reread"]


def reask_plan(
    reading: Mapping[str, Any],
    accounting: Mapping[str, Any],
    feed: Mapping[str, Any],
    *,
    budget: int,
    policy: PageAccountingPolicy,
) -> list[dict[str, Any]]:
    """The ids a page's re-ask names, `[{id, code, box_1000}]`, or `[]` for no re-ask.

    `reading` is the page's first `page-reading` payload, `accounting` that
    reading's `page-accounting` payload and `feed` the page's feed; `budget`
    is `reask_budget`'s and `policy` the sealed page-accounting policy. There
    is no re-ask with the budget at 0, or unless the first reading is a
    parsed answer, read, that finished on `stop`
    (`page_accounting.finished_on_stop`, the test rule (a) reads; an answer
    with no entry included). Otherwise the named ids are every distinct finding of the
    three `RE_ASKABLE` codes, sorted by id and code, whose id is one the feed
    showed and places by a box (`page_accounting.placement_boxes`) -- so an
    unboxed witness unit, one whose text is too short to vouch for its box, a
    witness shown flat, a Surya block and a detection the feed did not show
    are never named -- and, for a
    witness unit, that lies less than the sealed share inside the first
    reading's regions, `act` and `other` alike: a unit inside an entry's
    region was read and not cited, which the page holds rather than asks
    again.
    """
    if accounting.get("answer_basis") != ANSWER_BASIS_FIRST:
        raise ContractError("a re-ask is planned only from a first reading's accounting")
    if (
        budget == 0
        or reading["parse_state"] != PARSED
        or reading["disposition"] != "read"
        or not finished_on_stop(reading)
    ):
        return []
    candidates = feed_candidates(feed, policy)
    # Each entry's region is the list of its placing boxes; "inside" reads their union.
    regions = [
        box
        for entry in validate_answer(reading["answer"], candidates, policy=policy)["entries"]
        for box in entry["region_boxes_px"]
    ]
    found: set[tuple[str, str]] = set()
    for rule in accounting["rules"].values():
        for finding in rule["findings"]:
            code, identifier = finding["code"], finding.get("id")
            if code not in RE_ASKABLE or candidates.get(identifier) is None:
                continue
            if code == UNACCOUNTED_WITNESS_UNIT and is_inside(
                candidates[identifier], regions, policy
            ):
                continue
            found.add((identifier, code))
    # Each id's box on the 0-1000 grid, as the prompt showed it.
    boxes = {item["id"]: item["box_1000"] for _kind, item in feed_items(feed)}
    return [
        {"id": identifier, "code": code, "box_1000": boxes[identifier]}
        for identifier, code in sorted(found, key=lambda item: (id_key(item[0]), item[1]))
    ]


def named_ids(named: Sequence[Mapping[str, Any]]) -> list[str]:
    """The distinct ids a plan names, in its order: what the re-ask may cite."""
    return list(dict.fromkeys(item["id"] for item in named))


def render_reask(
    first_answer: Mapping[str, Any], named: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """What the re-ask shows beyond the first request, as data.

    The first reading's entries as `{n, kind, label, cites}` only -- never
    their text, so the re-ask cannot copy it -- and the named ids with their
    boxes. The feed and the page images are the first request's, unchanged.
    """
    return {
        "prior_entries": [
            {"n": act["n"], "kind": act["kind"], "label": act.get("label"), "cites": act["cites"]}
            for act in first_answer["entries"]
        ],
        "named": [dict(item) for item in named],
    }


def reask_outcome(named: Sequence[Mapping[str, Any]], accounting: Mapping[str, Any]) -> dict:
    """What a page's re-ask did about the ids it named, from the page's last accounting.

    `named` is the re-ask's `reask.named` and `accounting` the combined
    `page-accounting` payload. Each named id is `unread` while a re-askable
    finding of the last accounting still names it, else `set_aside` when the
    re-ask set it aside (rule (j), `reask-set-aside`), else `cleared` when a
    re-ask entry rule (j) does not hold accounts for it -- cites it, or holds
    its line or record in its region -- else `held`: the only re-ask entries
    that account for it are ones rule (j) holds. Each list keeps `named_ids`'
    order. `duplicate` is the combined number of each re-ask entry rule (j)
    holds as a first-reading entry's duplicate.
    """
    ids = named_ids(named)
    findings = [finding for rule in accounting["rules"].values() for finding in rule["findings"]]
    unread = {f.get("id") for f in findings if f["code"] in RE_ASKABLE}
    set_aside = {f["id"] for f in findings if f["code"] == REASK_SET_ASIDE} - unread
    held_entries = {f["n"] for f in accounting["rules"]["j"]["findings"] if "n" in f}
    standing = {
        entry["n"]
        for entry in accounting["entries"]
        if entry["reading_attempt"] == REASK_READING and entry["n"] not in held_entries
    }
    accounted_by: dict[str, set[int]] = {}
    for row in accounting["units"]:
        accounted_by.setdefault(row["id"], set()).update(row["by"])
    for row in accounting["lines"]:
        accounted_by.setdefault(row["id"], set()).update(row["inside"])
    for row in accounting["records"] or []:
        accounted_by.setdefault(row["id"], set()).update(row["act"] + row["other"])
    cleared = {i for i in ids if accounted_by.get(i, set()) & standing} - unread - set_aside
    return {
        "named": ids,
        "cleared": [i for i in ids if i in cleared],
        "set_aside": [i for i in ids if i in set_aside],
        "held": [i for i in ids if i not in unread | set_aside | cleared],
        "unread": [i for i in ids if i in unread],
        "duplicate": sorted({f["n"] for f in findings if f["code"] == REASK_DUPLICATE}),
    }
