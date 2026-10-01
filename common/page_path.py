"""The Perlector's page path: what its records derive, for the stage that writes them and every reader.

Stage 4 (`pipeline/4_perlector/page_run.py`)
reads each sealed page whole and publishes, per page, a `page-feed`, a
`page-reading`, a `page-accounting` and, for each entry of a read answer, one
`act-region` and one `perlectio.v3`. Everything those records hold that is
derived rather than given is derived here, once:

- `answer_problems`: what holds a parsed answer whole against its feed;
- `entry_plans`: each entry's identity, region, text, doubt marks, truncation
  and holds;
- `accounting_inputs`: every input the page accounting
  (`common/page_accounting.py`) measures the reading against -- the feed,
  every sealed witness of the page shown or hidden, the Designator's Surya and
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

from common import dissent, page_accounting, page_answer, page_render, truncation
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
from common.contracts.serving import reading_stop_reason
from common.contracts.stages import ATTESTATORES, DESIGNATOR, INK_MAP
from common.decoding import chair_decoding, engine_effective_sampling, recorded_wire_decimals
from common.page_witness_units import DAI, READ_OUTCOME, WITNESS_LETTERS, witness_reading
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
# Every kind the page reading publishes beside its Perlectios.
PAGE_PATH_KINDS: Final = frozenset(
    {PAGE_FEED_KIND, PAGE_READING_KIND, PAGE_ACCOUNTING_KIND, ACT_REGION_KIND}
)

# The attempt a page reading is: one reading per page; a second is later work.
PAGE_READ_OPERATION: Final = "page-read"
PAGE_READ_ORDINAL: Final = 1
ACT_REGION_OPERATION: Final = "reading-region"
PERLECTIO_OPERATION: Final = "perlegere"

# What became of a page's call, on its `page-reading`.
PARSED: Final = page_answer.PARSED
MALFORMED: Final = page_answer.MALFORMED
CUT_OFF: Final = "cut-off"
REFUSED_CAPACITY: Final = "refused-capacity"
CALL_FAILED: Final = "call-failed"
NOT_RUN: Final = "not-run"
READ: Final = "read"
HELD: Final = "held"

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

# The act classes an entry mints: placed on the page, or citing no boxed id.
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


def page_reading_attempt(page_id: str) -> str:
    return attempt_id(page_id, PAGE_READ_OPERATION, PAGE_READ_ORDINAL)


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


# --- the reply ------------------------------------------------------------------


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


def fixture_page_answer(context, ordinal: int) -> dict[str, Any]:
    """The synthetic fixture's one declared answer to page `ordinal` under this scenario."""
    rows = [
        row
        for row in context.fixture.get("page_answer", [])
        if row.get("scenario") == context.scenario and row.get("page_ordinal") == ordinal
    ]
    if len(rows) != 1:
        raise ContractError(
            f"the fixture declares {len(rows)} page answers for scenario {context.scenario!r}, "
            f"page {ordinal}; a page read offline needs exactly one"
        )
    row = rows[0]
    if not isinstance(row.get("answer"), str) or row.get("stop_reason", "stop") not in (
        "stop",
        "length",
    ):
        raise ContractError(
            f"the fixture's page answer for {context.scenario!r}, page {ordinal} is not an "
            "answer string with a stop reason of stop or length"
        )
    return row


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
    row: Any, serving_recipe: Any, feed: Mapping[str, Any], text: str, page_max_tokens: int
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
        page_max_tokens=page_max_tokens,
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
    """
    if not isinstance(engine_call, Mapping):
        raise ContractError("a live page reading's engine_call is not an object")
    body = read_verified(read_bytes, engine_call["raw_response_ref"], "a page reading's response")
    call = json.loads(
        read_verified(read_bytes, engine_call["call_record_ref"], "a page reading's call record")
    )
    if not isinstance(call, dict):
        raise ContractError("a page reading's call record is not a JSON object")
    try:
        content, finish_reason = reader.reading_reply(
            status=call.get("response_status"),
            body=body,
            kind=call.get("kind"),
            model_id=engine_call["served_model_id"],
        )
        stop_reason = reading_stop_reason(finish_reason)
    except (ContractError, ValueError) as error:
        raise ContractError(
            f"a page reading's retained response is not a reading: {error}"
        ) from error
    return {"content": content, "finish_reason": finish_reason, "stop_reason": stop_reason}


def read_reply(
    content: str,
    stop_reason: str | None,
    feed: Mapping[str, Any],
    accounting_policy: page_accounting.PageAccountingPolicy,
) -> tuple[str, Any, list[dict[str, Any]]]:
    """`(parse_state, answer, problems)` for a reply the engine finished or was cut on."""
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
        )
    state, answer, problems = page_answer.parse_page_answer(content)
    if state == PARSED:
        problems = answer_problems(answer, feed, stop_reason, accounting_policy)
    return state, answer, problems


# --- the answer -----------------------------------------------------------------
#
# The answer is read by `common/page_accounting.py`'s `validate_answer` against
# `feed_candidates`, the placement map the accounting measures against too, under
# the sealed page-accounting policy.


def answer_problems(
    answer: Any,
    feed: Mapping[str, Any],
    stop_reason: str | None,
    accounting_policy: page_accounting.PageAccountingPolicy,
) -> list[dict[str, Any]]:
    """Everything that holds a parsed answer whole, from the answer, its feed and the finish.

    The answer's own id problems against the feed, and `no-stop-reason` when
    the engine gave no finish reason. A reply cut at the output cap is not a
    parsed answer (`cut-off`) and is never given here.
    """
    candidates = page_accounting.feed_candidates(feed, accounting_policy)
    problems = page_accounting.validate_answer(answer, candidates)["problems"]
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
) -> list[dict[str, Any]]:
    """Each entry of a valid answer: the entry, its expanded ids, region and region holds."""
    candidates = page_accounting.feed_candidates(feed, accounting_policy)
    validated = page_accounting.validate_answer(answer, candidates)
    shared = {
        n
        for finding in page_accounting.duplicate_regions(validated["entries"], accounting_policy)
        for n in finding["ns"]
    }
    entries = []
    for act, entry in zip(answer["acts"], validated["entries"], strict=True):
        union = entry["union_box_px"]
        holds = [] if union is not None else [UNPLACED]
        if entry["n"] in shared:
            holds.append(DUPLICATE_REGION)
        entries.append(
            {
                "act": act,
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
) -> list[dict[str, Any]]:
    """Each entry of a read answer as it is published, from the answer, feed and sealed policies.

    Per entry: its act id and class, its region's boxes and their union box,
    its text and doubt layers (`reading_annotations.read_doubt_marks`), its
    truncation classification over its region's area (the union of its boxes,
    not the rectangle around them), and its own holds: the region's (`reading-unplaced`,
    `duplicate-region`, and `no-autopsia` when no page image was shown) and the
    reading's (`doubt-marks-malformed`, `reading-incomplete`,
    `entry-no-readable-text`). The page accounting reads the truncations from
    here, before any act record exists.
    """
    page_pixels = feed["page_size"]["w"] * feed["page_size"]["h"]
    attempt = page_reading_attempt(page_id)
    autopsia = feed["page_render"] is not None
    plans = []
    for entry in answer_entries(answer, feed, accounting_policy):
        act, union = entry["act"], entry["union_box_px"]
        act_class = READING_CLASS if union is not None else UNPLACED_CLASS
        region_holds = list(entry["holds"])
        if not autopsia:
            region_holds.append(NO_AUTOPSIA)
        text, assessment = annotations.read_doubt_marks(act["text"])
        reading_holds = list(region_holds)
        if assessment["state"] == annotations.ASSESSMENT_MALFORMED:
            reading_holds.append(DOUBT_MARKS_MALFORMED)
        record = (
            truncation.classify(
                text,
                region_pixels=page_accounting.region_area(entry["region_boxes_px"]),
                page_pixels=page_pixels,
                truncation_policy=truncation_policy,
                stop_reason=stop_reason,
            )
            if union is not None
            else None
        )
        if record is not None and truncation.holds_as_failure(record["classification"]):
            reading_holds.append(READING_INCOMPLETE)
        if not text.strip():
            reading_holds.append(ENTRY_NO_READABLE_TEXT)
        plans.append(
            {
                "act": act,
                "act_id": derive_act_id(
                    page_id,
                    act_class,
                    {"page_reading": attempt, "n": act["n"], "union_box_px": union},
                ),
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
    retain: Callable[[bytes], dict[str, str]] | None,
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
        crop_bounds=[],
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
    retain: Callable[[bytes], dict[str, str]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, str]]]:
    """One sealed page's `page-feed` payload, its page witnesses and its inputs.

    Stage 4 builds and publishes the feed from this, and the page-read
    denominator builds it again from the same sealed inputs and requires the
    sealed feed to equal it. `current` is each chair's latest page Testimonium
    of the page, `page_chairs` `declared_page_witness_chairs`, `surya_census`
    `sealed_surya_census`, `serving_recipe` the Perlector chair's or `None`
    when it is absent. `retain` stores the page render (stage 4's
    `context.retain`) or, for a reader, checks the render is already retained.
    Returns the feed, the page witnesses `page_witnesses` gives (empty for a
    page no witness testified to) and the feed record's inputs.
    """
    # The serving package reads `common.stage`, which reads this module.
    from common import page_feed

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
        return bracket_marker_view(text)["text"]
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
        compared = dissent.dissent_against(
            text,
            [
                {
                    "outcome": READ_OUTCOME,
                    "payload": {"chair": witness["letter"], "comparison_reported": reported},
                }
            ],
            max_comparison_steps=max_comparison_steps,
        )
        if len(compared) != 1:
            raise ContractError(f"dissent gave {len(compared)} rows for one witness, not one")
        row = dict(compared[0])
        row.pop("chair")
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
    an act-path dissent row under the witness's letter and goes through
    `dissent.validate_dissent`, the run's sealed budget included.
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
            dissent.validate_dissent(
                [{"chair": row["letter"], **rest}],
                text=text,
                basis_testimonia=[{"chair": row["letter"], "outcome": READ_OUTCOME}],
                max_comparison_steps=max_comparison_steps,
            )
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
        "n": act["n"],
        "kind": act["kind"],
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
    }


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
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """What the page accounting measures one page reading against, and the records it came from.

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
        "page_reading_ref": reading_ref,
    }
    inputs = distinct_refs(
        [
            feed_ref,
            reading_ref,
            *(witness["testimonium_ref"] for witness in witnesses),
            *detection_refs,
            *ink_refs,
        ]
    )
    return measured, inputs
