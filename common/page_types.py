"""Page types and entry kinds: what a page is, what each of its entries is, and what follows.

A page reading names its page's type and each entry's kind (`common.page_answer`, the
`entries` grammar). Both are closed lists:

    page types   register-acts, index, table, ledger, instrument, prose, blank
    entry kinds  act, index-row, table-row, ledger-entry, instrument, paragraph, other

An *act* (GLOSSARY) is one registered act; an *instrument* one notarial act or contract,
which counts like an act for loss. Every other kind is read, placed and accounted for
like an act but is not one: an index row points to an act elsewhere, a table row or a
ledger entry is a row of its own census, a paragraph is running text. So each kind has
an *act class*, the two-valued `kind` every record after the answer carries:

    act_class(kind) == "act"    for act and instrument
    act_class(kind) == "other"  for every other kind

and the records keep the kind itself as `entry_kind` where the answer named one. The
class is what the act census, the page breaks, the record-detector rule and the
Coniector read; the kind is what the length signal and the duplicate rule read, and what
the export writes.

An answer in the older `acts` grammar names neither: its kinds are `act` and `other`
already, and its page type is not stated (`None`). Every check then applies exactly as
it did before page types existed; nothing here relaxes a check on a page whose type the
reading did not state.

Which checks apply to a page of a stated type is `applicability`; the page accounting
records it with the type, and a check that does not apply keeps its findings on the
record without holding. Whether a check that applies holds or only flags the page is
the review configuration's decision, not this module's.

The model-free cross-check (`type_facts`, `type_agreement`) sets the stated type beside
what the detectors found: Surya's `Table` blocks, the witnesses' `Table` units and the
record detector's count. It is a fact on the page accounting, recorded and never held
here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

# --- the vocabularies ------------------------------------------------------------------

REGISTER_ACTS: Final = "register-acts"
INDEX: Final = "index"
TABLE: Final = "table"
LEDGER: Final = "ledger"
INSTRUMENT_PAGE: Final = "instrument"
PROSE: Final = "prose"
BLANK: Final = "blank"
PAGE_TYPES: Final = (REGISTER_ACTS, INDEX, TABLE, LEDGER, INSTRUMENT_PAGE, PROSE, BLANK)

ACT: Final = "act"
INDEX_ROW: Final = "index-row"
TABLE_ROW: Final = "table-row"
LEDGER_ENTRY: Final = "ledger-entry"
INSTRUMENT: Final = "instrument"
PARAGRAPH: Final = "paragraph"
OTHER: Final = "other"
ENTRY_KINDS: Final = (ACT, INDEX_ROW, TABLE_ROW, LEDGER_ENTRY, INSTRUMENT, PARAGRAPH, OTHER)

# How the page is written, named beside its type: the record detector was built for
# handwritten register acts, so whether its rule applies turns on this.
WRITINGS: Final = ("handwritten", "typed", "printed", "mixed")
HAND_WRITINGS: Final = frozenset({"handwritten", "mixed"})

# The two act classes every record after the answer carries as `kind`.
ACT_CLASS: Final = "act"
OTHER_CLASS: Final = "other"
ACT_CLASSES: Final = frozenset({ACT_CLASS, OTHER_CLASS})
# The kinds that count as acts for "never lose an act", continue across a page break
# and are reconstructed by the Coniector.
ACT_CLASS_KINDS: Final = frozenset({ACT, INSTRUMENT})
# The kinds that are rows of a list: written to `rows.jsonl`, never acts, and placed by
# their own lines where a witness gave their whole table as one unit.
ROW_KINDS: Final = frozenset({INDEX_ROW, TABLE_ROW, LEDGER_ENTRY})
# The kinds the truncation length signal is calibrated for: it was measured on register
# acts, and a 20-character index row in a row-sized box is not "short for its region".
LENGTH_SIGNAL_KINDS: Final = frozenset({ACT, INSTRUMENT})

# The kinds a page of each type is expected to hold beside `other`; not enforced, only
# recorded when an entry's kind is unexpected for its page (`kind_agreement`).
EXPECTED_KINDS: Final[Mapping[str, frozenset[str]]] = {
    REGISTER_ACTS: frozenset({ACT}),
    INDEX: frozenset({INDEX_ROW}),
    TABLE: frozenset({TABLE_ROW}),
    LEDGER: frozenset({LEDGER_ENTRY, TABLE_ROW}),
    INSTRUMENT_PAGE: frozenset({INSTRUMENT, TABLE_ROW}),
    PROSE: frozenset({PARAGRAPH}),
    BLANK: frozenset(),
}

# Not stated: an answer in the `acts` grammar.
NOT_STATED: Final = None


def act_class(kind: str) -> str:
    """The act class of an entry kind: `act` for an act or instrument, else `other`."""
    if kind not in ENTRY_KINDS:
        raise ValueError(f"{kind!r} is not an entry kind")
    return ACT_CLASS if kind in ACT_CLASS_KINDS else OTHER_CLASS


def is_act_class(kind: Any) -> bool:
    """Whether a kind as an answer or a record gives it counts as an act."""
    return kind in ACT_CLASS_KINDS


def length_signal_applies(entry_kind: str) -> bool:
    """Whether the act-calibrated truncation length signal judges an entry of this kind."""
    return entry_kind in LENGTH_SIGNAL_KINDS


# --- which checks apply per page type -------------------------------------------------

# The page accounting's rule (i): every detector record inside one act region, and a
# page of acts with no record at all held. Teklia's record detector finds acts on
# handwritten registers and nothing on indexes, lists, typed copies or most forms.
RECORD_DETECTOR_RULE: Final = "i"


def applicability(page_type: str | None, writing: str | None) -> dict[str, dict[str, Any]]:
    """`{rule: {"applies": bool, "reason": str}}` for the rules a page type can switch off.

    Only rule (i) depends on the page type today: it applies on a page whose type is not
    stated (as before page types) and on handwritten or mixed register-acts; on every
    other stated page it does not apply, and its findings are recorded, not held. Every
    other rule applies to every page; whether one holds or flags is review configuration.
    """
    if page_type is NOT_STATED:
        applies, reason = True, "page type not stated: every check applies"
    elif page_type == REGISTER_ACTS and writing in HAND_WRITINGS:
        applies, reason = True, f"{writing} register acts: the record detector's own page type"
    elif page_type == REGISTER_ACTS:
        applies, reason = (
            False,
            f"{writing} register acts: the record detector was built for handwriting",
        )
    else:
        applies, reason = False, f"{page_type} page: the record detector finds register acts"
    return {RECORD_DETECTOR_RULE: {"applies": applies, "reason": reason}}


# --- the model-free cross-check ---------------------------------------------------------

TABLE_LABEL: Final = "Table"


def type_facts(feed: Mapping[str, Any], record_count: int | None) -> dict[str, Any]:
    """What the detectors found that bears on the page type, as the feed shows it.

    `surya_table_blocks`: Surya blocks labelled `Table` (`None` when no block is shown);
    `witness_table_units`: per shown witness chair (its letter where the feed names no
    chair), its units labelled `Table` (a
    whole-page layout reader such as Chandra gives a table as one such unit);
    `detector_records`: the record detector's records on the page (`None` when it did
    not run or failed for the page).
    """
    surya = feed.get("surya")
    blocks = None if not surya or not surya.get("blocks") else surya["blocks"]
    return {
        "surya_table_blocks": None
        if blocks is None
        else sum(1 for block in blocks if block.get("label") == TABLE_LABEL),
        "witness_table_units": {
            row.get("chair") or row["letter"]: sum(
                1 for unit in row["units"] if unit.get("label") == TABLE_LABEL
            )
            for row in feed["witnesses"]
            if row["units"]
        },
        "detector_records": record_count,
    }


def _table_evidence(facts: Mapping[str, Any]) -> bool | None:
    seen = [facts["surya_table_blocks"], *facts["witness_table_units"].values()]
    measured = [count for count in seen if count is not None]
    if not measured:
        return None
    return any(count > 0 for count in measured)


def type_agreement(
    page_type: str | None, writing: str | None, facts: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Each model-free check of the stated type: `{check, agrees, detail}`, `agrees` None
    when the facts to judge it were not measured. Nothing for an unstated type.

    - `table-evidence`: a page of rows (index, table, ledger) shows a `Table` block or
      unit; a page of register acts shows none. (On the bake-off set Surya tagged a
      `Table` on 18 of 20 index and table pages and on none of 47 act pages.)
    - `detector-records`: handwritten register acts have records; an index or table
      page has none (the detector found none on any index page).
    """
    if page_type is NOT_STATED:
        return []
    checks = []
    table = _table_evidence(facts)
    if page_type in (INDEX, TABLE, LEDGER):
        checks.append(
            {
                "check": "table-evidence",
                "agrees": table,
                "detail": f"a {page_type} page; a Table block or unit "
                + ("was found" if table else "was not found" if table is False else "unmeasured"),
            }
        )
    elif page_type == REGISTER_ACTS:
        checks.append(
            {
                "check": "table-evidence",
                "agrees": None if table is None else not table,
                "detail": "a register-acts page; a Table block or unit "
                + ("was found" if table else "was not found" if table is False else "unmeasured"),
            }
        )
    records = facts["detector_records"]
    if page_type == REGISTER_ACTS and writing in HAND_WRITINGS:
        checks.append(
            {
                "check": "detector-records",
                "agrees": None if records is None else records > 0,
                "detail": f"handwritten register acts; the record detector found {records}",
            }
        )
    elif page_type in (INDEX, TABLE):
        checks.append(
            {
                "check": "detector-records",
                "agrees": None if records is None else records == 0,
                "detail": f"a {page_type} page; the record detector found {records}",
            }
        )
    return checks


def kind_agreement(page_type: str | None, entry_kinds: Sequence[str]) -> dict[str, Any] | None:
    """The entries whose kind a page of the stated type is not expected to hold.

    `None` for an unstated type. `other` is expected on every page.
    """
    if page_type is NOT_STATED:
        return None
    expected = EXPECTED_KINDS[page_type] | {OTHER}
    unexpected = sorted({kind for kind in entry_kinds if kind not in expected})
    return {"agrees": not unexpected, "unexpected_kinds": unexpected}


# --- the rows export -------------------------------------------------------------------

ROWS_SCHEMA: Final = "armarium-row.v1"


def row_record(
    perlectio: Mapping[str, Any],
    *,
    page_type: str | None,
    act_key: str,
    act_id: str,
    review: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """One `rows.jsonl` line for an entry of a row kind, or `None` for any other entry.

    The row is the entry's diplomatic text as read, with its page, number and kind; no
    cells, since splitting a row by code would invent structure the reading did not
    give. `review` is what the review made of it (`{outcome, hold_codes}`) when the
    caller exports one, else absent.
    """
    entry_kind = perlectio.get("entry_kind")
    if entry_kind not in ROW_KINDS:
        return None
    row: dict[str, Any] = {
        "schema": ROWS_SCHEMA,
        "act_key": act_key,
        "act_id": act_id,
        "page_id": perlectio["page_id"],
        "page_ordinal": perlectio["page_ordinal"],
        "page_type": page_type,
        "n": perlectio["n"],
        "entry_kind": entry_kind,
        "label": perlectio["label"],
        "text": perlectio["text"],
        "uncertain_spans": perlectio["uncertain_spans"],
        "gaps": perlectio["gaps"],
        "holds": sorted(set(perlectio["holds"]) | set(perlectio["page_holds"])),
    }
    if review is not None:
        row["review"] = {"outcome": review["outcome"], "hold_codes": list(review["hold_codes"])}
    return row


def entry_kind_of(perlectio: Mapping[str, Any]) -> str:
    """An entry's kind: as the reading named it, or its act class for an `acts`-grammar reading."""
    return perlectio.get("entry_kind", perlectio["kind"])
