"""Structural re-shoot deltas derived from sealed cross-capture dissent.

This is an instability instrument, not an accuracy measure.  It deliberately
keeps the Unit 19 caveat beside every derived record: matching observations do
not establish that a reading is right, and different observations do not say
which one is right.

It carries a second caveat of its own, because it creates a temptation Unit 19's
record does not.  Unit 19 holds observations; this record holds *rows that
invite counting*, and the sentence "the re-shoot delta rate was N" is available
to anyone holding it long before ground truth authorizes any such number
(GOVERNANCE 10).  So the shape is built to make the honest sentence the only one
the rows support: every capture pair is a row whether or not a delta could be
computed for it, each pair says in a field of its own whether it was compared at
all, and every locus a compared pair could not compare is named beside that
pair's deltas rather than omitted from them.
"""

from __future__ import annotations

from typing import Any, Final

from common.contracts.canonical import digest_of, self_hash, verify_self_hash
from common.contracts.errors import SchemaRefusal
from common.corpus_register import refuse_preference
from common.cross_capture_dissent import (
    CAVEAT,
    unit20_dissent_input,
    validate_cross_capture_dissent,
)

SCHEMA: Final = "reshoot-delta-record.v1"

# Unit 19's caveat governs what a reading may be said to be; this one governs
# what these rows may be said to measure.  It is sealed and re-derived exactly
# like the inherited one, so neither can be softened on the way to a reader.
DENOMINATOR_CAVEAT: Final = (
    "This record counts nothing and ranks nothing. Every capture pair remains a row, "
    "including the pairs no delta could be computed for, and every locus a compared pair "
    "could not compare is named beside that pair's deltas. A delta count quoted without "
    "its pair denominator, its not-compared pairs, and its named findings is not a "
    "measurement of this act."
)

_FIELDS: Final = frozenset(
    {
        "schema",
        "logical_act_id",
        "perlectio_ref",
        "dissent_digest",
        "pair_records",
        "review_flags",
        "caveat",
        "denominator_caveat",
        "self_hash",
    }
)
_PAIR_FIELDS: Final = frozenset(
    {"pair_id", "view_ids", "comparison_state", "finding_codes", "delta_loci", "uncompared_loci"}
)
_DELTA_FIELDS: Final = frozenset({"locus_id", "established_span_or_gap_ref", "view_ids"})
_UNCOMPARED_FIELDS: Final = frozenset(
    {"locus_id", "established_span_or_gap_ref", "view_ids", "reason_codes"}
)
_FLAG_FIELDS: Final = frozenset({"pair_id", "code", "locus_ids"})

# The five names Unit 19 gives its failed capture conditions.  Held here as
# vocabulary a reader can cite, and reconciled against Unit 19's own map by
# test, NOT enforced as the closed set of codes a pair may carry: Unit 19
# permits a pair to name findings beyond its condition failures, and an
# instrument that refused to carry an upstream finding it did not recognise
# would be deciding what may be reported to it.
CONDITION_CODES: Final = frozenset(
    {
        "capture-occlusion-condition-failed",
        "capture-comparability-condition-failed",
        "same-ink-condition-failed",
        "identical-run-configuration-condition-failed",
        "cross-capture-match-failure",
    }
)
_DELTA_FLAG: Final = "cross-capture-structural-delta"
_UNCOMPARED_FLAG: Final = "cross-capture-locus-not-compared"

COMPARED: Final = "compared"
NOT_COMPARED: Final = "not-compared"

# Why a compared pair could not compare one locus.  Structural statements about
# the evidence, never a judgement about either capture.
_LOCUS_ABSENT: Final = "locus-not-observed-at-every-view-of-the-pair"
_FORM_ABSENT: Final = "observed-form-absent-at-a-view-of-the-pair"
_STATE_UNREADABLE: Final = "locus-recorded-unreadable-across-views"
_STATE_NOT_COMPARABLE: Final = "locus-recorded-not-comparable-across-views"
_RECORDED_STATE_CODES: Final = {
    "unreadable": _STATE_UNREADABLE,
    "not-comparable": _STATE_NOT_COMPARABLE,
}


def _conditions_hold(pair: dict[str, Any]) -> bool:
    condition = pair["capture_condition"]
    return (
        condition["both_unoccluded"]
        and condition["comparably_captured"]
        and pair["same_ink"]
        and pair["identical_run_configuration"]
        and pair["act_match_correct"]
    )


def _locus_rows(dissent: dict[str, Any], view_ids: list[str]) -> tuple[list, list]:
    """Account for every locus at one compared pair: a delta, or a named reason.

    An earlier shape returned only the differing loci and dropped the rest by
    ``continue``.  That made the most informative case in the instrument -- a
    locus read in one capture and unreadable in the other -- indistinguishable
    from a locus both captures read identically: no delta, no finding, no row.
    A record that reads "these captures agreed" where one of them could not be
    read is a claim, and it is the claim GOVERNANCE 10 and GOVERNANCE 2 both
    forbid.  So nothing is filtered out here; loci divide into deltas and named
    non-comparisons, and the two lists together are the locus denominator.
    """
    wanted = set(view_ids)
    deltas: list[dict[str, Any]] = []
    uncompared: list[dict[str, Any]] = []
    for locus in dissent["loci"]:
        established_ref = locus["established_span_or_gap_ref"]
        anchor = {
            "locus_id": locus["locus_id"],
            "established_span_or_gap_ref": (
                dict(established_ref) if established_ref is not None else None
            ),
            "view_ids": list(view_ids),
        }
        observations = {
            item["view_id"]: item["observed_form"]
            for item in locus["observations"]
            if item["view_id"] in wanted
        }
        reasons: set[str] = set()
        if set(observations) != wanted:
            reasons.add(_LOCUS_ABSENT)
        elif None in observations.values():
            reasons.add(_FORM_ABSENT)
        recorded = _RECORDED_STATE_CODES.get(locus["comparison_state"])
        if recorded is not None:
            # The joint reader itself recorded that this locus is unreadable or
            # not comparable across the views.  Comparing its observed forms
            # anyway would be the instrument overruling the only party that saw
            # the ink, so the locus is named rather than compared.
            reasons.add(recorded)
        if reasons:
            uncompared.append({**anchor, "reason_codes": sorted(reasons)})
        elif len(set(observations.values())) > 1:
            deltas.append(anchor)
    return deltas, uncompared


def build_reshoot_delta_record(dissent_record: Any) -> dict[str, Any]:
    """Build Unit 20's non-evaluative structural consumer record.

    The narrow Unit 19 seam is consumed for subject, Perlectio reference,
    complete pair denominator, and caveat.  Access to per-locus observations
    intentionally validates the entire sealed dissent record instead of
    extending that seam.
    """
    dissent = validate_cross_capture_dissent(dissent_record)
    seam = unit20_dissent_input(dissent)
    pair_records: list[dict[str, Any]] = []
    review_flags: list[dict[str, Any]] = []
    for pair in seam["pairs"]:
        codes = sorted(
            {_named_code(code, "an upstream pair finding") for code in pair["finding_codes"]}
        )
        compared = _conditions_hold(pair)
        deltas, uncompared = _locus_rows(dissent, pair["view_ids"]) if compared else ([], [])
        pair_records.append(
            {
                "pair_id": pair["pair_id"],
                "view_ids": list(pair["view_ids"]),
                "comparison_state": COMPARED if compared else NOT_COMPARED,
                "finding_codes": codes,
                "delta_loci": deltas,
                "uncompared_loci": uncompared,
            }
        )
        for code in codes:
            review_flags.append({"pair_id": pair["pair_id"], "code": code, "locus_ids": []})
        for flag_code, rows in ((_DELTA_FLAG, deltas), (_UNCOMPARED_FLAG, uncompared)):
            if rows:
                review_flags.append(
                    {
                        "pair_id": pair["pair_id"],
                        "code": flag_code,
                        "locus_ids": [row["locus_id"] for row in rows],
                    }
                )
    result = {
        "schema": SCHEMA,
        "logical_act_id": seam["logical_act_id"],
        "perlectio_ref": dict(seam["perlectio_ref"]),
        "dissent_digest": digest_of(dissent),
        "pair_records": sorted(pair_records, key=lambda item: item["pair_id"]),
        "review_flags": sorted(
            review_flags, key=lambda item: (item["pair_id"], item["code"], item["locus_ids"])
        ),
        "caveat": seam["caveat"],
        "denominator_caveat": DENOMINATOR_CAVEAT,
    }
    result["self_hash"] = self_hash(result)
    return result


def _named_code(value: Any, what: str) -> str:
    """A finding code is carried, not curated -- but it must be a name."""
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        raise SchemaRefusal(f"reshoot delta record: {what} is not a printable finding name")
    return value


def validate_reshoot_delta_record(record: Any, dissent_record: Any) -> dict[str, Any]:
    """Refuse output not reproduced from the exact sealed Unit 19 evidence."""
    if not isinstance(record, dict) or set(record) != _FIELDS or not verify_self_hash(record):
        raise SchemaRefusal("reshoot delta record: closed shape or self_hash failed")
    refuse_preference(record, what="reshoot delta record")
    if record["schema"] != SCHEMA or record["caveat"] != CAVEAT:
        raise SchemaRefusal("reshoot delta record: schema or dissent caveat is not binding")
    if record["denominator_caveat"] != DENOMINATOR_CAVEAT:
        raise SchemaRefusal(
            "reshoot delta record: the denominator caveat is not the binding wording; a delta "
            "count may not be released from the rows that make it readable"
        )
    if not isinstance(record["dissent_digest"], str) or len(record["dissent_digest"]) != 64:
        raise SchemaRefusal("reshoot delta record: dissent digest is malformed")
    if not isinstance(record["pair_records"], list):
        raise SchemaRefusal(
            "reshoot delta record: pair_records is not a list; the pair denominator cannot "
            "be checked"
        )
    if not isinstance(record["review_flags"], list):
        raise SchemaRefusal(
            "reshoot delta record: review_flags is not a list; named findings cannot be checked"
        )
    pair_ids: set[str] = set()
    for pair in record["pair_records"]:
        if not isinstance(pair, dict) or set(pair) != _PAIR_FIELDS:
            raise SchemaRefusal("reshoot delta record: a pair row is outside its closed shape")
        if not isinstance(pair["pair_id"], str) or pair["pair_id"] in pair_ids:
            raise SchemaRefusal("reshoot delta record: pair rows are malformed or repeated")
        pair_ids.add(pair["pair_id"])
        if not isinstance(pair["comparison_state"], str) or pair["comparison_state"] not in {
            COMPARED,
            NOT_COMPARED,
        }:
            raise SchemaRefusal(
                "reshoot delta record: a pair does not say whether it was compared; an empty "
                "delta list may not stand in for a pair that was never eligible for one"
            )
        if not isinstance(pair["finding_codes"], list):
            raise SchemaRefusal("reshoot delta record: pair findings are not a list")
        for code in pair["finding_codes"]:
            _named_code(code, "a pair finding")
        if not isinstance(pair["delta_loci"], list):
            raise SchemaRefusal("reshoot delta record: delta_loci is not a list")
        for delta in pair["delta_loci"]:
            if not isinstance(delta, dict) or set(delta) != _DELTA_FIELDS:
                raise SchemaRefusal("reshoot delta record: delta is not a structural locus anchor")
        if not isinstance(pair["uncompared_loci"], list):
            raise SchemaRefusal("reshoot delta record: uncompared_loci is not a list")
        for row in pair["uncompared_loci"]:
            if (
                not isinstance(row, dict)
                or set(row) != _UNCOMPARED_FIELDS
                or not isinstance(row["reason_codes"], list)
                or not row["reason_codes"]
            ):
                raise SchemaRefusal(
                    "reshoot delta record: an uncompared locus is not a structural anchor with "
                    "at least one named reason"
                )
            for code in row["reason_codes"]:
                _named_code(code, "an uncompared-locus reason")
    for flag in record["review_flags"]:
        if (
            not isinstance(flag, dict)
            or set(flag) != _FLAG_FIELDS
            or not isinstance(flag["pair_id"], str)
            or flag["pair_id"] not in pair_ids
            or not isinstance(flag["locus_ids"], list)
        ):
            raise SchemaRefusal("reshoot delta record: review flag is malformed")
        _named_code(flag["code"], "a review flag code")
    dissent = validate_cross_capture_dissent(dissent_record)
    if record["dissent_digest"] != digest_of(dissent):
        raise SchemaRefusal("reshoot delta record: dissent digest does not name its sealed input")
    if record != build_reshoot_delta_record(dissent):
        raise SchemaRefusal(
            "reshoot delta record: rows are not reproduced from the sealed dissent evidence"
        )
    return record
