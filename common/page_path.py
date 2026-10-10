"""The Perlector's page path: what its records derive, for the stage that writes them and every reader.

Stage 4 (`pipeline/4_perlector/page_run.py`) reads each sealed page whole and
publishes, per page, a `page-feed`, a `page-reading` and a `page-accounting`
and, for each entry of a read answer, one `act-region` and one `perlectio.v3`.
Everything those records hold that is derived rather than given is derived
here, once, in this order:

- record kinds, schemas and attempt ids;
- why a page is not asked, and the fixture's declared answers;
- an operator re-read's record and its checks;
- the request: its text, images, digest and capacity, first reading and re-ask;
- the reply: `read_reply` and `answer_problems`;
- `entry_plans`: each entry's identity, region, text, doubt marks, truncation
  and holds, for a first reading, a re-ask (`common/page_reask.py`) and an
  operator re-read;
- the feed and its witness roster (`page_feed_of`);
- the Perlectio: page dissent and `expected_perlectio`;
- `accounting_inputs`: every input the page accounting
  (`common/page_accounting.py`) measures a reading against -- the feed, every
  sealed witness of the page shown or hidden, the Designator's Surya and
  detector records, the Ink Map's runs and the entries' truncations -- and the
  records it read them from.

Stage 4 publishes from these, and the page-read denominator
(`common.stage.reading_denominator`) calls the same functions over the sealed
records to recompute what stage 4 published, so the writer and the counter
cannot read one page two ways.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final

from common import (
    dissent,
    page_accounting,
    page_answer,
    page_edges,
    page_render,
    page_types,
    truncation,
)
from common import reading_annotations as annotations
from common.alignment import bracket_marker_view
from common.background import (
    validate_ink_not_measurable_payload,
    validate_measured_ink_map_payload,
)
from common.chairs.models import ChairIdentity
from common.contracts.canonical import digest_bytes, digest_of, is_plain_int
from common.contracts.envelope import read_verified
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal
from common.contracts.identities import act_id as derive_act_id
from common.contracts.identities import attempt_id, perlector_attempt_id
from common.contracts.outcomes import WITNESS_READING_OUTCOMES
from common.contracts.serving import (
    CHAIR_STREAM_CALL_RECORD_SCHEMA,
    CHAIR_STREAM_FIELDS,
    CHAIR_STREAM_SCHEMA,
    READER_STOP_REPETITION_LOOP,
    reading_stop_reason,
)
from common.contracts.stages import ATTESTATORES, DESIGNATOR, INK_MAP
from common.decoding import chair_decoding, engine_effective_sampling, recorded_wire_decimals
from common.page_witness_units import DAI, READ_OUTCOME, WITNESS_LETTERS, witness_reading
from common.repetition_loop import first_repetition_loop, validate_guard
from common.request_capacity import page_request_capacity
from common.residual_ink import (
    INK_NOT_MEASURABLE,
    MINIMUM_CONTRAST_BELOW_BACKGROUND,
    load_coverage_audit_config,
    reconcile_edge_finding_with_runs,
    resolve_coverage_audit_policy,
)

if TYPE_CHECKING:
    from common.stage import ServingReader

# The record kinds and schemas of the page path.
PAGE_FEED_KIND: Final = "page-feed"
PAGE_READING_KIND: Final = "page-reading"
PAGE_ACCOUNTING_KIND: Final = "page-accounting"
ACT_REGION_KIND: Final = "act-region"
PERLECTIO_KIND: Final = "perlectio"
PAGE_READING_SCHEMA: Final = "perlector-page-reading.v2"
ACT_REGION_SCHEMA: Final = "perlector-act-region.v2"
PERLECTIO_SCHEMA: Final = "perlectio.v3"
# Every field a sealed Perlectio holds: what `expected_perlectio` names, and the
# entry's `dissent` stage 4 publishes beside it.
PERLECTIO_FIELDS: Final = frozenset(
    {
        "schema",
        "page_id",
        "page_ordinal",
        "act_region_ref",
        "page_reading_ref",
        "page_accounting_ref",
        "feed_ref",
        "n",
        "kind",
        "entry_kind",
        "label",
        "text",
        "uncertain_spans",
        "gaps",
        "uncertainty_assessment",
        "truncation",
        "autopsia",
        "continues_from_previous_page",
        "continues_to_next_page",
        "holds",
        "page_holds",
        "engine_call",
        "provenance",
        "dissent",
    }
)
# Every kind the page reading publishes beside its Perlectios.
PAGE_PATH_KINDS: Final = frozenset(
    {PAGE_FEED_KIND, PAGE_READING_KIND, PAGE_ACCOUNTING_KIND, ACT_REGION_KIND}
)

PAGE_READ_OPERATION: Final = "page-read"
# The machine's own readings of a page: the first and its one re-ask. An operator
# re-read is numbered after both (`is_operator_reread`, "an operator re-read" below).
READING_ORDINALS: Final = (page_edges.FIRST_READING, page_edges.REASK_READING)
# The `page-reading` field only an operator re-read carries (`operator_reread_record`).
OPERATOR_REREAD_FIELD: Final = "operator_reread"
# The `page-reading` field only a reading whose reply was repaired before parsing
# carries (`page_answer.parse_page_answer_repaired`).
ANSWER_REPAIRS_FIELD: Final = "answer_repairs"
ACT_REGION_OPERATION: Final = "reading-region"
PERLECTIO_OPERATION: Final = "perlegere"

# What became of a page's call, on its `page-reading`.
PARSED: Final = page_answer.PARSED
MALFORMED: Final = page_answer.MALFORMED
CUT_OFF: Final = "cut-off"
# A streamed reply the client abandoned on a repetition loop (`common/repetition_loop.py`):
# held whole like a cut-off, never parsed.
REPETITION_LOOP: Final = READER_STOP_REPETITION_LOOP
REFUSED_CAPACITY: Final = "refused-capacity"
CALL_FAILED: Final = "call-failed"
NOT_RUN: Final = "not-run"
READ: Final = "read"
HELD: Final = "held"
# A page whose call failed is held for review like any unread page, and its
# record's outcome is the Perlector's `failed`, so the run-level hard-failure
# cap (`config/hard_failure.toml`, `(perlector, failed)`) counts it. A failed
# re-ask call is `failed` too and counted the same, but its page stands on its
# first reading, held under `reask-unread` rather than as `page-unread`.
FAILED: Final = "failed"


def reading_outcome(parse_state: str, disposition: str) -> str:
    """The `page-reading` record's outcome: `failed` for a failed call, else its disposition."""
    return FAILED if parse_state == CALL_FAILED else disposition


# Why a page is not asked (`not-run`), and why a parsed answer is held whole.
PAGE_NOT_SEALED: Final = "page-not-sealed"
CHAIR_ABSENT: Final = "chair-absent"
NO_WITNESS_TESTIMONY: Final = "no-witness-testimony"
NOTHING_TO_SHOW: Final = "nothing-to-show"
NO_STOP_REASON: Final = "no-stop-reason"

# Why one entry of a parsed, valid answer is held.
UNPLACED: Final = "reading-unplaced"
DUPLICATE_REGION: Final = page_accounting.DUPLICATE_REGION
READING_INCOMPLETE: Final = "reading-incomplete"
DOUBT_MARKS_MALFORMED: Final = "doubt-marks-malformed"
ENTRY_NO_READABLE_TEXT: Final = "entry-no-readable-text"
NO_AUTOPSIA: Final = "no-autopsia"
# Held on every entry of an operator re-read that does not read each act of the
# reading it replaces as one act of its own (`superseded_acts_kept`).
SUPERSEDED_ACT_NOT_READ: Final = "superseded-act-not-read"
# An entry, or every entry of a page reading, with more of its text doubtful or
# unread than the sealed page-accounting policy's `[doubt]` limits allow, so a
# reading cannot pass by marking everything doubtful.
DOUBT_SHARE_HIGH: Final = "doubt-share-high"
PAGE_DOUBT_SHARE_HIGH: Final = "page-doubt-share-high"

# The act classes an entry mints: placed on the page, or citing no placing id
# (`page_accounting.placement_boxes`).
READING_CLASS: Final = "reading"
UNPLACED_CLASS: Final = "reading-unplaced"

# The Designator's stage-2 Surya records (`surya-page` census per page,
# `surya-line` and `surya-block` per detection).
SURYA_PAGE_KIND: Final = "surya-page"
SURYA_LINE_KIND: Final = "surya-line"
SURYA_BLOCK_KIND: Final = "surya-block"
# How Surya ordered a page's blocks (a census's `reading_order`, the feed's
# `block_sequence`): its reading-order head, or a raster sort (top to bottom,
# then left to right) with the reason Surya fell back to it. Restated from
# `operations/serving/surya/contract.py`, which Surya's own environment loads
# standalone without this package; `common/test_surya_reading_orders.py` holds
# the two equal.
SURYA_ORDER_HEAD: Final = "surya-order-head"
SURYA_RASTER_FALLBACK: Final = "raster-fallback"
SURYA_READING_ORDERS: Final = (SURYA_ORDER_HEAD, SURYA_RASTER_FALLBACK)
# The Attestatores' page witness record a feed is built from.
PAGE_TESTIMONIUM_KIND: Final = "page-testimonium"


def is_operator_reread(ordinal: Any) -> bool:
    """Whether a page reading's ordinal is an operator re-read's (3 or more)."""
    return is_plain_int(ordinal) and ordinal >= page_edges.OPERATOR_REREAD_FIRST


def is_whole_page_reading(ordinal: Any) -> bool:
    """Whether a reading read the whole page: the first reading or an operator re-read.

    A re-ask (2) was asked about named ids alone, so its entries never set a
    page's edges.
    """
    return ordinal == page_edges.FIRST_READING or is_operator_reread(ordinal)


def page_reading_attempt(page_id: str, ordinal: int) -> str:
    """The attempt of a page's first reading (1), its re-ask (2) or an operator re-read (3+)."""
    if ordinal not in READING_ORDINALS and not is_operator_reread(ordinal):
        raise ContractError(
            "a page reading is attempt 1, its re-ask 2, or an operator re-read from "
            f"{page_edges.OPERATOR_REREAD_FIRST} on, never {ordinal!r}"
        )
    return attempt_id(page_id, PAGE_READ_OPERATION, ordinal)


def region_attempt(act_id: str) -> str:
    return attempt_id(act_id, ACT_REGION_OPERATION, 1)


def perlectio_attempt(act_id: str) -> str:
    return perlector_attempt_id(act_id, PERLECTIO_OPERATION, 1)


def distinct_refs(references: list[dict[str, str] | None]) -> list[dict[str, str]]:
    """The references once each, in first-seen order, skipping `None`.

    One content-addressed blob can honestly be reached twice (a re-proof answering the
    same bytes; a page partition and its native capture). Two digests under one path
    means a blob was rewritten, and refuses.
    """
    seen: dict[str, dict[str, str]] = {}
    for reference in references:
        if reference is None:
            continue
        known = seen.setdefault(reference["relative_path"], dict(reference))
        if known != reference:
            raise SchemaRefusal(
                f"two different digests are claimed for input {reference['relative_path']!r}: "
                f"{known!r} and {reference!r}"
            )
    return list(seen.values())


def refs_by_path(references: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(references, key=lambda reference: reference["relative_path"])


# --- pages not asked, and the fixture's declared answers --------------------------


def not_run_problems(
    feed: Mapping[str, Any], *, chair_present: bool, no_testimony: bool
) -> list[dict[str, str]]:
    """Every reason a sealed page with a feed is not asked, in the order they are recorded."""
    # The serving package reads `common.stage`, which reads this module.
    from common import page_feed

    problems = []
    if not chair_present:
        problems.append(
            {
                "code": CHAIR_ABSENT,
                "detail": "the Perlector chair is absent from this run's roster; nothing read "
                "the page",
            }
        )
    if no_testimony:
        problems.append(
            {
                "code": NO_WITNESS_TESTIMONY,
                "detail": "no witness testified to this page: the sealed roster seats no page "
                "witness, or the Attestatores recorded no page Testimonium for it; the page is "
                "held for a human, not read without its witnesses. Seat a page witness or "
                "complete the Attestatores pass, then read the page again",
            }
        )
    if page_feed.shows_nothing(feed):
        problems.append(
            {
                "code": NOTHING_TO_SHOW,
                "detail": "the sealed feed shows no page image, and this page has no witness "
                "text and no detection to show; a reading would have nothing to be made from",
            }
        )
    return problems


def _fixture_rows(context, table: str, ordinal: int) -> list[dict[str, Any]]:
    """The synthetic fixture's `[[table]]` rows for page `ordinal` under this scenario."""
    return [
        row
        for row in context.fixture.get(table, [])
        if row.get("scenario") == context.scenario and row.get("page_ordinal") == ordinal
    ]


def _one_fixture_answer(rows: list[dict[str, Any]], what: str, context, ordinal: int) -> dict:
    """The one fixture answer row `rows` must be: an answer string, stopped or cut."""
    if len(rows) != 1:
        raise ContractError(
            f"the fixture declares {len(rows)} {what}s for scenario {context.scenario!r}, "
            f"page {ordinal}; a page read offline needs exactly one"
        )
    row = rows[0]
    if not isinstance(row.get("answer"), str) or row.get("stop_reason", "stop") not in (
        "stop",
        "length",
    ):
        raise ContractError(
            f"the fixture's {what} for {context.scenario!r}, page {ordinal} is not an "
            "answer string with a stop reason of stop or length"
        )
    return row


def fixture_page_answer(context, ordinal: int) -> dict[str, Any]:
    """The synthetic fixture's one declared answer to page `ordinal` under this scenario."""
    return _one_fixture_answer(
        _fixture_rows(context, "page_answer", ordinal), "page answer", context, ordinal
    )


def fixture_reask_answer(context, ordinal: int, *, planned: bool) -> dict[str, Any] | None:
    """The synthetic fixture's one declared re-ask answer to page `ordinal`, when it is planned.

    A page the plan re-asks needs exactly one `[[page_reask_answer]]` row
    under the scenario; a row for a page the plan does not re-ask is refused
    by name, since its answer would never be asked for. Read only with the
    re-ask on: with `page_level_reread = 0` no page is planned and the rows
    are not read, so one fixture serves both budgets.
    """
    rows = _fixture_rows(context, "page_reask_answer", ordinal)
    if not planned:
        if rows:
            raise ContractError(
                f"the fixture declares a page re-ask answer for scenario {context.scenario!r}, "
                f"page {ordinal}, but the page's first reading plans no re-ask; the answer "
                "would never be asked for"
            )
        return None
    return _one_fixture_answer(rows, "page re-ask answer", context, ordinal)


# --- an operator re-read ----------------------------------------------------------
#
# A person's page `re-ask` decision, current when the Perlector runs, makes the
# page's next operator re-read: a whole-page reading like the first, asked the
# same request (a synthetic fixture answers it with the page's declared
# `[[page_answer]]`, as a fixed reader asked the same request would), that
# becomes the page's current reading. It names the decisions
# it answers and every earlier reading of the page, which it supersedes; those
# stay in the run tree as read. It is outside the sealed `page_level_reread`
# budget, which bounds the machine's own re-ask, and it is never re-asked by
# the machine: a person asks again with another decision.


def operator_reread_record(
    decisions: list[tuple[dict[str, str], Mapping[str, Any]]],
    supersedes: list[dict[str, str]],
) -> dict[str, Any]:
    """The `operator_reread` an operator re-read records: its decisions and what it supersedes.

    `decisions` is each answered decision's stored approval reference and
    record; `supersedes` every earlier reading of the page, in attempt order.
    """
    return {
        "decisions": sorted(
            (
                {"decision_hash": record["self_hash"], "approval_ref": dict(reference)}
                for reference, record in decisions
            ),
            key=lambda decision: decision["decision_hash"],
        ),
        "supersedes": [dict(reference) for reference in supersedes],
    }


def require_operator_reread(
    block: Any,
    *,
    run_id: str,
    page_id: str,
    stored: Mapping[str, tuple[Any, Mapping[str, Any]]],
    supersedes: list[dict[str, str]],
    what: str,
) -> list[str]:
    """Refuse an `operator_reread` that is not a stored page re-ask's of this page; its hashes.

    `stored` is the run's stored decisions by digest
    (`RunTree.review_decision_records`). Each decision must be stored under the
    reference it names and be a page `re-ask` of this page in this run, and
    `supersedes` must be exactly the page's earlier readings.
    """
    from common.contracts.approval import PAGE_SCOPE, REVIEW_ACTION

    if (
        not isinstance(block, Mapping)
        or set(block) != {"decisions", "supersedes"}
        or not isinstance(block["decisions"], list)
        or not block["decisions"]
    ):
        raise FatalAccounting(f"{what} names no operator decision it answers")
    hashes = []
    for decision in block["decisions"]:
        reference = decision.get("approval_ref") if isinstance(decision, Mapping) else None
        found = stored.get(reference.get("sha256")) if isinstance(reference, Mapping) else None
        record = found[1] if found is not None else None
        review = record.get("review") if isinstance(record, Mapping) else None
        if (
            found is None
            or set(decision) != {"decision_hash", "approval_ref"}
            or found[0].to_record() != reference
            or record.get("action") != REVIEW_ACTION
            or record.get("self_hash") != decision["decision_hash"]
            or not isinstance(review, Mapping)
            or (review.get("run_id"), review.get("scope"), review.get("decision"))
            != (run_id, PAGE_SCOPE, "re-ask")
            or record.get("subject_ids") != [page_id]
        ):
            raise FatalAccounting(
                f"{what} names a decision that is not a stored page re-ask of this page"
            )
        hashes.append(decision["decision_hash"])
    if hashes != sorted(set(hashes)):
        raise FatalAccounting(f"{what} names its decisions out of order or twice")
    if block["supersedes"] != supersedes:
        raise FatalAccounting(
            f"{what} does not supersede exactly the page's earlier readings, in attempt order"
        )
    return hashes


# --- the request -----------------------------------------------------------------
#
# What stage 4 sends for a page, built from its feed, and what the page-read
# denominator builds again to bind a sealed reading to its page's request.


def request_text(serving_recipe: Any, feed: Mapping[str, Any]) -> str:
    """The page request's text: `page_prompt.build_page_prompt` over the feed."""
    # The serving package reads `common.stage`, which reads this module.
    from common import page_prompt

    return page_prompt.build_page_prompt(serving_recipe, feed)


def request_image_sha256s(feed: Mapping[str, Any]) -> list[str]:
    """The digest of each image the page request sends: the render, then the overlay."""
    digests = [feed["page_render"]["image_sha256"]] if feed["page_render"] else []
    if feed["overlay"] is not None:
        digests.append(feed["overlay"]["image_sha256"])
    return digests


def request_images(feed: Mapping[str, Any], read_bytes) -> list[bytes]:
    """The images the page request sends, in order, read digest-checked."""
    from common import page_overlay

    images = []
    if feed["page_render"] is not None:
        render = feed["page_render"]
        images.append(
            read_verified(
                read_bytes,
                {"relative_path": render["image_path"], "sha256": render["image_sha256"]},
                "the page render",
            )
        )
    if feed["overlay"] is not None:
        images.append(page_overlay.overlay_image(feed, read_bytes))
    return images


def request_digest(text: str, image_sha256s: list[str]) -> str:
    """The digest a page reading records of the request its page was asked with."""
    return digest_of({"image_sha256s": image_sha256s, "text_sha256": digest_bytes(text.encode())})


def request_capacity(
    row: Any, serving_recipe: Any, feed: Mapping[str, Any], text: str, generation: Mapping[str, int]
) -> dict[str, Any]:
    """The page request admitted against its sealed serving row, or `RequestCapacityRefusal`."""
    from common import page_feed, page_prompt

    return page_request_capacity(
        row,
        image_sizes=page_feed.request_image_sizes(feed),
        prompt_text=text,
        prompt_parts=page_prompt.prompt_parts(serving_recipe, feed),
        template_digest=page_prompt.BUILDER_SHA256,
        answer_measure=feed["answer_measure"],
        generation=generation,
    )


def reask_request_text(
    serving_recipe: Any, feed: Mapping[str, Any], reask: Mapping[str, Any]
) -> str:
    """The re-ask's text: `page_prompt.page_reask_prompt` over the feed and `render_reask`'s data."""
    from common import page_prompt

    return page_prompt.page_reask_prompt(serving_recipe, feed, reask)


def reask_record(
    *,
    reading_ref: dict[str, str],
    accounting_ref: dict[str, str],
    shown: Mapping[str, Any],
    budget: int,
    serving_recipe: Any,
    feed: Mapping[str, Any],
) -> dict[str, Any]:
    """The `reask` a page's re-ask reading records: why it was asked, what it showed, how.

    `reading_ref` and `accounting_ref` are the first reading and its
    accounting the plan was made from; `shown` is `page_reask.render_reask`'s
    (`prior_entries` and `named`); `budget` the sealed page_level_reread; and
    `prompt` the re-ask's prompt evidence (`page_prompt.reask_prompt_evidence`).
    Stage 4 adopts a sealed re-ask only when it records exactly this.
    """
    from common import page_prompt

    return {
        "trigger_reading_ref": dict(reading_ref),
        "trigger_accounting_ref": dict(accounting_ref),
        "named": [dict(item) for item in shown["named"]],
        "prior_entries": [dict(item) for item in shown["prior_entries"]],
        "budget": budget,
        "prompt": page_prompt.reask_prompt_evidence(serving_recipe, feed, shown),
    }


def reask_request_capacity(
    row: Any,
    serving_recipe: Any,
    feed: Mapping[str, Any],
    reask: Mapping[str, Any],
    text: str,
    generation: Mapping[str, int],
) -> dict[str, Any]:
    """The re-ask admitted against the page's sealed row, or `RequestCapacityRefusal`.

    The same images as the first request; the answer reserved on the named
    witness units' text (`request_capacity.reask_answer_measure`).
    """
    from common import page_feed, page_prompt
    from common.request_capacity import reask_answer_measure

    named = {item["id"] for item in reask["named"]}
    units = [
        (witness["letter"], unit["text"])
        for witness in feed["witnesses"]
        for unit in witness["units"]
        if unit["id"] in named
    ]
    surya = feed["surya"]
    lines = set() if surya is None else {line["id"] for line in surya["lines"]}
    return page_request_capacity(
        row,
        image_sizes=page_feed.request_image_sizes(feed),
        prompt_text=text,
        prompt_parts=page_prompt.reask_prompt_parts(serving_recipe, feed, reask),
        template_digest=page_prompt.BUILDER_SHA256,
        answer_measure=reask_answer_measure(units, len(named), named_lines=len(named & lines)),
        generation=generation,
    )


def page_sampling(decoding_policy: Mapping[str, Any], role: str) -> dict[str, Any]:
    """The sealed Perlector row a live page call sends, and what the engine samples under.

    `ChairClient` puts exactly this row on the wire, with the serving receipt's
    seed. A page reading is sampled, so a second call would be a second draw:
    a resumed pass adopts the sealed reading and never asks again.
    """
    values = chair_decoding(decoding_policy, role)
    return {
        "chair": role,
        "sent": recorded_wire_decimals(values),
        "effective": recorded_wire_decimals(engine_effective_sampling(values)),
    }


def retained_reply(
    read_bytes, engine_call: Mapping[str, Any], reader: ServingReader
) -> dict[str, Any]:
    """What the engine answered a live page call, read again from its retained bytes.

    `engine_call` is the `page-reading`'s: the raw response and the call record
    are read digest-checked, and `reader` (the stage's `common.stage.ServingReader`)
    parses the response as the serving client parsed it. Returns `{content,
    finish_reason, stop_reason}`.

    A streamed reply (`chair-stream-call-record`) is scanned again under the guard
    its call record names, and the loop found must be exactly the one the record
    says stopped it, or none; its `stop_reason` is then `repetition-loop`. Whether
    a reply looped is measured from its bytes, never taken from the record.
    """
    if not isinstance(engine_call, Mapping):
        raise ContractError("a live page reading's engine_call is not an object")
    body = read_verified(read_bytes, engine_call["raw_response_ref"], "a page reading's response")
    call = json.loads(
        read_verified(read_bytes, engine_call["call_record_ref"], "a page reading's call record")
    )
    if not isinstance(call, dict):
        raise ContractError("a page reading's call record is not a JSON object")
    stream = _call_stream(call)
    try:
        content, finish_reason = reader.reading_reply(
            status=call.get("response_status"),
            body=body,
            kind=call.get("kind"),
            model_id=engine_call["served_model_id"],
            **({} if stream is None else {"stopped": stream["stopped"] is not None}),
        )
        stop_reason = reading_stop_reason(finish_reason)
    except (ContractError, ValueError) as error:
        raise ContractError(
            f"a page reading's retained response is not a reading: {error}"
        ) from error
    if stream is not None:
        loop = first_repetition_loop(content, stream["loop_guard"])
        if loop != stream["stopped"]:
            raise ContractError(
                f"a page reading's streamed reply shows the repetition loop {loop!r}, but its "
                f"call record says it was stopped on {stream['stopped']!r}"
            )
        if loop is not None:
            stop_reason = REPETITION_LOOP
    return {"content": content, "finish_reason": finish_reason, "stop_reason": stop_reason}


def _call_stream(call: Mapping[str, Any]) -> dict[str, Any] | None:
    """A streamed call record's `stream`, its guard checked, or `None` for a whole reply."""
    if call.get("schema") != CHAIR_STREAM_CALL_RECORD_SCHEMA:
        return None
    stream = call.get("stream")
    if (
        not isinstance(stream, dict)
        or set(stream) != CHAIR_STREAM_FIELDS
        or stream["schema"] != CHAIR_STREAM_SCHEMA
        or not (stream["stopped"] is None or isinstance(stream["stopped"], dict))
    ):
        raise ContractError("a page reading's streamed call record has no valid stream record")
    validate_guard(stream["loop_guard"])
    return stream


def read_reply(
    content: str,
    stop_reason: str | None,
    feed: Mapping[str, Any],
    accounting_policy: page_accounting.PageAccountingPolicy,
    named: list[str] | None = None,
) -> tuple[str, Any, list[dict[str, Any]], list[dict[str, Any]]]:
    """`(parse_state, answer, problems, repairs)` for a reply the engine finished or was cut
    on, or the client stopped on a repetition loop.

    `named` is `None` for a first reading, and a re-ask's named ids for its reply.
    `repairs` is the page answer's one repair when it was applied
    (`page_answer.parse_page_answer_repaired`), else empty; a cut-off or looping
    reply is never parsed, so never repaired.
    """
    if stop_reason == REPETITION_LOOP:
        return (
            REPETITION_LOOP,
            None,
            [
                {
                    "code": REPETITION_LOOP,
                    "detail": "the reply repeated the same line or block of lines over and "
                    "over, and was stopped; the answer is held whole",
                }
            ],
            [],
        )
    if stop_reason == "length":
        return (
            CUT_OFF,
            None,
            [
                {
                    "code": CUT_OFF,
                    "detail": "the engine stopped at the output cap; the answer is held whole",
                }
            ],
            [],
        )
    state, answer, problems, repairs = page_answer.parse_page_answer_repaired(content)
    if state == PARSED:
        problems = answer_problems(answer, feed, stop_reason, accounting_policy, named)
    return state, answer, problems, repairs


# --- the answer -----------------------------------------------------------------
#
# The answer is read by `common/page_accounting.py`'s `validate_answer` against
# `feed_candidates`, the placement map the accounting measures against too, under
# the sealed page-accounting policy.


def _validated(
    answer: Any,
    feed: Mapping[str, Any],
    accounting_policy: page_accounting.PageAccountingPolicy,
    named: list[str] | None,
) -> dict[str, Any]:
    """The answer read against every feed id, or a re-ask's against the ids it names."""
    candidates = page_accounting.feed_candidates(feed, accounting_policy)
    if named is None:
        return page_accounting.validate_answer(answer, candidates, policy=accounting_policy)
    return page_accounting.validate_reask_answer(answer, candidates, named)


def answer_problems(
    answer: Any,
    feed: Mapping[str, Any],
    stop_reason: str | None,
    accounting_policy: page_accounting.PageAccountingPolicy,
    named: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Everything that holds a parsed answer whole, from the answer, its feed and the finish.

    The answer's own id problems against the feed -- for a re-ask, against
    the ids it names, with any continuation flag set a problem too -- and
    `no-stop-reason` when the engine gave no finish reason. A reply cut at
    the output cap is not a parsed answer (`cut-off`) and is never given here.
    """
    problems = list(_validated(answer, feed, accounting_policy, named)["problems"])
    if stop_reason is None:
        problems.append(
            {
                "code": NO_STOP_REASON,
                "detail": "the engine gave no finish reason, so whether the answer ran to "
                "its own end is unknown; the answer is kept and held whole",
            }
        )
    return problems


def answer_entries(
    answer: dict[str, Any],
    feed: Mapping[str, Any],
    accounting_policy: page_accounting.PageAccountingPolicy,
    named: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Each entry of a valid answer: the entry, its expanded ids, region and region holds.

    The entry's `kind` is its act class; `entry_kind` is the kind the answer named.
    """
    validated = _validated(answer, feed, accounting_policy, named)
    shared = {
        n
        for finding in page_accounting.duplicate_regions(validated["entries"], accounting_policy)
        for n in finding["ns"]
    }
    entries = []
    for raw, entry in zip(answer["entries"], validated["entries"], strict=True):
        union = entry["union_box_px"]
        holds = [] if union is not None else [UNPLACED]
        if entry["n"] in shared:
            holds.append(DUPLICATE_REGION)
        entries.append(
            {
                # The entry as given, its kind its act class (`common.page_types`).
                "act": {**raw, "kind": entry["kind"]},
                "entry_kind": entry["entry_kind"],
                "cited_ids": entry["cited_ids"],
                "region_boxes_px": entry["region_boxes_px"],
                "union_box_px": union,
                "holds": holds,
            }
        )
    return entries


def entry_plans(
    answer: dict[str, Any],
    feed: Mapping[str, Any],
    *,
    page_id: str,
    stop_reason: str | None,
    truncation_policy: Mapping[str, Any],
    accounting_policy: page_accounting.PageAccountingPolicy,
    attempt: int = page_edges.FIRST_READING,
    named: list[str] | None = None,
    first_count: int = 0,
    superseded: list[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Each entry of a read answer as it is published, from the answer, feed and sealed policies.

    `attempt` is the reading's ordinal: a re-ask's entries (`attempt = 2`,
    `named` its named ids) are read against the ids it names and bound to
    its own attempt, so they never take a first-reading entry's identity; an
    operator re-read's (3 or more) are a whole page's, like a first
    reading's, bound to their own attempt.
    Each plan's `n` is the number the page accounting names the entry by and
    `reading_n` its number in its own answer: equal on a first reading, and
    for a re-ask `n = first_count + reading_n`, `first_count` being the first
    reading's entries (`page_accounting._combined`).

    Per entry: its act id and class, its region's boxes and their union box,
    its text and doubt layers (`reading_annotations.read_doubt_marks`), its
    truncation classification over its region's area (the union of its boxes,
    not the rectangle around them), and its own holds: the region's (`reading-unplaced`,
    `duplicate-region`, and `no-autopsia` when no page image was shown) and the
    reading's (`doubt-marks-malformed`, `reading-incomplete`,
    `entry-no-readable-text`, and `doubt-share-high` over the sealed `[doubt]`
    limit). The page limit is decided over every entry the page publishes
    (`hold_doubtful_page`). The page accounting reads the truncations from here,
    before any act record exists.

    An operator re-read is given `superseded`, the entry plans the page counted
    before it. When it does not read each of their acts as one act of its own
    (`superseded_acts_kept`), every one of its entries holds
    `superseded-act-not-read`: an act may not leave the count inside a re-read by
    being dropped, merged into another or read as something else.
    """
    if (superseded is not None) != is_operator_reread(attempt):
        raise ContractError("only an operator re-read is planned against the reading it replaces")
    page_pixels = feed["page_size"]["w"] * feed["page_size"]["h"]
    if (attempt == page_edges.REASK_READING) != (named is not None):
        raise ContractError("a re-ask's entries are planned with its named ids, and only its")
    if first_count and attempt != page_edges.REASK_READING:
        raise ContractError("only a re-ask's entries are numbered on after a first reading's")
    reading_attempt = page_reading_attempt(page_id, attempt)
    autopsia = feed["page_render"] is not None
    plans = []
    for entry in answer_entries(answer, feed, accounting_policy, named):
        act, union = entry["act"], entry["union_box_px"]
        act_class = READING_CLASS if union is not None else UNPLACED_CLASS
        region_holds = list(entry["holds"])
        if not autopsia:
            region_holds.append(NO_AUTOPSIA)
        text, assessment = annotations.read_doubt_marks(act["text"])
        reading_holds = list(region_holds)
        if assessment["state"] == annotations.ASSESSMENT_MALFORMED:
            reading_holds.append(DOUBT_MARKS_MALFORMED)
        entry_kind = entry["entry_kind"]
        record = (
            truncation.classify(
                text,
                region_pixels=page_accounting.region_area(entry["region_boxes_px"]),
                page_pixels=page_pixels,
                truncation_policy=truncation_policy,
                stop_reason=stop_reason,
                length_exempt_kind=None
                if page_types.length_signal_applies(entry_kind)
                else entry_kind,
            )
            if union is not None
            else None
        )
        if record is not None and truncation.holds_as_failure(record["classification"]):
            reading_holds.append(READING_INCOMPLETE)
        if not text.strip():
            reading_holds.append(ENTRY_NO_READABLE_TEXT)
        # An entry with no readable text, or marks that do not parse, is held by
        # its own rule above.
        if (
            text.strip()
            and assessment["state"] == annotations.ASSESSMENT_ASSESSED
            and annotations.doubt_exceeds(
                annotations.doubt_count(text, assessment),
                accounting_policy.max_act_doubt_share_bp,
            )
        ):
            reading_holds.append(DOUBT_SHARE_HIGH)
        plans.append(
            {
                "act": act,
                "act_id": derive_act_id(
                    page_id,
                    act_class,
                    {"page_reading": reading_attempt, "n": act["n"], "union_box_px": union},
                ),
                "reading_attempt": attempt,
                "n": first_count + act["n"],
                "reading_n": act["n"],
                "entry_kind": entry_kind,
                "act_class": act_class,
                "cited_ids": list(entry["cited_ids"]),
                "region_boxes_px": list(entry["region_boxes_px"]),
                "union_box_px": union,
                "region_holds": region_holds,
                "reading_holds": reading_holds,
                "text": text,
                "assessment": assessment,
                "truncation": record,
                "autopsia": autopsia,
            }
        )
    if superseded is not None and not superseded_acts_kept(superseded, plans):
        for plan in plans:
            plan["reading_holds"].append(SUPERSEDED_ACT_NOT_READ)
    return plans


def page_doubt(plans: list[Mapping[str, Any]]) -> tuple[int, int]:
    """`(doubtful or unread, out of)` over a page's entry plans together."""
    counts = [annotations.doubt_count(plan["text"], plan["assessment"]) for plan in plans]
    return sum(count[0] for count in counts), sum(count[1] for count in counts)


def hold_doubtful_page(
    plans: list[dict[str, Any]], policy: page_accounting.PageAccountingPolicy
) -> None:
    """Hold every plan `page-doubt-share-high` when the page's entries together are over the limit.

    Given every entry the page publishes act records for, first reading and
    counted re-ask together, once, before any of them is published.
    """
    if plans and annotations.doubt_exceeds(page_doubt(plans), policy.max_page_doubt_share_bp):
        for plan in plans:
            plan["reading_holds"].append(PAGE_DOUBT_SHARE_HIGH)


def superseded_acts_kept(
    superseded: list[Mapping[str, Any]], plans: list[Mapping[str, Any]]
) -> bool:
    """Whether a re-read reads every act of the reading it replaces as one act of its own.

    The rule follows acts by the ids they cite, and only by them. An `act` entry
    of `superseded` is followed by its own ids, those no other superseded act
    cites: all of them must be cited by exactly one `act` entry of the re-read,
    and that entry may cite no other superseded act's own ids. The re-read must
    name at least as many acts citing an id as the reading it replaces names acts. An act with no id of
    its own (none at all, or only ids another act shares) cannot be followed; while
    there is one, the re-read's acts may cite no id the superseded acts did not,
    so a new act cannot stand in for it. A merge, a split, a relabel as anything
    but an act, an act left out or set aside, and a moved boundary each fail.

    What it cannot see: text. An act read again over the same ids with other
    words, or two acts whose texts are swapped between their entries, keeps.
    """
    acts = [entry for entry in superseded if entry["act"]["kind"] == "act"]
    # An act entry citing nothing reads no ink, so it cannot stand for a replaced act.
    readings = [
        set(plan["cited_ids"])
        for plan in plans
        if plan["act"]["kind"] == "act" and plan["cited_ids"]
    ]
    if len(readings) < len(acts):
        return False
    owners: dict[str, int] = {}
    for entry in acts:
        for cited in set(entry["cited_ids"]):
            owners[cited] = owners.get(cited, 0) + 1
    own = [{cited for cited in entry["cited_ids"] if owners[cited] == 1} for entry in acts]
    if any(not ids for ids in own) and not set().union(*readings) <= set(owners):
        return False
    for index, ids in enumerate(own):
        if not ids:
            continue
        others = set().union(*(other for at, other in enumerate(own) if at != index))
        covering = [cited for cited in readings if cited & ids]
        if len(covering) != 1 or not ids <= covering[0] or covering[0] & others:
            return False
    return True


def keeps_counted(plans: list[Mapping[str, Any]]) -> bool:
    """Whether an operator re-read's plans become what the page's next re-read is planned
    against: it read something and kept every act it replaced. A re-read that dropped
    one never does, so a later re-read cannot launder the drop."""
    return bool(plans) and SUPERSEDED_ACT_NOT_READ not in plans[0]["reading_holds"]


def reask_act_plans(
    accounting: Mapping[str, Any],
    first_plans: list[dict[str, Any]],
    reask_plans: list[dict[str, Any]],
    what: str,
) -> list[dict[str, Any]]:
    """The plans a re-asked page publishes act records for, in the accounting's order.

    `accounting` is the page's combined `page-accounting` payload, and
    `first_plans` and `reask_plans` its two readings' `entry_plans`. The
    first reading's entries always, and the re-ask's only when the
    accounting counts it (`page_accounting.reask_stood`); refused
    (`FatalAccounting`) unless they are exactly the entries the accounting
    counts, by reading, number in that reading and number on the page, so
    no act record is published for an entry nothing measured and none the
    accounting measured is left without one.
    """
    plans = first_plans + (reask_plans if page_accounting.reask_stood(accounting) else [])
    counted = [
        (entry["reading_attempt"], entry["reading_n"], entry["n"])
        for entry in accounting["entries"]
    ]
    if counted != [(plan["reading_attempt"], plan["reading_n"], plan["n"]) for plan in plans]:
        raise FatalAccounting(
            f"{what}'s re-ask accounting counts other entries than its readings give act "
            "records for; an act nothing measured, or a measured entry with no act, is never "
            "counted"
        )
    return plans


# --- the page feed ----------------------------------------------------------------


def declared_page_witness_chairs(context) -> set[str]:
    """The page witnesses of the sealed roster, read from the sealed model configuration.

    A consumer may not inherit trust across a stage boundary. The uniqueness and roster
    checks stop a duplicate or a nonexistent chair from silently erasing page coverage.
    """
    roster = context.witness_chairs
    # Exact `str`, not `isinstance`: set construction and refusal formatting would run
    # subclass code.
    if (
        not isinstance(roster, list)
        or any(type(chair) is not str for chair in roster)
        or len(roster) != len(set(roster))
    ):
        raise SchemaRefusal(
            "the sealed witness roster is not a unique list of chair names. Page-witness scope "
            "cannot be derived from this run authority. Start a new run from the sealed models "
            "configuration; do not edit the existing run"
        )
    configured = context.registry.config.chairs
    unknown = set(roster) - set(configured)
    if unknown:
        raise SchemaRefusal(
            "the sealed witness roster names chair(s) absent from the current models "
            "configuration: "
            f"{sorted(unknown)} not in {sorted(configured)}. The run authority and current models "
            "configuration do not describe the same witness set. Reopen the run with its original "
            "models configuration or start a new run; do not edit sealed evidence"
        )
    return {
        chair
        for chair in roster
        if isinstance(configured[chair], ChairIdentity)
        and configured[chair].witness_scope == "page"
    }


def page_witness_chairs(context, page_id: str, declared: set[str] | None = None) -> set[str]:
    """One page's page witnesses: the sealed roster's, less a routed chair not routed to it.

    `declared` is `declared_page_witness_chairs(context)`, read once by a
    caller that walks many pages. A run that routes no witness
    (`common/witness_routing.py`) gets the sealed roster for every page.
    """
    # `witness_routing` reads this module.
    from common.witness_routing import page_roster

    if declared is None:
        declared = declared_page_witness_chairs(context)
    return page_roster(context, page_id, declared)


def require_page_roster(page_id: str, records: list[dict], page_chairs: set[str]) -> None:
    """A page some witness testified to carries every configured page witness and no other."""
    present = {record["payload"]["chair"] for record in records}
    if present - page_chairs:
        raise FatalAccounting(
            f"page {page_id} carries page Testimonia from chair(s) "
            f"{sorted(present - page_chairs)}, which this run did not seal as page witnesses"
        )
    if page_chairs - present:
        raise FatalAccounting(
            f"page {page_id} has no current page Testimonium for configured page witness(es) "
            f"{sorted(page_chairs - present)}; it cannot be read or counted over a shortened "
            "roster"
        )


def page_witnesses(
    context, page_id: str, current: list[dict[str, Any]], page_chairs: set[str]
) -> list[dict[str, Any]]:
    """Every configured page witness's current Testimonium for one page, as the feed takes it.

    `current` is each chair's latest page Testimonium of the page. Called only
    for a page some witness testified to: a page with none is read as
    `no-witness-testimony`, but a roster chair missing beside others that
    testified is a shortened roster and refuses.
    """
    # `witness_regime` reads `common.stage`, which reads this module.
    from common.witness_regime import witness_label

    require_page_roster(page_id, current, page_chairs)
    by_chair = {record["payload"]["chair"]: record for record in current}
    return [
        {
            "chair": chair,
            "witness_label": witness_label(
                chair,
                regime=context.witness_context,
                run_id=context.tree.run_id,
                config_digest=context.config_digest,
            ),
            "adapter": context.registry.resolve(chair).witness_adapter,
            "testimonium": by_chair[chair],
            "testimonium_ref": context.artifact_ref(
                ATTESTATORES, PAGE_TESTIMONIUM_KIND, by_chair[chair]["artifact_id"]
            ),
        }
        for chair in sorted(page_chairs)
    ]


def _feed_surya(census: dict[str, dict[str, Any]] | None, page_id: str) -> Any:
    # The serving package reads `common.stage`, which reads this module.
    from common import page_feed

    if census is None:
        return page_feed.SURYA_ABSENT
    if page_id not in census:
        raise FatalAccounting(
            f"this run seals Surya censuses, but none for page {page_id}; the page cannot be "
            "shown its detections"
        )
    return census[page_id]


def _feed_render(
    context,
    protocol_config: Mapping[str, Any],
    page_id: str,
    ordinal: int,
    retain: Callable[[bytes, str], dict[str, str]] | None,
):
    """The page image the sealed `page_image` switch shows, or `None` when it is off."""
    setting = protocol_config["feed"]["page_image"]
    if setting == "off":
        return None
    return page_render.build_page_render(
        context,
        source_page_id=page_id,
        source_page_ordinal=ordinal,
        page_context=protocol_config["page_context"],
        full_page=setting == "full",
        retain=retain,
    )


def page_feed_of(
    context,
    *,
    page_id: str,
    ordinal: int,
    page_size: tuple[int, int],
    protocol_config: Mapping[str, Any],
    page_chairs: set[str],
    current: list[dict[str, Any]],
    surya_census: dict[str, dict[str, Any]] | None,
    serving_recipe: str | None,
    fixture_placeholders: bool,
    retain: Callable[[bytes, str], dict[str, str]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, str]]]:
    """One sealed page's `page-feed` payload, its page witnesses and its inputs.

    Stage 4 builds and publishes the feed from this, and the page-read
    denominator builds it again from the same sealed inputs and requires the
    sealed feed to equal it. `current` is each chair's latest page Testimonium
    of the page, `page_chairs` `declared_page_witness_chairs` (narrowed here to
    the page's own roster, `page_witness_chairs`), `surya_census`
    `sealed_surya_census`, `serving_recipe` the Perlector chair's or `None`
    when it is absent. `retain` stores the page render (stage 4's
    `context.retain`) or, for a reader, checks the render is already retained.
    Returns the feed, the page witnesses `page_witnesses` gives (empty for a
    page no witness testified to) and the feed record's inputs.
    """
    # The serving package reads `common.stage`, which reads this module.
    from common import page_feed

    # A routed witness the page is not routed to is no part of this page's roster.
    page_chairs = page_witness_chairs(context, page_id, page_chairs)
    no_testimony = not current
    witnesses = [] if no_testimony else page_witnesses(context, page_id, current, page_chairs)
    feed = page_feed.build_page_feed(
        page_id=page_id,
        page_ordinal=ordinal,
        page_size=page_size,
        feed_switches=protocol_config["feed"],
        witness_regime=context.witness_context,
        roster=sorted(page_chairs),
        witnesses=witnesses,
        surya=_feed_surya(surya_census, page_id),
        page_render=_feed_render(context, protocol_config, page_id, ordinal, retain),
        serving_recipe=serving_recipe,
        read_bytes=context.tree.read_bytes,
        fixture_placeholders=fixture_placeholders,
        no_testimony=no_testimony,
    )
    return feed, witnesses, feed_inputs(context, feed, witnesses)


def feed_inputs(
    context, feed: Mapping[str, Any], witnesses: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Every record and image the feed names or was built from, digest-checked on disk.

    `witnesses` is every page witness of the sealed roster, shown or hidden:
    a hidden witness's Testimonium is an input too, since the accounting
    measures it.
    """
    named: list[dict[str, str] | None] = [
        witness["testimonium_ref"] for witness in feed["witnesses"]
    ] + [witness["testimonium_ref"] for witness in witnesses]
    render = feed["page_render"]
    if render is not None:
        named += [
            dict(render["source"]),
            {"relative_path": render["image_path"], "sha256": render["image_sha256"]},
        ]
    surya = feed["surya"]
    if surya is not None:
        named.append(surya["census_ref"])
        named += [row["ref"] for row in surya["lines"] + surya["blocks"]]
    references = distinct_refs(named)
    for reference in references:
        if context.input_ref(reference["relative_path"]) != reference:
            raise SchemaRefusal(
                f"the page feed names {reference!r}, but the bytes at that path differ"
            )
    return references


# --- the Perlectio -------------------------------------------------------------------


def _comparison_text(text: str, capabilities: Any) -> str:
    """A witness's text as dissent compares it: its own doubt markers removed when it has any.

    A witness whose declared format can express uncertainty writes its doubt
    inline (DAI's `[UNCERTAIN]`, `[CROSSED_OUT]`); those characters are the
    witness's doubt, not a reading the Perlector departed from.
    """
    if isinstance(capabilities, Mapping) and capabilities.get("can_express_uncertainty") is True:
        return bracket_marker_view(text)
    return text


def page_dissent(
    text: str,
    feed: Mapping[str, Any],
    cited_ids: list[str],
    witnesses: list[dict[str, Any]],
    max_comparison_steps: int,
) -> list[dict[str, Any]]:
    """Where an entry's reading departed from each shown witness's cited units.

    Each alignment runs under `max_comparison_steps`, the run's sealed
    `[dissent] max_comparison_steps`, a counted bound, so the same entry, feed
    and budget always give the same dissent; one that needs more is recorded as
    not compared (`dissent.unaligned_row`).
    """
    capabilities = {
        witness["witness_label"]: witness["testimonium"]["payload"].get("format_capabilities")
        for witness in witnesses
    }
    cited = set(cited_ids)
    rows = []
    for witness in feed["witnesses"]:
        head = {"letter": witness["letter"], "witness_label": witness["witness_label"]}
        if witness["outcome"] != READ_OUTCOME:
            reason = f"this witness's page outcome is {witness['outcome']}; it has no units"
            rows.append({**head, "cited_units": [], "compared": False, "reason": reason})
            continue
        units = [unit for unit in witness["units"] if unit["id"] in cited]
        if not units:
            reason = "no unit of this witness is cited by this entry"
            rows.append({**head, "cited_units": [], "compared": False, "reason": reason})
            continue
        reported = _comparison_text(
            "\n".join(unit["text"] for unit in units), capabilities[witness["witness_label"]]
        )
        row = dissent.dissent_against(text, reported, max_comparison_steps=max_comparison_steps)
        rows.append({**head, "cited_units": [unit["id"] for unit in units], **row})
    validate_page_dissent(
        rows, text=text, feed=feed, cited_ids=cited_ids, max_comparison_steps=max_comparison_steps
    )
    return rows


_PAGE_DISSENT_HEAD: Final = ("letter", "witness_label", "cited_units")


def validate_page_dissent(
    rows: Any,
    *,
    text: str,
    feed: Mapping[str, Any],
    cited_ids: list[str],
    max_comparison_steps: int,
) -> None:
    """Refuse a page-path dissent record that loses a shown witness or misstates one row.

    One row per shown witness, in the feed's order. A witness with no reading or
    no unit this entry cites was not compared, and says so; every other row is
    a dissent row (`dissent.validate_row`) under the witness's letter, the run's
    sealed budget included.
    """
    if not isinstance(rows, list) or len(rows) != len(feed["witnesses"]):
        raise SchemaRefusal("a page-path dissent record does not have one row per shown witness")
    cited = set(cited_ids)
    for index, (row, witness) in enumerate(zip(rows, feed["witnesses"], strict=True)):
        if not isinstance(row, dict) or any(field not in row for field in _PAGE_DISSENT_HEAD):
            raise SchemaRefusal(f"page dissent[{index}] has no witness head")
        if (row["letter"], row["witness_label"]) != (witness["letter"], witness["witness_label"]):
            raise SchemaRefusal(f"page dissent[{index}] names another witness than the feed's")
        units = [
            unit["id"]
            for unit in witness["units"]
            if witness["outcome"] == READ_OUTCOME and unit["id"] in cited
        ]
        if row["cited_units"] != units:
            raise SchemaRefusal(f"page dissent[{index}] misstates the units this entry cites")
        rest = {key: value for key, value in row.items() if key not in _PAGE_DISSENT_HEAD}
        if not units:
            if (
                set(rest) != {"compared", "reason"}
                or rest["compared"] is not False
                or not isinstance(rest["reason"], str)
                or not rest["reason"]
            ):
                raise SchemaRefusal(
                    f"page dissent[{index}] claims a comparison for a witness it could not compare"
                )
            continue
        try:
            dissent.validate_row(rest, text=text, max_comparison_steps=max_comparison_steps)
        except SchemaRefusal as error:
            raise SchemaRefusal(f"page dissent[{index}]: {error}") from error


def dissent_holds(
    rows: Any,
    text: str,
    feed: Mapping[str, Any],
    cited_ids: list[str],
    witnesses: list[dict[str, Any]],
    max_comparison_steps: int,
) -> bool:
    """Whether a sealed Perlectio's dissent is exactly the one its entry, feed and budget give."""
    return rows == page_dissent(text, feed, cited_ids, witnesses, max_comparison_steps)


def expected_perlectio(
    *,
    page_id: str,
    ordinal: int,
    plan: Mapping[str, Any],
    refs: Mapping[str, dict[str, str]],
    page_holds: list[str],
    reading: Mapping[str, Any],
) -> dict[str, Any]:
    """Every field of an entry's `perlectio.v3` but its dissent, from its plan and page records.

    `refs` names the entry's `act_region_ref` and the page's
    `page_reading_ref`, `page_accounting_ref` and `feed_ref`; `reading` is the
    `page-reading` payload, whose `engine_call` and `provenance` the Perlectio
    repeats. Stage 4 publishes this with the entry's `page_dissent`, adopts a
    sealed Perlectio only when it holds exactly these fields, and the
    page-read denominator requires them of every Perlectio it counts.
    """
    act, assessment = plan["act"], plan["assessment"]
    return {
        "schema": PERLECTIO_SCHEMA,
        "page_id": page_id,
        "page_ordinal": ordinal,
        "act_region_ref": refs["act_region_ref"],
        "page_reading_ref": refs["page_reading_ref"],
        "page_accounting_ref": refs["page_accounting_ref"],
        "feed_ref": refs["feed_ref"],
        "n": plan["n"],
        "kind": act["kind"],
        "entry_kind": plan["entry_kind"],
        "label": act.get("label"),
        "text": plan["text"],
        "uncertain_spans": assessment["uncertain_spans"],
        "gaps": assessment["gaps"],
        "uncertainty_assessment": assessment,
        "truncation": plan["truncation"],
        "autopsia": plan["autopsia"],
        "continues_from_previous_page": act["continues_from_previous_page"],
        "continues_to_next_page": act["continues_to_next_page"],
        "holds": list(plan["reading_holds"]),
        "page_holds": list(page_holds),
        "engine_call": reading["engine_call"],
        "provenance": reading["provenance"],
        **recovered_fields(plan),
    }


# What a Perlectio of an entry the re-ask recovered holds beyond `PERLECTIO_FIELDS`.
RECOVERED_FIELDS: Final = frozenset({"reading_attempt", "reading_n"})


def is_perlectio_field_set(payload: Mapping[str, Any]) -> bool:
    """True when `payload` holds exactly a first reading's Perlectio fields, or those
    and `RECOVERED_FIELDS` with the re-ask's `reading_attempt`, and its `entry_kind`
    is an entry kind whose act class is its `kind`."""
    fields = set(payload)
    if "entry_kind" in fields and (
        payload["entry_kind"] not in page_types.ENTRY_KINDS
        or page_types.act_class(payload["entry_kind"]) != payload.get("kind")
    ):
        return False
    if fields == PERLECTIO_FIELDS:
        return True
    return (
        fields == PERLECTIO_FIELDS | RECOVERED_FIELDS
        and payload["reading_attempt"] == page_edges.REASK_READING
    )


def recovered_fields(plan: Mapping[str, Any]) -> dict[str, int]:
    """What an entry the re-ask recovered adds to its act-region and Perlectio, else nothing.

    `reading_attempt: 2` and `reading_n`, its number in the re-ask's answer,
    so a recovered entry is always told from a first reading's; its `n` is
    the page accounting's.
    """
    if plan["reading_attempt"] != page_edges.REASK_READING:
        return {}
    return {"reading_attempt": page_edges.REASK_READING, "reading_n": plan["reading_n"]}


# --- the page accounting's inputs -----------------------------------------------


def _fields(payload: Any, names: tuple[str, ...], what: str) -> dict[str, Any]:
    """The named fields of a sealed payload, or a refusal naming the first one missing."""
    if not isinstance(payload, dict):
        raise FatalAccounting(f"{what} carries no payload object")
    for name in names:
        if name not in payload:
            raise FatalAccounting(f"{what} carries no {name!r}")
    return {name: payload[name] for name in names}


def accounting_witnesses(
    feed: Mapping[str, Any],
    witnesses: list[dict[str, Any]],
    *,
    read_bytes,
    fixture_placeholders: bool,
) -> list[dict[str, Any]]:
    """Every sealed page witness as the accounting measures it, shown or hidden.

    `witnesses` is `[{witness_label, adapter, testimonium}]`, one per chair of
    the sealed page-witness roster. A shown witness is its feed row. A hidden
    one is read from its Testimonium's retained bytes and takes the next letter
    the feed did not use, in sorted `witness_label` order, so its ids never
    collide with a shown one's.
    """
    shown = {row["witness_label"]: row for row in feed["witnesses"]}
    used = {row["letter"] for row in shown.values()}
    free = iter(letter for letter in WITNESS_LETTERS if letter not in used)
    page_size = (feed["page_size"]["w"], feed["page_size"]["h"])
    rows = []
    for witness in sorted(witnesses, key=lambda item: item["witness_label"]):
        testimonium = witness["testimonium"]
        outcome = testimonium["outcome"]
        text = testimonium["payload"].get("payload")
        # Measured from the retained text itself, never from its self-reported health.
        blank = (
            text.strip() == ""
            if outcome in WITNESS_READING_OUTCOMES and isinstance(text, str)
            else None
        )
        row = shown.get(witness["witness_label"])
        if row is not None:
            letter = row["letter"]
            units = [
                {"id": unit["id"], "box_px": unit["box_px"], "text": unit["text"]}
                for unit in row["units"]
            ]
        else:
            letter = next(free)
            reading = witness_reading(
                testimonium,
                adapter=witness["adapter"],
                page_size=page_size,
                read_bytes=read_bytes,
                fixture_placeholders=fixture_placeholders,
            )
            units = [
                {"id": f"{letter}{number}", "box_px": unit["box_px"], "text": unit["text"]}
                for number, unit in enumerate(reading["units"], start=1)
            ]
        rows.append({"letter": letter, "outcome": outcome, "blank": blank, "units": units})
    return rows


_SURYA_SUBJECT: Final = re.compile(r"^(?P<page>.+)-surya-(?P<kind>line|block)-(?P<n>[1-9][0-9]*)$")
_SURYA_CENSUS_FIELDS: Final = (
    "page_id",
    "line_count",
    "block_count",
    "line_subjects",
    "block_subjects",
    "reading_order",
    "reading_order_reason",
)
_SURYA_DETECTION_FIELDS: Final = {
    SURYA_LINE_KIND: ("page_id", "n", "bounds", "confidence_bp"),
    SURYA_BLOCK_KIND: (
        "page_id",
        "n",
        "bounds",
        "confidence_bp",
        "label",
        "reading_order_position",
        "reading_order",
    ),
}


def _surya_detections(
    context,
    kind: str,
    page_id: str,
    subjects: Any,
    count: Any,
    entries: dict[tuple[str, str], dict[str, Any]],
    reading_order: str,
) -> list[dict[str, Any]]:
    """One page's Surya lines or blocks, in the census's order, checked against it."""
    what = f"page {page_id}'s Surya census"
    if (
        not isinstance(subjects, list)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or len(subjects) != count
    ):
        raise FatalAccounting(f"{what} names {kind} subjects that are not a list of its count")
    rows = []
    for n, subject in enumerate(subjects, start=1):
        match = _SURYA_SUBJECT.match(subject) if isinstance(subject, str) else None
        if (
            match is None
            or match["page"] != page_id
            or f"surya-{match['kind']}" != kind
            or int(match["n"]) != n
        ):
            raise FatalAccounting(f"{what} names {subject!r} as its {kind} {n}")
        entry = entries.get((kind, subject))
        if entry is None:
            raise FatalAccounting(f"{what} names {subject!r}, which the Designator never sealed")
        record = context.tree.read_artifact(DESIGNATOR, kind, entry["artifact_id"])
        payload = _fields(record["payload"], _SURYA_DETECTION_FIELDS[kind], f"Surya {subject}")
        if payload["page_id"] != page_id or payload["n"] != n:
            raise FatalAccounting(
                f"Surya {subject} states page {payload['page_id']!r}, n {payload['n']!r}, "
                f"where its census places it on page {page_id!r} as {n}"
            )
        row = {
            "box_px": payload["bounds"],
            "confidence_bp": payload["confidence_bp"],
            "ref": context.artifact_ref(DESIGNATOR, kind, entry["artifact_id"]),
        }
        if kind == SURYA_BLOCK_KIND:
            if payload["reading_order"] != reading_order:
                raise FatalAccounting(
                    f"Surya {subject} states reading order {payload['reading_order']!r}, "
                    f"but its page census states {reading_order!r}"
                )
            row["label"] = payload["label"]
            row["position"] = payload["reading_order_position"]
        rows.append(row)
    return rows


def sealed_surya_census(
    context, designator_entries: list[dict[str, Any]]
) -> dict[str, dict[str, Any]] | None:
    """Every page's Surya census as the page feed takes it, or `None` when the run has none.

    Read from the Designator's stage-2 records (`designator_entries`, its
    manifest's artifacts): each `surya-page` (subject page_id) names its
    `line_subjects` and `block_subjects` in Surya's order with their counts,
    and how Surya ordered the blocks (`reading_order`, and
    `reading_order_reason` for a raster fallback; the feed calls them
    `block_sequence`); each `surya-line` and `surya-block` carries `n`, its
    `bounds` and `confidence_bp`, and a block its `label`,
    `reading_order_position` and the page's `reading_order`. The census and
    its detections must agree exactly, and every sealed detection must be
    named by its page's census. This is the one place that shape is read.
    """
    censuses = [entry for entry in designator_entries if entry["kind"] == SURYA_PAGE_KIND]
    if not censuses:
        return None
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in designator_entries:
        if entry["kind"] in (SURYA_LINE_KIND, SURYA_BLOCK_KIND):
            key = (entry["kind"], entry["subject_id"])
            if key in entries:
                raise FatalAccounting(f"the Designator sealed {key[1]!r} twice")
            entries[key] = entry
    by_page: dict[str, dict[str, Any]] = {}
    named: set[tuple[str, str]] = set()
    for entry in censuses:
        page_id = entry["subject_id"]
        record = context.tree.read_artifact(DESIGNATOR, SURYA_PAGE_KIND, entry["artifact_id"])
        census = _fields(record["payload"], _SURYA_CENSUS_FIELDS, f"page {page_id}'s Surya census")
        if census["page_id"] != page_id:
            raise FatalAccounting(f"page {page_id}'s Surya census names another page")
        if census["reading_order"] not in SURYA_READING_ORDERS:
            raise FatalAccounting(
                f"page {page_id}'s Surya census states reading order "
                f"{census['reading_order']!r}, which is not one Surya gives"
            )
        lines, blocks = (
            _surya_detections(
                context,
                kind,
                page_id,
                census[f"{name}_subjects"],
                census[f"{name}_count"],
                entries,
                census["reading_order"],
            )
            for kind, name in ((SURYA_LINE_KIND, "line"), (SURYA_BLOCK_KIND, "block"))
        )
        named |= {(SURYA_LINE_KIND, s) for s in census["line_subjects"]}
        named |= {(SURYA_BLOCK_KIND, s) for s in census["block_subjects"]}
        by_page[page_id] = {
            "census_ref": context.artifact_ref(DESIGNATOR, SURYA_PAGE_KIND, entry["artifact_id"]),
            "block_sequence": census["reading_order"],
            "block_sequence_reason": census["reading_order_reason"],
            "lines": lines,
            "blocks": blocks,
        }
    unnamed = sorted(subject for _kind, subject in set(entries) - named)
    if unnamed:
        raise FatalAccounting(
            f"the Designator sealed Surya detections {unnamed} that no page census names"
        )
    return by_page


def _one_record(entries: list[dict[str, Any]], kind: str, subject: str) -> dict[str, Any]:
    found = [entry for entry in entries if entry["kind"] == kind and entry["subject_id"] == subject]
    if len(found) != 1:
        raise FatalAccounting(
            f"the Designator sealed {len(found)} {kind} records for {subject!r}, not one"
        )
    return found[0]


def _dai_unit_ids(
    feed: Mapping[str, Any], witnesses: list[dict[str, Any]]
) -> dict[tuple[int, int, int, int], list[str]]:
    """The feed ids of DAI's units by box, in the feed's order.

    DAI reads one unit per detector record it was shown, boxed by that record's
    bounds, so a detector record and the DAI unit with its box are one record.
    A DAI the feed hides has no row and names none.
    """
    labels = {w["witness_label"] for w in witnesses if w["adapter"] == DAI}
    ids: dict[tuple[int, int, int, int], list[str]] = {}
    for row in feed["witnesses"]:
        if row["witness_label"] not in labels:
            continue
        for unit in row["units"]:
            box = unit["box_px"]
            if box is not None:
                ids.setdefault((box["x"], box["y"], box["w"], box["h"]), []).append(unit["id"])
    return ids


def detector_max_det(read_bytes, raw_output_ref: Any, page_id: str) -> int | None:
    """The cap the record detector ran at on one page, from its retained output's run facts.

    `None` when the run facts state no `max_det`, so whether the detector
    stopped at its cap is unknown; a stated cap that is not a positive integer
    refuses by name.
    """
    output = json.loads(read_verified(read_bytes, raw_output_ref, "a detector output"))
    if not isinstance(output, dict) or not isinstance(output.get("run"), dict):
        raise FatalAccounting(f"page {page_id}'s detector output carries no run facts")
    if "max_det" not in output["run"]:
        return None
    max_det = output["run"]["max_det"]
    if not is_plain_int(max_det) or max_det < 1:
        raise FatalAccounting(f"page {page_id}'s detector run facts state a max_det of {max_det!r}")
    return max_det


def empty_detector_page(
    context, designator_entries: list[dict[str, Any]], page_id: str
) -> dict[str, str] | None:
    """The page's `detector-page` reference when the record detector saw nothing on it.

    That is a census of no record from a detector whose retained run facts are
    complete, its cap (`max_det`) included, as rule (i) reads them. `None` when
    the detector published no census for the page, found records, or its run
    facts state no cap.
    """
    pages = [
        e for e in designator_entries if e["kind"] == "detector-page" and e["subject_id"] == page_id
    ]
    if not pages:
        return None
    entry = _one_record(designator_entries, "detector-page", page_id)
    census = _fields(
        context.tree.read_artifact(DESIGNATOR, "detector-page", entry["artifact_id"])["payload"],
        ("raw_output_ref", "record_subjects", "detection_count"),
        f"page {page_id}'s detector-page",
    )
    if census["detection_count"] != 0 or census["record_subjects"] != []:
        return None
    if detector_max_det(context.tree.read_bytes, census["raw_output_ref"], page_id) is None:
        return None
    return context.artifact_ref(DESIGNATOR, "detector-page", entry["artifact_id"])


def _record_detections(
    context,
    designator_entries: list[dict[str, Any]],
    page_id: str,
    ordinal: int,
    unit_ids: dict[tuple[int, int, int, int], list[str]],
) -> tuple[Any, Any, list[dict[str, str]]]:
    """The record detector's records and census for one page, and the records read.

    Records and census are `None` when the detector published no
    `detector-page` for the page, or when its run facts state no `max_det`, so
    whether it stopped at its cap is unknown; the accounting then holds rule
    (i) as not measured, and the `detector-page` read is still an input. A
    record whose corners enclose no crop is given with no box, and the
    accounting reports it not measured. A record carries the feed id of the
    DAI unit that is that record, so setting that unit aside is seen as
    setting the record aside. The census and its records must agree exactly --
    count, subjects, order, page -- and every sealed record of the page must be
    named by its census, or the stage refuses by name.
    """
    entries = designator_entries
    prefix = f"{page_id}-detector-"
    sealed = {
        e["subject_id"]
        for e in entries
        if e["kind"] == "detector-record" and e["subject_id"].startswith(prefix)
    }
    pages = [e for e in entries if e["kind"] == "detector-page" and e["subject_id"] == page_id]
    if not pages:
        if sealed:
            raise FatalAccounting(
                f"the Designator sealed detector records {sorted(sealed)} but no "
                f"detector-page for page {page_id}"
            )
        return None, None, []
    entry = _one_record(entries, "detector-page", page_id)
    page_ref = context.artifact_ref(DESIGNATOR, "detector-page", entry["artifact_id"])
    what = f"page {page_id}'s detector-page"
    census = _fields(
        context.tree.read_artifact(DESIGNATOR, "detector-page", entry["artifact_id"])["payload"],
        ("page_ordinal", "raw_output_ref", "record_subjects", "detection_count"),
        what,
    )
    subjects, count = census["record_subjects"], census["detection_count"]
    if (
        not isinstance(subjects, list)
        or not all(isinstance(subject, str) for subject in subjects)
        or not is_plain_int(count)
        or len(subjects) != count
    ):
        raise FatalAccounting(f"{what} names record subjects that are not a list of its count")
    if census["page_ordinal"] != ordinal:
        raise FatalAccounting(f"{what} states page ordinal {census['page_ordinal']!r}")
    unnamed = sorted(sealed - set(subjects))
    if unnamed:
        raise FatalAccounting(
            f"the Designator sealed detector records {unnamed} that {what} does not name"
        )
    max_det = detector_max_det(context.tree.read_bytes, census["raw_output_ref"], page_id)
    if max_det is None:
        return None, None, [page_ref]
    unit_ids = {box: list(ids) for box, ids in unit_ids.items()}
    references = [page_ref]
    records = []
    for position, subject in enumerate(subjects):
        if subject != f"{prefix}{position}":
            raise FatalAccounting(f"{what} names {subject!r} as its record {position}")
        row = _one_record(entries, "detector-record", subject)
        record = context.tree.read_artifact(DESIGNATOR, "detector-record", row["artifact_id"])
        fields = _fields(
            record["payload"],
            ("page_ordinal", "detector_ordinal", "bounds"),
            f"detector record {subject}",
        )
        if (fields["page_ordinal"], fields["detector_ordinal"]) != (ordinal, position):
            raise FatalAccounting(
                f"detector record {subject} states page {fields['page_ordinal']!r}, "
                f"ordinal {fields['detector_ordinal']!r}"
            )
        bounds = fields["bounds"]
        reference = context.artifact_ref(DESIGNATOR, "detector-record", row["artifact_id"])
        references.append(reference)
        item: dict[str, Any] = {"box_px": bounds, "ref": reference}
        if isinstance(bounds, dict):
            same_box = unit_ids.get(tuple(bounds.get(name) for name in ("x", "y", "w", "h")))
            if same_box:
                item["id"] = same_box.pop(0)
        records.append(item)
    return (
        records,
        {"detection_count": count, "max_det": max_det, "max_det_reached": count >= max_det},
        references,
    )


def _accounting_detections(
    context,
    feed: Mapping[str, Any],
    witnesses: list[dict[str, Any]],
    surya_census: dict[str, dict[str, Any]] | None,
    designator_entries: list[dict[str, Any]],
    record_detector_configured: bool,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """The page's sealed detections as the accounting takes them, and their references."""
    page_id = feed["page_id"]
    surya, references = None, []
    if surya_census is not None:
        if page_id not in surya_census:
            raise FatalAccounting(
                f"this run seals Surya censuses, but none for page {page_id}; the page's "
                "lines cannot be accounted for"
            )
        census = surya_census[page_id]
        shown = feed["surya"] or {"lines": [], "blocks": []}
        surya = {
            kind: [
                {"id": row["id"], "box_px": row["box_px"], "ref": row["ref"]} for row in shown[kind]
            ]
            if shown[kind]
            else [{"box_px": row["box_px"], "ref": row["ref"]} for row in census[kind]]
            for kind in ("lines", "blocks")
        }
        references = [census["census_ref"]] + [
            row["ref"] for row in census["lines"] + census["blocks"]
        ]
    records, record_census, record_refs = (
        _record_detections(
            context,
            designator_entries,
            page_id,
            feed["page_ordinal"],
            _dai_unit_ids(feed, witnesses),
        )
        if record_detector_configured
        else (None, None, [])
    )
    return {
        "surya": surya,
        "records": records,
        "record_detector": "configured" if record_detector_configured else "absent",
        "record_census": record_census,
    }, references + record_refs


def _accounting_ink(
    context,
    ink_entries: list[dict[str, Any]],
    page_id: str,
    ordinal: int,
    page_size: tuple[int, int],
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """The page's retained ink runs and resolved coverage policy, or `None` when unmeasured.

    The runs must span the sealed page (`page_size`) and reconcile with the Ink
    Map's own edge finding and outcome before rule (f) counts them.
    """
    found = []
    for entry in ink_entries:
        if entry["kind"] != "ink-map":
            continue
        record = context.tree.read_artifact(INK_MAP, "ink-map", entry["artifact_id"])
        if record["payload"].get("page_ordinal") == ordinal:
            found.append((entry, record))
    if len(found) > 1:
        raise FatalAccounting(
            f"the Ink Map sealed {len(found)} ink maps for page {page_id}, not one; the "
            "page's residual ink cannot be measured against one of them by choice"
        )
    if not found:
        return None, []
    [(entry, record)] = found
    reference = context.artifact_ref(INK_MAP, "ink-map", entry["artifact_id"])
    if record["outcome"] == INK_NOT_MEASURABLE:
        refusal = validate_ink_not_measurable_payload(record["payload"])
        context.require_sealed_config("ink-map", refusal["background_config_sha256"])
        return None, [reference]
    if record["outcome"] not in {"mapped", "unclaimed-edge-ink"}:
        raise ContractError(f"page {page_id}'s ink map has an unknown outcome")
    coverage = load_coverage_audit_config(context.args.ink_map_config)
    context.require_sealed_config("ink-map", coverage["config_sha256"])
    measured = validate_measured_ink_map_payload(
        record["payload"],
        audit_contrast=MINIMUM_CONTRAST_BELOW_BACKGROUND,
        ink_margin_bp=coverage["ink_margin_bp"],
    )
    if coverage["config_sha256"] != measured["background_config_sha256"]:
        raise ContractError(
            f"page {page_id}'s ink map and the coverage policy read different sealed bytes"
        )
    runs = record["payload"]["edge_findings"]
    coverage_policy = resolve_coverage_audit_policy(coverage, *page_size)
    initial_measure = reconcile_edge_finding_with_runs(
        record["payload"]["edge"],
        runs,
        coverage_policy=coverage_policy,
        expected_dimensions=page_size,
    )
    if record["outcome"] != ("unclaimed-edge-ink" if initial_measure["flagged"] else "mapped"):
        raise ContractError(
            f"page {page_id}'s ink map outcome disagrees with its retained edge measurement"
        )
    return {"runs": runs, "coverage_policy": coverage_policy}, [reference]


def accounting_inputs(
    context,
    *,
    feed: Mapping[str, Any],
    feed_ref: dict[str, str],
    reading: Mapping[str, Any],
    reading_ref: dict[str, str],
    witnesses: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    surya_census: dict[str, dict[str, Any]] | None,
    designator_entries: list[dict[str, Any]],
    ink_entries: list[dict[str, Any]],
    record_detector_configured: bool,
    fixture_placeholders: bool,
    reask: Mapping[str, Any] | None = None,
    attempt: int = page_edges.FIRST_READING,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """What the page accounting measures one page reading against, and the records it came from.

    `attempt` is the whole-page reading's ordinal: the first reading's, or an
    operator re-read's, which the accounting is bound to (`page_accounting`).

    With `reask` -- `{reading, reading_ref, plans, named}`, the page's re-ask
    `page-reading` payload and reference, its `entry_plans` (empty unless it
    was read) and its named ids -- it is what the re-ask's accounting
    measures: `reading` and `plans` stay the first reading's, and the
    accounting names the re-ask's reading and inputs both.

    `reading` is the `page-reading` payload; `witnesses` is `[{witness_label,
    adapter, testimonium, testimonium_ref}]`, one per chair of the sealed
    page-witness roster (empty for a page no witness testified to); `plans`
    are `entry_plans` for a read answer, else empty. `surya_census` is
    `sealed_surya_census`, and `designator_entries` and `ink_entries` the
    Designator's and Ink Map's manifest artifacts. `record_detector_configured`
    says whether the sealed roster has a record detector, and
    `fixture_placeholders` whether the run is synthetic (a joined fixture
    Chandra page is then read).

    Returns the keyword arguments `page_accounting.page_accounting` takes
    besides its policy, and the page accounting's inputs: the feed, the
    reading, every Testimonium, and every Designator and Ink Map record read.
    """
    detections, detection_refs = _accounting_detections(
        context, feed, witnesses, surya_census, designator_entries, record_detector_configured
    )
    ink, ink_refs = _accounting_ink(
        context,
        ink_entries,
        feed["page_id"],
        feed["page_ordinal"],
        (feed["page_size"]["w"], feed["page_size"]["h"]),
    )
    measured = {
        "feed": feed,
        "witnesses": accounting_witnesses(
            feed,
            witnesses,
            read_bytes=context.tree.read_bytes,
            fixture_placeholders=fixture_placeholders,
        ),
        "detections": detections,
        "reading": {
            "parse_state": reading["parse_state"],
            "finish_reason": reading["finish_reason"],
            "answer": reading["answer"],
        },
        "entry_truncation": {
            plan["act"]["n"]: plan["truncation"]["classification"]
            for plan in plans
            if plan["truncation"] is not None
        },
        "ink": ink,
        "feed_ref": feed_ref,
        "page_reading_ref": reading_ref if reask is None else reask["reading_ref"],
        "attempt": attempt,
        "reask": None
        if reask is None
        else {
            "reading": {
                "parse_state": reask["reading"]["parse_state"],
                "finish_reason": reask["reading"]["finish_reason"],
                "answer": reask["reading"]["answer"],
            },
            "named": list(reask["named"]),
            "entry_truncation": {
                plan["act"]["n"]: plan["truncation"]["classification"]
                for plan in reask["plans"]
                if plan["truncation"] is not None
            },
        },
    }
    inputs = distinct_refs(
        [
            feed_ref,
            reading_ref,
            *([] if reask is None else [reask["reading_ref"]]),
            *(witness["testimonium_ref"] for witness in witnesses),
            *detection_refs,
            *ink_refs,
        ]
    )
    return measured, inputs
