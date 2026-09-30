"""The Recensor's page-path records, read for the two stages after it.

A page-read run's Recensor publishes one `review` per counted unit of
`common.stage.reading_acts` (an act or other reading of a page, or the one row
standing for a page with none) and one `continuation-link` per page break
either side's answer flag names. The Archetypus and the
Armarium both read them; this module is the one place the shape of those
records is read, so the two stages cannot disagree about it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from common.contracts.errors import FatalAccounting
from common.contracts.stages import ATTESTATORES, PERLECTOR, RECENSOR
from common.stage import (
    NO_ACT_ON_PAGE_HOLD,
    PAGE_REFUSED_CLASS,
    latest_attempt,
    latest_per_chair,
    stage_manifest,
)
from common.witness_regime import NAMED, witness_label

REVIEW_KIND: Final = "review"
CONTINUATION_LINK_KIND: Final = "continuation-link"
REVIEW_OPERATION: Final = "recense"
# The row hold a review may release by name over a reading: the Recensor
# confirms that a page read as holding no act holds none, and the page's other
# readings are then established. Every other row hold keeps its reading held.
RELEASABLE_READING_HOLDS: Final = frozenset({NO_ACT_ON_PAGE_HOLD})


def _payload(record: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        raise FatalAccounting(
            f"Recensor {record.get('kind')} {record.get('artifact_id')!r} has no payload"
        )
    return payload


def reviewed_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The rows the Recensor decides about: every row but a refused page's."""
    return [row for row in rows if row["class"] != PAGE_REFUSED_CLASS]


def current_page_reviews(context, rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """The current Recensor review of every counted unit, by act id.

    Every row must have one, and every review must name a row: a unit with no
    review reached no decision, and a review of a unit the denominator does not
    count decided about nothing this run counts.
    """
    by_subject: dict[str, list[dict[str, Any]]] = {}
    for entry in stage_manifest(context, RECENSOR)["artifacts"]:
        if entry["kind"] != REVIEW_KIND:
            continue
        record = context.tree.read_artifact(RECENSOR, REVIEW_KIND, entry["artifact_id"])
        by_subject.setdefault(entry["subject_id"], []).append(record)
    counted = {row["act_id"] for row in rows}
    strays = sorted(set(by_subject) - counted)
    if strays:
        raise FatalAccounting(
            f"the Recensor reviewed {strays}, which this page-read run does not count; a "
            "review of an uncounted unit decided about nothing"
        )
    reviews: dict[str, dict[str, Any]] = {}
    for row in rows:
        act_id = row["act_id"]
        if act_id not in by_subject:
            raise FatalAccounting(
                f"{row['act_key']} ({act_id}) has no Recensor review; a counted unit nobody "
                "decided about has no terminal category"
            )
        review = latest_attempt(
            by_subject[act_id], f"review of {row['act_key']}", operation=REVIEW_OPERATION
        )
        _require_review_of_row(review, row)
        reviews[act_id] = review
    return reviews


def _require_review_of_row(review: Mapping[str, Any], row: Mapping[str, Any]) -> None:
    """The review names the row it decides: its key, kind and reading."""
    payload = _payload(review)
    reading_ref = payload.get("perlectio_ref")
    if (
        payload.get("act_key") != row["act_key"]
        or payload.get("kind") != row["kind"]
        or reading_ref != row["perlectio_ref"]
        or (reading_ref is not None and reading_ref not in review.get("inputs", []))
    ):
        raise FatalAccounting(
            f"the Recensor review of {row['act_key']} does not name that unit's key, kind and "
            "reading as the denominator counts them"
        )


def require_establishable(row: Mapping[str, Any], review: Mapping[str, Any]) -> None:
    """An accepted review stands over a reading the denominator reads, or releases its hold.

    A row with no reading has no text to establish. A held row may be accepted
    only when every hold it carries is one a review may release and the review
    names exactly those codes in its `release`; otherwise a stage after the
    Recensor would be resurrecting a held reading.
    """
    if review.get("outcome") != "accepted":
        raise FatalAccounting(f"the review of {row['act_key']} did not accept it")
    if row["perlectio_ref"] is None:
        raise FatalAccounting(
            f"{row['act_key']} is a {row['class']} row with no reading, yet the Recensor "
            "accepted it; a page with no reading has no text to establish"
        )
    release = _payload(review).get("release")
    if row["disposition"] == "read" and release is None:
        return
    codes = list(row["hold_codes"])
    if (
        not codes
        or not set(codes) <= RELEASABLE_READING_HOLDS
        or not isinstance(release, Mapping)
        or release.get("hold_codes") != sorted(codes)
    ):
        raise FatalAccounting(
            f"{row['act_key']} is held ({', '.join(codes) or 'no code'}), yet the Recensor "
            "accepted it without releasing exactly those holds; a stage may not resurrect a "
            "held reading into an established one"
        )


def shown_page_witnesses(
    context, reading: Mapping[str, Any], testimonia: Sequence[Mapping[str, Any]], what: str
) -> list[dict[str, Any]]:
    """The page witnesses a page reading was shown, each its chair's current page Testimonium.

    `testimonia` are the page Testimonia of the reading's page on disk. The feed
    the reading inputs lists every witness it showed; each row must name one of
    the current ones, under the label this run's regime gives that chair (a
    blinded feed names no chair, so the Testimonium is found by its reference).
    A feed that showed no witness made the reading a Lectio nuda, which is never
    established. The reading's dissent compares against exactly the letters the
    feed showed, once each. Returns `{letter, witness_label, chair, testimonium,
    testimonium_ref}` per shown witness, in letter order.
    """
    payload = reading.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    page_id = payload.get("page_id")
    feed_ref = payload.get("feed_ref")
    if feed_ref not in reading.get("inputs", []):
        raise FatalAccounting(f"{what} does not input the page feed it was read from")
    feed = context.tree.read_artifact_reference(
        feed_ref, stage=PERLECTOR, kind="page-feed", subject_id=page_id
    )["payload"]
    regime = context.witness_context
    if feed.get("witness_regime") != regime:
        raise FatalAccounting(
            f"{what} was read from a feed under witness regime {feed.get('witness_regime')!r}, "
            f"not this run's {regime!r}"
        )
    rows = feed.get("witnesses")
    if not isinstance(rows, list) or not rows:
        raise FatalAccounting(
            f"{what} was shown no page witness: a reading shown no witness is a Lectio nuda, "
            "an instrument record, never an establishing read"
        )
    current = {}
    for record in latest_per_chair(list(testimonia), f"page Testimonium of {page_id}"):
        reference = context.artifact_ref(ATTESTATORES, "page-testimonium", record["artifact_id"])
        current[(reference["relative_path"], reference["sha256"])] = (reference, record)
    shown: list[dict[str, Any]] = []
    for row in rows:
        reference = row.get("testimonium_ref") if isinstance(row, Mapping) else None
        found = (
            current.get((reference.get("relative_path"), reference.get("sha256")))
            if isinstance(reference, Mapping)
            else None
        )
        if found is None or found[0] != reference:
            raise FatalAccounting(
                f"{what} was shown a witness from a Testimonium that is not its chair's current "
                "page Testimonium; nothing is established over superseded testimony"
            )
        record = found[1]
        chair = record["payload"].get("chair")
        if not isinstance(chair, str) or not chair:
            raise FatalAccounting(f"{what} was shown a page Testimonium that names no chair")
        label = witness_label(
            chair, regime=regime, run_id=context.tree.run_id, config_digest=context.config_digest
        )
        if row.get("witness_label") != label or row.get("chair") != (
            chair if regime == NAMED else None
        ):
            raise FatalAccounting(
                f"{what} was shown witness {row.get('witness_label')!r}, which is not the label "
                "this run's regime gives the chair whose Testimonium it names"
            )
        shown.append(
            {
                "letter": row.get("letter"),
                "witness_label": label,
                "chair": chair,
                "testimonium": record,
                "testimonium_ref": reference,
            }
        )
    letters = [witness["letter"] for witness in shown]
    labels = {witness["letter"]: witness["witness_label"] for witness in shown}
    if len(set(letters)) != len(letters) or len({w["chair"] for w in shown}) != len(shown):
        raise FatalAccounting(f"{what} was shown one witness letter or chair twice")
    dissent = payload.get("dissent")
    if (
        not isinstance(dissent, list)
        or not all(isinstance(row, Mapping) for row in dissent)
        or not all(isinstance(row.get("letter"), str) for row in dissent)
        or sorted(row.get("letter") for row in dissent) != sorted(letters)
        or any(labels[row["letter"]] != row.get("witness_label") for row in dissent)
    ):
        raise FatalAccounting(
            f"{what} does not record its dissent against exactly the witnesses its feed showed, "
            "once each"
        )
    return sorted(shown, key=lambda witness: witness["letter"])


def review_reading_ref(review: Mapping[str, Any]) -> dict[str, str] | None:
    """The Perlectio the review decided about; `None` for a page row's review."""
    return _payload(review).get("perlectio_ref")


def review_reason(review: Mapping[str, Any]) -> str:
    """The review's own reason, with its hold codes named when it holds."""
    payload = _payload(review)
    reason = payload.get("reason")
    reason = reason if isinstance(reason, str) else ""
    codes = payload.get("hold_codes")
    if isinstance(codes, list) and codes:
        named = ", ".join(str(code) for code in codes)
        return f"{reason} (holds: {named})" if reason else f"holds: {named}"
    return reason


def review_coverage(review: Mapping[str, Any]) -> dict[str, Any]:
    """The witness-coverage record the Recensor measured for the unit's page."""
    coverage = _payload(review).get("coverage")
    if not isinstance(coverage, dict):
        raise FatalAccounting(
            f"Recensor review {review.get('artifact_id')!r} carries no witness coverage record"
        )
    return coverage


CONTINUATION_LINK_SCHEMA: Final = "recensor-continuation-link.v1"
_CONTINUATION_LINK_FIELDS: Final = frozenset(
    {
        "schema",
        "from_page_ordinal",
        "to_page_ordinal",
        "from_act_id",
        "from_act_key",
        "to_act_id",
        "to_act_key",
        "continues_to_next_page",
        "continues_from_previous_page",
        "agreed",
    }
)


def continuation_links(context, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every `continuation-link`, as `{ref, from_page_ordinal, to_page_ordinal,
    head_act_id, tail_act_id, agreed}`.

    One link per page break an answer flags, subject `page-break:<p>:<p+1>`:
    `head` (`from_act_id`) is the last `act` entry of page p and `tail`
    (`to_act_id`) the first of page p+1, either `None` where that side has no
    act entry. Each named side must be a counted row under its own key; the
    link is `agreed` exactly when both flags say an act crosses the break, and
    is `accepted` exactly when it agrees.
    """
    counted = {row["act_id"]: row for row in rows}
    links: list[dict[str, Any]] = []
    for entry in stage_manifest(context, RECENSOR)["artifacts"]:
        if entry["kind"] != CONTINUATION_LINK_KIND:
            continue
        record = context.tree.read_artifact(RECENSOR, CONTINUATION_LINK_KIND, entry["artifact_id"])
        payload = _payload(record)
        what = f"Recensor continuation-link {entry['artifact_id']!r}"
        if set(payload) != _CONTINUATION_LINK_FIELDS or payload["schema"] != (
            CONTINUATION_LINK_SCHEMA
        ):
            raise FatalAccounting(f"{what} is not a {CONTINUATION_LINK_SCHEMA} record")
        from_page, to_page = payload["from_page_ordinal"], payload["to_page_ordinal"]
        flags = (payload["continues_to_next_page"], payload["continues_from_previous_page"])
        sides = []
        for side, flag in (
            ("from", "continues_to_next_page"),
            ("to", "continues_from_previous_page"),
        ):
            act_id, act_key = payload[f"{side}_act_id"], payload[f"{side}_act_key"]
            if act_id is None and act_key is None:
                sides.append(None)
                continue
            row = counted.get(act_id) if isinstance(act_id, str) else None
            if row is None or row["act_key"] != act_key:
                raise FatalAccounting(f"{what} names a reading this run does not count")
            if row["page_ordinal"] != payload[f"{side}_page_ordinal"]:
                raise FatalAccounting(
                    f"{what} names {act_key}, which is not on its side of the page break"
                )
            if payload[flag] is not (row[flag] is True):
                raise FatalAccounting(f"{what} does not carry {act_key}'s own {flag} flag")
            sides.append(act_id)
        if (
            not all(
                isinstance(ordinal, int) and not isinstance(ordinal, bool)
                for ordinal in (from_page, to_page)
            )
            or to_page != from_page + 1
            or record.get("subject_id") != f"page-break:{from_page}:{to_page}"
            or not all(isinstance(flag, bool) for flag in flags)
            or not any(flags)
            or (flags[0] and sides[0] is None)
            or (flags[1] and sides[1] is None)
            or payload["agreed"] is not (flags[0] and flags[1])
            or record.get("outcome") != ("accepted" if payload["agreed"] else "held-for-review")
        ):
            raise FatalAccounting(
                f"{what} does not name one flagged page break, its flags, and whether they agree"
            )
        links.append(
            {
                "ref": {"relative_path": entry["relative_path"], "sha256": entry["sha256"]},
                "from_page_ordinal": from_page,
                "to_page_ordinal": to_page,
                "head_act_id": sides[0],
                "tail_act_id": sides[1],
                "continues_to_next_page": flags[0],
                "continues_from_previous_page": flags[1],
                "agreed": payload["agreed"],
            }
        )
    return sorted(links, key=lambda link: (link["from_page_ordinal"], link["to_page_ordinal"]))
