"""The Recensor's page-path records, read for the two stages after it.

A page-read run's Recensor publishes one `review` per counted unit of
`common.stage.reading_acts` (an act or other reading of a page, or the one row
standing for a page with none) and one `continuation-link` per pair of readings
whose answer flags say an act crosses a page break. The Archetypus and the
Armarium both read them; this module is the one place the shape of those
records is read, so the two stages cannot disagree about it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from common.contracts.errors import FatalAccounting
from common.contracts.stages import RECENSOR
from common.stage import latest_attempt, stage_manifest

REVIEW_KIND: Final = "review"
CONTINUATION_LINK_KIND: Final = "continuation-link"
REVIEW_OPERATION: Final = "recense"
# A page the Exemplar refused: a row of the denominator that is never an act and
# is never reviewed; the page census reports it with the Door's reason.
PAGE_REFUSED_CLASS: Final = "page-refused"


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


def continuation_links(context, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every `continuation-link`, as `{ref, head_act_id, tail_act_id, agreed}`.

    `head` is the reading that continues onto the next page and `tail` the one
    continuing from the previous page; both must be counted rows.
    """
    counted = {row["act_id"] for row in rows}
    links: list[dict[str, Any]] = []
    for entry in stage_manifest(context, RECENSOR)["artifacts"]:
        if entry["kind"] != CONTINUATION_LINK_KIND:
            continue
        record = context.tree.read_artifact(RECENSOR, CONTINUATION_LINK_KIND, entry["artifact_id"])
        payload = _payload(record)
        sides = payload.get("act_ids")
        agreed = payload.get("agreed")
        if (
            not isinstance(sides, list)
            or len(sides) != 2
            or not all(isinstance(side, str) and side in counted for side in sides)
            or sides[0] == sides[1]
            or not isinstance(agreed, bool)
        ):
            raise FatalAccounting(
                f"Recensor continuation-link {entry['artifact_id']!r} does not name two counted "
                "readings and whether their flags agree"
            )
        links.append(
            {
                "ref": {"relative_path": entry["relative_path"], "sha256": entry["sha256"]},
                "head_act_id": sides[0],
                "tail_act_id": sides[1],
                "agreed": agreed,
            }
        )
    return sorted(links, key=lambda link: link["ref"]["relative_path"])
