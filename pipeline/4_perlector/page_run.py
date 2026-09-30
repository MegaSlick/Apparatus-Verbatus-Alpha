"""The Perlector's page path: each sealed page read whole, its answer made into act records.

Under the sealed `reading_unit = "page"` (`protocol.py`) stage 4 does not read
the Designator's acts. It reads every sealed Exemplar page in one call, shown
the page image and every witness's page broken into that witness's own units,
and the Perlector establishes the acts itself (`page_feed.py`,
`page_prompt.py`, `common/page_answer.py`). Per page, in the order published:

    page-feed       (subject page_id)  what the reading is shown, every id it may cite
    reader-sent     (subject page_id)  live only, before the call leaves
    page-reading    (subject page_id)  the answer as given: parsed, or held whole
    page-accounting (subject page_id)  rules a-i over the reading (`common/page_accounting.py`)
    act-region      (subject act_id)   one per entry of a parsed, valid answer
    perlectio       (subject act_id)   `perlectio.v2`, one per entry

Every feed is built and published before any page is read, so a live pass
counts exactly the pages it will send before its chair starts.

The accounting is measured before any act record is published, so every act
record names it (`page_accounting_ref`) and carries the page's hold codes
(`page_holds`): an act on a held page is held. An answer that is not the
grammar, is cut off at the output cap, cites an id the feed does not define,
or could not be asked is held whole on its `page-reading` and makes no act
record: nothing is repaired, trimmed or split. An entry of a valid answer that
cites no placing id is still published, as a held `reading-unplaced` act with
no crop. Every page has a `page-reading`, a page the Exemplar refused
included, so no page is silently absent; every sealed page has a feed and an
accounting too, even one no witness testified to, one that shows nothing to
read, or one read in a run whose Perlector chair is absent.

Pass C, Lectio nuda and the primed-without-prior control read acts one at a
time; they do not run here. A run sealed with a blind read or either sampling
rate on refuses at stage open by name; a sealed audit policy is recorded on
every `page-reading` as not run.

The record shapes are in CONTRACT.md, "Page reading".
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable, Final

import annotations
import dossier
import page_feed
import page_overlay
import page_prompt
import truncation
from dissent import dissent_against
from live_reader import EngineSignalRefusal, send_page_request
from throughput import planned_seconds_per_page

import operations.serving.errors as serving_errors
from common import page_accounting, page_answer
from common.alignment import bracket_marker_view
from common.background import validate_measured_ink_map_payload
from common.chairs.models import AbsentChair, ChairIdentity
from common.contracts.canonical import digest_bytes, digest_of, is_plain_int
from common.contracts.envelope import read_verified
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal
from common.contracts.identities import act_id as derive_act_id
from common.contracts.identities import (
    artifact_id,
    attempt_id,
    perlector_attempt_id,
    region_id,
)
from common.contracts.stages import ATTESTATORES, DESIGNATOR, EXEMPLAR, INK_MAP, PERLECTOR
from common.decoding import chair_decoding, engine_effective_sampling, recorded_sampling
from common.exemplar_boundary import cut_exemplar_crop, read_sealed_page
from common.imaging import dimensions
from common.page_testimonia import (
    current_page_testimonia,
    declared_page_witness_chairs,
    require_page_roster,
)
from common.request_capacity import RequestCapacityRefusal, page_request_capacity
from common.residual_ink import (
    INK_NOT_MEASURABLE,
    MINIMUM_CONTRAST_BELOW_BACKGROUND,
    load_coverage_audit_config,
    resolve_coverage_audit_policy,
)
from common.stage import (
    SECONDARY_PROPOSER_CHAIR,
    exemplar_page_ids,
    stage_manifest,
)
from common.witness_regime import witness_label
from operations.serving.assembly import bound_serving_recipes
from operations.serving.errors import ChairResponseRefusal
from operations.serving.http import EndpointUnavailable

READING_UNIT: Final = "page"

PAGE_FEED_KIND: Final = "page-feed"
PAGE_READING_KIND: Final = "page-reading"
PAGE_ACCOUNTING_KIND: Final = "page-accounting"
ACT_REGION_KIND: Final = "act-region"
PERLECTIO_KIND: Final = "perlectio"
PAGE_READING_SCHEMA: Final = "perlector-page-reading.v1"
ACT_REGION_SCHEMA: Final = "perlector-act-region.v1"
PERLECTIO_SCHEMA: Final = "perlectio.v2"

# The `reader-sent` pass a page's call is recorded under, and the attempt a
# page reading is. One reading per page; a second is later work.
PAGE_READING_PASS: Final = "page-reading"
PAGE_READ_OPERATION: Final = "page-read"
PAGE_READ_ORDINAL: Final = 1
ACT_REGION_OPERATION: Final = "reading-region"

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
DUPLICATE_REGION: Final = "duplicate-region"
READING_INCOMPLETE: Final = "reading-incomplete"
DOUBT_MARKS_MALFORMED: Final = "doubt-marks-malformed"
ENTRY_NO_READABLE_TEXT: Final = "entry-no-readable-text"
NO_AUTOPSIA: Final = "no-autopsia"

READING_CLASS: Final = "reading"
UNPLACED_CLASS: Final = "reading-unplaced"

# The Designator's stage-2 Surya records (`surya-page` census per page,
# `surya-line` and `surya-block` per detection).
SURYA_PAGE_KIND: Final = "surya-page"
SURYA_LINE_KIND: Final = "surya-line"
SURYA_BLOCK_KIND: Final = "surya-block"

_PAGE_LOCAL_CALL_FAILURES: Final = (
    EngineSignalRefusal,
    ChairResponseRefusal,
    EndpointUnavailable,
    serving_errors.ChairTransportFailure,
)


@dataclass(frozen=True)
class StageHooks:
    """The helpers of the act path's `run.py` the page path reads its evidence through."""

    provenance_for: Callable[..., dict[str, Any]]
    engine_call_inputs: Callable[..., list[dict[str, str]]]
    start_chair: Callable[..., None]
    publish_sent: Callable[..., None]
    sent_records: Callable[..., list[dict[str, Any]]]
    unrecorded_replies: Callable[..., tuple[list[dict[str, Any]], bool]]
    answers_a_send: Callable[..., bool]
    in_order_window: Callable[..., list[Any]]
    refuse_past_deadline: Callable[..., None]
    failure_record: Callable[..., dict[str, Any] | None]
    real_ingress: Callable[..., bool]
    sent_kind: str


# --- stage open -------------------------------------------------------------------


def refuse_unsupported_settings(context) -> None:
    """Refuse, by name, a sealed setting the page path does not apply."""
    if context.blind_read != "off":
        raise ContractError(
            f"the sealed blind_read is {context.blind_read!r}, but a run sealed with "
            'reading_unit = "page" reads each page once, with no blind first pass; seal '
            "blind_read = off for a page-read run"
        )
    for name, value in (
        ("nuda_per_mille", context.nuda_per_mille),
        ("perlector_instrument_per_mille", context.perlector_instrument_per_mille),
    ):
        if value:
            raise ContractError(
                f"the sealed {name} is {value}, but Lectio nuda and the primed-without-prior "
                'control read acts one at a time and do not run under reading_unit = "page"; '
                f"seal {name} = 0 for a page-read run"
            )


def audit_not_run(audit_policy: dict[str, Any], audit_sha256: str) -> dict[str, Any]:
    """What every `page-reading` says about the sealed Pass-C audit: it did not run."""
    return {
        "state": "not-run",
        "round_cap": audit_policy["round_cap"],
        "policy_sha256": audit_sha256,
        "reason": "Pass C flags and re-proves acts read one at a time; the page path does "
        "not run it",
    }


# --- sealed inputs ----------------------------------------------------------------


def page_key(page_ordinal: int) -> str:
    """The key a page's `reader-sent` records name, as the Attestatores key a page."""
    return f"page-{page_ordinal}"


def _page_witnesses(
    context, page_id: str, current: list[dict[str, Any]], page_chairs: set[str]
) -> list[dict[str, Any]]:
    """Every configured page witness's current Testimonium for one page, as the feed takes it.

    Called only for a page some witness testified to: a page with none is read
    as `no-witness-testimony`, but a roster chair missing beside others that
    testified is a shortened roster and refuses.
    """
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
                ATTESTATORES, "page-testimonium", by_chair[chair]["artifact_id"]
            ),
        }
        for chair in sorted(page_chairs)
    ]


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


def _fields(payload: Any, names: tuple[str, ...], what: str) -> dict[str, Any]:
    """The named fields of a sealed payload, or a refusal naming the first one missing."""
    if not isinstance(payload, dict):
        raise FatalAccounting(f"{what} carries no payload object")
    for name in names:
        if name not in payload:
            raise FatalAccounting(f"{what} carries no {name!r}")
    return {name: payload[name] for name in names}


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


def sealed_surya_census(context) -> dict[str, dict[str, Any]] | None:
    """Every page's Surya census as the page feed takes it, or `None` when the run has none.

    Read from the Designator's stage-2 records: each `surya-page` (subject
    page_id) names its `line_subjects` and `block_subjects` in Surya's order
    with their counts, and how Surya ordered the blocks (`reading_order`, and
    `reading_order_reason` for a raster fallback; the feed calls them
    `block_sequence`); each `surya-line` and `surya-block` carries `n`, its
    `bounds` and `confidence_bp`, and a block its `label`,
    `reading_order_position` and the page's `reading_order`. The census and
    its detections must agree exactly, and every sealed detection must be
    named by its page's census. This is the one place that shape is read.
    """
    records = stage_manifest(context, DESIGNATOR)["artifacts"]
    censuses = [entry for entry in records if entry["kind"] == SURYA_PAGE_KIND]
    if not censuses:
        return None
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in records:
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
        if census["reading_order"] not in (page_feed.ORDER_HEAD, page_feed.RASTER_FALLBACK):
            raise FatalAccounting(
                f"page {page_id}'s Surya census states reading order "
                f"{census['reading_order']!r}, which is not one Surya gives"
            )
        lines = _surya_detections(
            context,
            SURYA_LINE_KIND,
            page_id,
            census["line_subjects"],
            census["line_count"],
            entries,
            census["reading_order"],
        )
        blocks = _surya_detections(
            context,
            SURYA_BLOCK_KIND,
            page_id,
            census["block_subjects"],
            census["block_count"],
            entries,
            census["reading_order"],
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


def _surya_for(census: dict[str, dict[str, Any]] | None, page_id: str) -> Any:
    if census is None:
        return page_feed.SURYA_ABSENT
    if page_id not in census:
        raise FatalAccounting(
            f"this run seals Surya censuses, but none for page {page_id}; the page cannot be "
            "shown its detections"
        )
    return census[page_id]


def _page_render(context, protocol_config: dict[str, Any], page_id: str, ordinal: int):
    """The page image the sealed `page_image` switch shows, or `None` when it is off."""
    setting = protocol_config["feed"]["page_image"]
    if setting == "off":
        return None
    return dossier.build_page_render(
        context,
        source_page_id=page_id,
        source_page_ordinal=ordinal,
        page_context=protocol_config["page_context"],
        crop_bounds=[],
        full_page=setting == "full",
    )


def _distinct(references: list[dict[str, str] | None]) -> list[dict[str, str]]:
    seen: dict[str, dict[str, str]] = {}
    for reference in references:
        if reference is None:
            continue
        known = seen.setdefault(reference["relative_path"], dict(reference))
        if known != reference:
            raise SchemaRefusal(f"two digests are claimed for input {reference['relative_path']!r}")
    return list(seen.values())


def feed_inputs(
    context, feed: dict[str, Any], witnesses: list[dict[str, Any]]
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
    references = _distinct(named)
    for reference in references:
        if context.input_ref(reference["relative_path"]) != reference:
            raise SchemaRefusal(
                f"the page feed names {reference!r}, but the bytes at that path differ"
            )
    return references


# --- the answer's ids ---------------------------------------------------------------
#
# The answer is read by `common/page_accounting.py`'s `validate_answer` against
# `feed_candidates`, the placement map the accounting measures against too.


def page_problems(validated: dict[str, Any]) -> list[dict[str, Any]]:
    """The problems that hold the page reading whole; a shared union box holds its entries."""
    return [
        problem
        for problem in validated["problems"]
        if problem["code"] != page_accounting.DUPLICATE_REGION
    ]


# --- one page ---------------------------------------------------------------------


@dataclass
class _Page:
    """One page's sealed inputs and its request, resolved on the main thread."""

    page_id: str
    ordinal: int
    page_record: dict[str, Any]
    feed: dict[str, Any] | None = None
    feed_ref: dict[str, str] | None = None
    text: str | None = None
    image_sha256s: list[str] | None = None
    capacity: dict[str, Any] | None = None
    refusal: RequestCapacityRefusal | None = None
    adopted: dict[str, Any] | None = None
    fixture_row: dict[str, Any] | None = None
    # Every reason the page is not asked; empty for a page that is.
    not_run: list[dict[str, str]] = field(default_factory=list)
    page_size: tuple[int, int] | None = None
    witnesses: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _PagePass:
    """What every page of one pass reads under."""

    run: Any
    hooks: StageHooks
    audit: dict[str, Any]
    page_chairs: set[str]
    testimonia: dict[str, list[dict[str, Any]]]
    surya: dict[str, dict[str, Any]] | None
    accounting_policy: Any
    row: Any = None

    @property
    def context(self):
        return self.run.context

    @property
    def live(self) -> bool:
        return self.run.serving_mode == "live"

    @property
    def chair_present(self) -> bool:
        return not isinstance(self.run.chair, AbsentChair)


def page_reading_attempt(page_id: str) -> str:
    return attempt_id(page_id, PAGE_READ_OPERATION, PAGE_READ_ORDINAL)


def _artifact(kind: str, subject: str, attempt: str | None) -> str:
    return artifact_id(PERLECTOR, kind, subject, attempt)


def _sealed(context, kind: str, subject: str, attempt: str | None) -> dict[str, Any] | None:
    """The record this stage already published under that identity, or `None`."""
    identifier = _artifact(kind, subject, attempt)
    if not context.tree.has_artifact(PERLECTOR, kind, identifier):
        return None
    return context.tree.read_artifact(PERLECTOR, kind, identifier)


def _existing_reading(context, page_id: str) -> dict[str, Any] | None:
    return _sealed(context, PAGE_READING_KIND, page_id, page_reading_attempt(page_id))


def _fixture_answer(context, ordinal: int) -> dict[str, Any]:
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


def _prepare(state: _PagePass, ordinal: int, page_id: str) -> _Page:
    """Build and publish one page's feed and resolve its request; publish nothing else."""
    run, context = state.run, state.context
    page_record = context.tree.read_artifact(
        EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", page_id)
    )
    page = _Page(page_id=page_id, ordinal=ordinal, page_record=page_record)
    if page_record["outcome"] != "sealed":
        page.not_run = [
            {
                "code": PAGE_NOT_SEALED,
                "detail": "the Exemplar refused this page; there are no sealed pixels to read",
            }
        ]
        return page
    page_record, page_bytes = read_sealed_page(context.tree, page_id, refusal=ContractError)
    width, height = dimensions(page_bytes)
    page.page_size = (width, height)
    current = state.testimonia.get(page_id, [])
    no_testimony = not current
    if not no_testimony:
        page.witnesses = _page_witnesses(context, page_id, current, state.page_chairs)
    feed = page_feed.build_page_feed(
        page_id=page_id,
        page_ordinal=ordinal,
        page_size=(width, height),
        feed_switches=run.protocol_config["feed"],
        witness_regime=context.witness_context,
        roster=sorted(state.page_chairs),
        witnesses=page.witnesses,
        surya=_surya_for(state.surya, page_id),
        page_render=_page_render(context, run.protocol_config, page_id, ordinal),
        serving_recipe=run.chair.serving_recipe if state.chair_present else None,
        read_bytes=context.tree.read_bytes,
        fixture_placeholders=not state.hooks.real_ingress(context),
        no_testimony=no_testimony,
    )
    published = context.publish(
        kind=PAGE_FEED_KIND,
        subject_id=page_id,
        outcome="read",
        inputs=feed_inputs(context, feed, page.witnesses),
        payload=feed,
    )
    page.feed, page.feed_ref = feed, context.input_ref(published.relative_path)
    if not state.chair_present:
        page.not_run.append(
            {
                "code": CHAIR_ABSENT,
                "detail": "the Perlector chair is absent from this run's roster; nothing read "
                "the page",
            }
        )
    if no_testimony:
        page.not_run.append(
            {
                "code": NO_WITNESS_TESTIMONY,
                "detail": "no witness testified to this page (the Attestatores serve only pages "
                "with a proposed Designator act); the page is held for a human, not read "
                "without its witnesses",
            }
        )
    if page_feed.shows_nothing(feed):
        page.not_run.append(
            {
                "code": NOTHING_TO_SHOW,
                "detail": "the sealed feed shows no page image, and this page has no witness "
                "text and no detection to show; a reading would have nothing to be made from",
            }
        )
    page.adopted = _existing_reading(context, page_id)
    if page.not_run or page.adopted is not None:
        return page
    page.text = page_prompt.build_page_prompt(run.chair.serving_recipe, feed)
    page.image_sha256s = [feed["page_render"]["image_sha256"]] if feed["page_render"] else []
    if feed["overlay"] is not None:
        page.image_sha256s.append(feed["overlay"]["image_sha256"])
    if not state.live:
        page.fixture_row = _fixture_answer(context, ordinal)
        return page
    try:
        page.capacity = page_request_capacity(
            _serving_row(state),
            image_sizes=page_feed.request_image_sizes(feed),
            prompt_text=page.text,
            prompt_parts=page_prompt.prompt_parts(run.chair.serving_recipe, feed),
            template_digest=page_prompt.BUILDER_SHA256,
            answer_measure=feed["answer_measure"],
            page_max_tokens=run.page_max_tokens,
        )
    except RequestCapacityRefusal as refusal:
        if refusal.capacity is None:
            raise
        page.refusal = refusal
    return page


def _sends(state: _PagePass, page: _Page) -> bool:
    """Whether this pass sends the page's call: live, asked, not refused, not already read."""
    return state.live and not page.not_run and page.adopted is None and page.refusal is None


def _request_images(context, feed: dict[str, Any]) -> list[bytes]:
    """The images the page's request sends, in order, read digest-checked."""
    images = []
    if feed["page_render"] is not None:
        render = feed["page_render"]
        images.append(
            read_verified(
                context.tree.read_bytes,
                {"relative_path": render["image_path"], "sha256": render["image_sha256"]},
                "the page render",
            )
        )
    if feed["overlay"] is not None:
        images.append(page_overlay.overlay_image(feed, context.tree.read_bytes))
    return images


def _serving_row(state: _PagePass):
    if state.row is None:
        run = state.run
        state.row = bound_serving_recipes(
            run.context, run.args.serving_recipes_config
        ).for_identity(run.chair, run.args.placement_tier)
    return state.row


def _refuse_past_page_deadline(state: _PagePass, seconds_needed: int, what: str) -> None:
    """The deadline check in pages: a page-read pass reads every sealed page, so only
    a later deadline makes room."""
    per_page = planned_seconds_per_page(state.run.page_max_tokens)
    state.hooks.refuse_past_deadline(
        state.run.args.reading_deadline,
        seconds_needed,
        what,
        rate=f"{per_page}s a page (planned_seconds_per_page for the sealed page answer cap)",
        remedy="Give a later --reading-deadline; a page-read pass reads every sealed page",
    )


def _page_job(state: _PagePass, page: _Page):
    """One prepared page's call, if it is sent, and its in-order finish."""
    run, hooks = state.run, state.hooks
    finish = partial(_finish, state, page)
    if not _sends(state, page):
        return None, finish
    _refuse_past_page_deadline(
        state, planned_seconds_per_page(run.page_max_tokens), f"reading page {page.ordinal}"
    )
    images = _request_images(state.context, page.feed)
    if run.service.client is None:
        hooks.start_chair(run)
    hooks.publish_sent(
        run,
        page.page_id,
        page_key(page.ordinal),
        PAGE_READ_ORDINAL,
        PAGE_READING_PASS,
        page.image_sha256s,
    )
    return partial(_call, run, page, images), finish


def _call(run, page: _Page, images: list[bytes]) -> dict[str, Any] | Exception:
    """The page's one call, safe on a worker thread: it publishes nothing."""
    try:
        return send_page_request(
            run.service.client,
            images=images,
            text=page.text,
            capacity=page.capacity["capacity"],
            max_tokens=page.capacity["max_tokens"],
            what=f"page {page.page_id}",
        )
    except _PAGE_LOCAL_CALL_FAILURES as error:
        return error


def _request_digest(page: _Page) -> str | None:
    if page.text is None:
        return None
    return digest_of(
        {
            "image_sha256s": page.image_sha256s,
            "text_sha256": digest_bytes(page.text.encode("utf-8")),
        }
    )


def _read_reply(content: str, stop_reason: str | None, feed: dict[str, Any]):
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
        problems = page_problems(
            page_accounting.validate_answer(answer, page_accounting.feed_candidates(feed))
        )
        if stop_reason is None:
            problems.append(
                {
                    "code": NO_STOP_REASON,
                    "detail": "the engine gave no finish reason, so whether the answer ran to "
                    "its own end is unknown; the answer is kept and held whole",
                }
            )
    return state, answer, problems


def _finish(state: _PagePass, page: _Page, result: dict[str, Any] | Exception | None) -> None:
    """Publish the page's reading, then its accounting, then its act records."""
    if page.adopted is not None:
        reading = page.adopted
        _check_adopted(state, page, reading)
    else:
        reading = _publish_reading(state, page, result)
    if page.feed is None:
        return
    plans = entry_plans(state, page, reading) if reading["payload"]["disposition"] == READ else []
    accounting = publish_page_accounting(state, page, reading, plans)
    publish_act_records(state, page, reading, plans, accounting)


def _publish_reading(state: _PagePass, page: _Page, result) -> dict[str, Any]:
    run, hooks, context = state.run, state.hooks, state.context
    attempted = not page.not_run and page.refusal is None
    engine_call = capacity = failure = answer = None
    finish_reason = stop_reason = None
    inputs: list[dict[str, str] | None] = [
        page.feed_ref,
        context.artifact_ref(EXEMPLAR, "page", page.page_record["artifact_id"]),
    ]
    receipt_ref = None
    if page.not_run:
        parse_state, problems = NOT_RUN, [dict(problem) for problem in page.not_run]
    elif page.refusal is not None:
        parse_state = REFUSED_CAPACITY
        capacity = {"capacity": page.refusal.capacity, "answer_reserve": None, "max_tokens": None}
        problems = [{"code": REFUSED_CAPACITY, "detail": str(page.refusal)}]
    elif not state.live:
        row = page.fixture_row
        finish_reason = stop_reason = row.get("stop_reason", "stop")
        parse_state, answer, problems = _read_reply(row["answer"], stop_reason, page.feed)
    else:
        receipt_ref = run.receipt_ref
        capacity = page.capacity
        inputs += [
            context.artifact_ref(PERLECTOR, hooks.sent_kind, marker["artifact_id"])
            for marker in hooks.sent_records(
                context, page.page_id, page_key(page.ordinal), PAGE_READ_ORDINAL, PAGE_READING_PASS
            )
        ]
        if isinstance(result, Exception):
            failure = hooks.failure_record(result, phase=PAGE_READING_PASS)
            if failure is None:
                raise result
            parse_state = CALL_FAILED
            problems = [{"code": failure["code"], "detail": failure["detail"]}]
            inputs += [
                dict(failure[name])
                for name in ("raw_response_ref", "call_record_ref")
                if failure[name] is not None
            ]
        else:
            engine_call = result["engine_call"]
            finish_reason, stop_reason = result["finish_reason"], result["stop_reason"]
            inputs += hooks.engine_call_inputs(context, engine_call, variance_arm=None)
            parse_state, answer, problems = _read_reply(result["content"], stop_reason, page.feed)
    disposition = READ if parse_state == PARSED and not problems else HELD
    payload = {
        "schema": PAGE_READING_SCHEMA,
        "page_id": page.page_id,
        "page_ordinal": page.ordinal,
        "reading_unit": READING_UNIT,
        "feed_ref": page.feed_ref,
        "request_digest": _request_digest(page) if attempted else None,
        "engine_call": engine_call,
        "sampling": _page_sampling(run) if receipt_ref is not None else None,
        "capacity": capacity,
        "finish_reason": finish_reason,
        "stop_reason": stop_reason,
        "parse_state": parse_state,
        "answer": answer,
        "problems": problems,
        "failure": failure,
        "disposition": disposition,
        "audit": state.audit,
        "provenance": hooks.provenance_for(
            context, run.chair, attempted=attempted, receipt_ref=receipt_ref
        ),
    }
    attempt = page_reading_attempt(page.page_id)
    context.publish(
        kind=PAGE_READING_KIND,
        subject_id=page.page_id,
        outcome=disposition,
        attempt=attempt,
        inputs=_distinct(inputs),
        payload=payload,
    )
    return context.tree.read_artifact(
        PERLECTOR, PAGE_READING_KIND, _artifact(PAGE_READING_KIND, page.page_id, attempt)
    )


def _page_sampling(run) -> dict[str, Any]:
    """The sealed Perlector row a live page call sends, and what the engine samples under.

    `ChairClient` puts exactly this row on the wire, with the serving receipt's
    seed; the call record the reading names is held to it
    (`engine_call_inputs`). A page reading is sampled, so a second call would be
    a second draw: a resumed pass adopts the sealed reading and never asks again.
    """
    values = chair_decoding(run.decoding_policy, run.chair.role)
    return {
        "chair": run.chair.role,
        "sent": recorded_sampling(values),
        "effective": recorded_sampling(engine_effective_sampling(values)),
    }


def _check_adopted(state: _PagePass, page: _Page, record: dict[str, Any]) -> None:
    """Refuse a retained page reading this run could not have made from this page's feed."""
    payload = record["payload"]
    if (
        record["config_digest"] != state.context.config_digest
        or not isinstance(payload, dict)
        or payload.get("schema") != PAGE_READING_SCHEMA
        or payload.get("feed_ref") != page.feed_ref
        or payload.get("disposition") not in (READ, HELD)
    ):
        raise ContractError(
            f"page {page.page_id}'s retained page reading was made from another feed or "
            "configuration than this page has now; it is not adopted and the page is not "
            "asked again. Read this page in a new run"
        )
    if payload.get("engine_call") is not None:
        # The retained call record is held to the sealed row it was sent under.
        state.hooks.engine_call_inputs(state.context, payload["engine_call"], variance_arm=None)
        if payload.get("sampling") != _page_sampling(state.run):
            raise ContractError(
                f"page {page.page_id}'s retained page reading names sampling other than the "
                "sealed Perlector row; it is not adopted and the page is not asked again. "
                "Read this page in a new run"
            )


# --- the answer's entries -----------------------------------------------------------


def answer_entries(answer: dict[str, Any], feed: dict[str, Any]) -> list[dict[str, Any]]:
    """Each entry of a valid answer: the entry, its expanded ids, union box and entry holds."""
    validated = page_accounting.validate_answer(answer, page_accounting.feed_candidates(feed))
    shared = {
        n
        for problem in validated["problems"]
        if problem["code"] == page_accounting.DUPLICATE_REGION
        for n in problem["ns"]
    }
    entries = []
    for act, entry in zip(answer["acts"], validated["entries"], strict=True):
        union = entry["union_box_px"]
        holds = [] if union is not None else [UNPLACED]
        if entry["n"] in shared:
            holds.append(DUPLICATE_REGION)
        entries.append(
            {"act": act, "cited_ids": entry["cited_ids"], "union_box_px": union, "holds": holds}
        )
    return entries


def entry_plans(state: _PagePass, page: _Page, reading: dict[str, Any]) -> list[dict[str, Any]]:
    """Each entry of a read page as it will be published, computed without publishing.

    Per entry: its act id and class, its union box, its text and doubt layers
    (`annotations.read_doubt_marks`), its truncation classification over its
    region, and its own holds: the region's (`reading-unplaced`,
    `duplicate-region`, and `no-autopsia` when no page image was shown) and the
    reading's (`doubt-marks-malformed`, `reading-incomplete`,
    `entry-no-readable-text`). The page accounting reads the truncations from
    here, before any act record exists.
    """
    run, feed = state.run, page.feed
    payload = reading["payload"]
    page_pixels = feed["page_size"]["w"] * feed["page_size"]["h"]
    attempt = page_reading_attempt(page.page_id)
    autopsia = feed["page_render"] is not None
    plans = []
    for entry in answer_entries(payload["answer"], feed):
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
                region_pixels=union["w"] * union["h"],
                page_pixels=page_pixels,
                truncation_policy=run.protocol_config["truncation"],
                stop_reason=payload["stop_reason"],
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
                    page.page_id,
                    act_class,
                    {"page_reading": attempt, "n": act["n"], "union_box_px": union},
                ),
                "act_class": act_class,
                "cited_ids": list(entry["cited_ids"]),
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


def _comparison_text(text: str, capabilities: Any) -> str:
    """A witness's text as dissent compares it: its own doubt markers removed when it has any.

    A witness whose declared format can express uncertainty writes its doubt
    inline (DAI's `[UNCERTAIN]`, `[CROSSED_OUT]`); those characters are the
    witness's doubt, not a reading the Perlector departed from.
    """
    if isinstance(capabilities, Mapping) and capabilities.get("can_express_uncertainty") is True:
        return bracket_marker_view(text)["text"]
    return text


def _dissent(
    text: str, feed: dict[str, Any], cited_ids: list[str], witnesses: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Where the entry's reading departed from each shown witness's cited units."""
    capabilities = {
        witness["witness_label"]: witness["testimonium"]["payload"].get("format_capabilities")
        for witness in witnesses
    }
    cited = set(cited_ids)
    rows = []
    for witness in feed["witnesses"]:
        head = {"letter": witness["letter"], "witness_label": witness["witness_label"]}
        if witness["outcome"] != page_feed.READ_OUTCOME:
            rows.append(
                {
                    **head,
                    "cited_units": [],
                    "compared": False,
                    "reason": f"this witness's page outcome is {witness['outcome']}; it has no "
                    "units",
                }
            )
            continue
        units = [unit for unit in witness["units"] if unit["id"] in cited]
        if not units:
            rows.append(
                {
                    **head,
                    "cited_units": [],
                    "compared": False,
                    "reason": "no unit of this witness is cited by this entry",
                }
            )
            continue
        reported = _comparison_text(
            "\n".join(unit["text"] for unit in units), capabilities[witness["witness_label"]]
        )
        compared = dissent_against(
            text,
            [
                {
                    "outcome": page_feed.READ_OUTCOME,
                    "payload": {"chair": witness["letter"], "comparison_reported": reported},
                }
            ],
        )
        if len(compared) != 1:
            raise ContractError(f"dissent gave {len(compared)} rows for one witness, not one")
        row = dict(compared[0])
        row.pop("chair")
        rows.append({**head, "cited_units": [unit["id"] for unit in units], **row})
    return rows


def _check_adopted_perlectio(record: dict[str, Any], expected: dict[str, Any], act_id: str) -> None:
    payload = record["payload"]
    if not isinstance(payload, dict) or any(
        payload.get(name) != value for name, value in expected.items()
    ):
        raise ContractError(
            f"act {act_id}'s retained perlectio names another region, reading, feed or "
            "accounting than this page has now; it is not adopted. Read this page in a new run"
        )


def publish_act_records(
    state: _PagePass,
    page: _Page,
    reading: dict[str, Any],
    plans: list[dict[str, Any]],
    accounting: dict[str, Any],
) -> None:
    """One `act-region` and one `perlectio` per planned entry, each naming the accounting.

    Every record carries `page_accounting_ref` and the page's hold codes as
    `page_holds`, and is held when those or its own holds are non-empty. A
    `perlectio` already sealed for the entry is adopted, not recomputed: its
    dissent is bounded by time, so a second computation could differ.
    """
    if not plans:
        return
    context = state.context
    payload = reading["payload"]
    reading_ref = context.artifact_ref(PERLECTOR, PAGE_READING_KIND, reading["artifact_id"])
    accounting_ref = context.artifact_ref(
        PERLECTOR, PAGE_ACCOUNTING_KIND, accounting["artifact_id"]
    )
    page_holds = list(accounting["payload"]["holds"])
    _page, page_bytes = read_sealed_page(context.tree, page.page_id, refusal=ContractError)
    page_ref = {
        "relative_path": page.page_record["payload"]["image_path"],
        "sha256": page.page_record["payload"]["source_sha256"],
    }
    engine_inputs = state.hooks.engine_call_inputs(
        context, payload["engine_call"], variance_arm=None
    )
    attempt = page_reading_attempt(page.page_id)
    for plan in plans:
        act, union, act_id = plan["act"], plan["union_box_px"], plan["act_id"]
        crop = (
            cut_exemplar_crop(context.retain, page_bytes, page.ordinal, page.page_id, union)
            if union is not None
            else None
        )
        region_holds = list(plan["region_holds"])
        region_payload = {
            "schema": ACT_REGION_SCHEMA,
            "page_id": page.page_id,
            "page_ordinal": page.ordinal,
            "reading_unit": READING_UNIT,
            "n": act["n"],
            "kind": act["kind"],
            "label": act.get("label"),
            "cites": list(act["cites"]),
            "cited_ids": list(plan["cited_ids"]),
            "act_class": plan["act_class"],
            "page_reading_attempt": attempt,
            "union_box_px": union,
            "region_id": region_id(act_id, crop["transform"]) if crop else None,
            "image_path": crop["image_path"] if crop else None,
            "image_sha256": crop["image_sha256"] if crop else None,
            "transform": crop["transform"] if crop else None,
            "transform_digest": crop["transform_digest"] if crop else None,
            "page_reading_ref": reading_ref,
            "page_accounting_ref": accounting_ref,
            "feed_ref": page.feed_ref,
            "holds": region_holds,
            "page_holds": list(page_holds),
        }
        region_inputs = [reading_ref, accounting_ref, page.feed_ref, page_ref]
        if crop:
            region_inputs.append(
                {"relative_path": crop["image_path"], "sha256": crop["image_sha256"]}
            )
        published = context.publish(
            kind=ACT_REGION_KIND,
            subject_id=act_id,
            outcome=HELD if region_holds or page_holds else READ,
            attempt=attempt_id(act_id, ACT_REGION_OPERATION, 1),
            inputs=_distinct(region_inputs),
            payload=region_payload,
        )
        region_ref = context.input_ref(published.relative_path)
        refs = {
            "act_region_ref": region_ref,
            "page_reading_ref": reading_ref,
            "page_accounting_ref": accounting_ref,
            "feed_ref": page.feed_ref,
        }
        perlectio_attempt = perlector_attempt_id(act_id, "perlegere", 1)
        adopted = _sealed(context, PERLECTIO_KIND, act_id, perlectio_attempt)
        if adopted is not None:
            _check_adopted_perlectio(adopted, refs, act_id)
            continue
        reading_holds = list(plan["reading_holds"])
        outcome = HELD if reading_holds or page_holds else READ
        assessment = plan["assessment"]
        context.publish(
            kind=PERLECTIO_KIND,
            subject_id=act_id,
            outcome=outcome,
            attempt=perlectio_attempt,
            inputs=_distinct(
                [region_ref, reading_ref, accounting_ref, page.feed_ref, *engine_inputs]
            ),
            payload={
                "schema": PERLECTIO_SCHEMA,
                "page_id": page.page_id,
                "page_ordinal": page.ordinal,
                "reading_unit": READING_UNIT,
                **refs,
                "n": act["n"],
                "kind": act["kind"],
                "label": act.get("label"),
                "text": plan["text"],
                "uncertain_spans": assessment["uncertain_spans"],
                "gaps": assessment["gaps"],
                "uncertainty_assessment": assessment,
                "dissent": _dissent(plan["text"], page.feed, plan["cited_ids"], page.witnesses),
                "truncation": plan["truncation"],
                "autopsia": plan["autopsia"],
                "continues_from_previous_page": act["continues_from_previous_page"],
                "continues_to_next_page": act["continues_to_next_page"],
                "holds": reading_holds,
                "page_holds": list(page_holds),
                "engine_call": payload["engine_call"],
                "provenance": payload["provenance"],
            },
        )


# --- the page accounting -----------------------------------------------------------


def _accounting_witnesses(state: _PagePass, page: _Page) -> list[dict[str, Any]]:
    """Every sealed page witness as the accounting measures it, shown or hidden.

    A shown witness is its feed row. A hidden one is read by the feed's own
    extraction and takes the next letter the feed did not use, in sorted
    `witness_label` order, so its ids never collide with a shown one's.
    """
    shown = {row["witness_label"]: row for row in page.feed["witnesses"]}
    used = {row["letter"] for row in shown.values()}
    free = iter(letter for letter in page_feed.WITNESS_LETTERS if letter not in used)
    rows = []
    for witness in sorted(page.witnesses, key=lambda item: item["witness_label"]):
        testimonium = witness["testimonium"]
        outcome = testimonium["outcome"]
        health = testimonium["payload"].get("content_health")
        blank = health.get("blank") if outcome == "read" and isinstance(health, dict) else None
        row = shown.get(witness["witness_label"])
        if row is not None:
            letter = row["letter"]
            units = [
                {"id": unit["id"], "box_px": unit["box_px"], "text": unit["text"]}
                for unit in row["units"]
            ]
        else:
            letter = next(free)
            reading = page_feed.witness_reading(
                testimonium,
                adapter=witness["adapter"],
                page_size=page.page_size,
                read_bytes=state.context.tree.read_bytes,
                fixture_placeholders=not state.hooks.real_ingress(state.context),
            )
            units = [
                {"id": f"{letter}{number}", "box_px": unit["box_px"], "text": unit["text"]}
                for number, unit in enumerate(reading["units"], start=1)
            ]
        rows.append({"letter": letter, "outcome": outcome, "blank": blank, "units": units})
    return rows


def _one_record(entries: list[dict[str, Any]], kind: str, subject: str) -> dict[str, Any]:
    found = [entry for entry in entries if entry["kind"] == kind and entry["subject_id"] == subject]
    if len(found) != 1:
        raise FatalAccounting(
            f"the Designator sealed {len(found)} {kind} records for {subject!r}, not one"
        )
    return found[0]


def _dai_unit_ids(page: _Page) -> dict[tuple[int, int, int, int], list[str]]:
    """The feed ids of DAI's units by box, in the feed's order.

    DAI reads one unit per detector record it was shown, boxed by that record's
    bounds, so a detector record and the DAI unit with its box are one record.
    A DAI the feed hides has no row and names none.
    """
    labels = {w["witness_label"] for w in page.witnesses if w["adapter"] == page_feed.DAI}
    ids: dict[tuple[int, int, int, int], list[str]] = {}
    for row in page.feed["witnesses"]:
        if row["witness_label"] not in labels:
            continue
        for unit in row["units"]:
            box = unit["box_px"]
            if box is not None:
                ids.setdefault((box["x"], box["y"], box["w"], box["h"]), []).append(unit["id"])
    return ids


def _record_detections(context, page: _Page) -> tuple[Any, Any, list[dict[str, str]]]:
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
    page_id = page.page_id
    entries = stage_manifest(context, DESIGNATOR)["artifacts"]
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
    if census["page_ordinal"] != page.ordinal:
        raise FatalAccounting(f"{what} states page ordinal {census['page_ordinal']!r}")
    unnamed = sorted(sealed - set(subjects))
    if unnamed:
        raise FatalAccounting(
            f"the Designator sealed detector records {unnamed} that {what} does not name"
        )
    output = json.loads(
        read_verified(context.tree.read_bytes, census["raw_output_ref"], "a detector output")
    )
    if not isinstance(output, dict) or not isinstance(output.get("run"), dict):
        raise FatalAccounting(f"page {page_id}'s detector output carries no run facts")
    if "max_det" not in output["run"]:
        return None, None, [page_ref]
    max_det = output["run"]["max_det"]
    if not is_plain_int(max_det) or max_det < 1:
        raise FatalAccounting(f"page {page_id}'s detector run facts state a max_det of {max_det!r}")
    unit_ids = _dai_unit_ids(page)
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
        if (fields["page_ordinal"], fields["detector_ordinal"]) != (page.ordinal, position):
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


def _accounting_detections(state: _PagePass, page: _Page) -> tuple[dict[str, Any], list]:
    """The page's sealed detections as the accounting takes them, and their references."""
    context, feed = state.context, page.feed
    surya, references = None, []
    if state.surya is not None:
        census = state.surya[page.page_id]
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
    configured = isinstance(context.registry.resolve(SECONDARY_PROPOSER_CHAIR), ChairIdentity)
    records, record_census, record_refs = (
        _record_detections(context, page) if configured else (None, None, [])
    )
    return {
        "surya": surya,
        "records": records,
        "record_detector": "configured" if configured else "absent",
        "record_census": record_census,
    }, references + record_refs


def _accounting_ink(context, page: _Page) -> tuple[dict[str, Any] | None, list]:
    """The page's retained ink runs and resolved coverage policy, or `None` when unmeasured."""
    found = []
    for entry in stage_manifest(context, INK_MAP)["artifacts"]:
        if entry["kind"] != "ink-map":
            continue
        record = context.tree.read_artifact(INK_MAP, "ink-map", entry["artifact_id"])
        if record["payload"].get("page_ordinal") == page.ordinal:
            found.append((entry, record))
    if len(found) > 1:
        raise FatalAccounting(
            f"the Ink Map sealed {len(found)} ink maps for page {page.page_id}, not one; the "
            "page's residual ink cannot be measured against one of them by choice"
        )
    if not found:
        return None, []
    [(entry, record)] = found
    reference = context.artifact_ref(INK_MAP, "ink-map", entry["artifact_id"])
    if record["outcome"] == INK_NOT_MEASURABLE:
        return None, [reference]
    measured = validate_measured_ink_map_payload(
        record["payload"], audit_contrast=MINIMUM_CONTRAST_BELOW_BACKGROUND
    )
    coverage = load_coverage_audit_config(context.args.ink_map_config)
    context.require_sealed_config("ink-map", coverage["config_sha256"])
    if coverage["config_sha256"] != measured["background_config_sha256"]:
        raise ContractError(
            f"page {page.page_id}'s ink map and the coverage policy read different sealed bytes"
        )
    runs = record["payload"]["edge_findings"]
    return {
        "runs": runs,
        "coverage_policy": resolve_coverage_audit_policy(coverage, runs["width"], runs["height"]),
    }, [reference]


def _by_path(references: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(references, key=lambda reference: reference["relative_path"])


def publish_page_accounting(
    state: _PagePass, page: _Page, reading: dict[str, Any], plans: list[dict[str, Any]]
) -> dict[str, Any]:
    """The page's `page-accounting`: rules a-i over its reading, witnesses and detections.

    Published before any act record, which names it. A resumed pass adopts the
    one already sealed for the page when it was measured from exactly these
    inputs under this policy -- rule (e) is bounded by a clock, so a second
    measurement could differ -- and refuses by name when any input differs.
    """
    context = state.context
    reading_ref = context.artifact_ref(PERLECTOR, PAGE_READING_KIND, reading["artifact_id"])
    detections, detection_refs = _accounting_detections(state, page)
    ink, ink_refs = _accounting_ink(context, page)
    inputs = _distinct(
        [
            page.feed_ref,
            reading_ref,
            *(witness["testimonium_ref"] for witness in page.witnesses),
            *detection_refs,
            *ink_refs,
        ]
    )
    # Bound to the reading's attempt: a later reading of the page is accounted apart.
    attempt = page_reading_attempt(page.page_id)
    sealed = _sealed(context, PAGE_ACCOUNTING_KIND, page.page_id, attempt)
    if sealed is not None:
        payload = sealed["payload"]
        if (
            sealed["config_digest"] != context.config_digest
            or _by_path(sealed["inputs"]) != _by_path(inputs)
            or not isinstance(payload, dict)
            or payload.get("feed_ref") != page.feed_ref
            or payload.get("page_reading_ref") != reading_ref
            or payload.get("policy_sha256") != state.accounting_policy.sha256
        ):
            raise ContractError(
                f"page {page.page_id}'s retained page accounting was measured from other inputs "
                "or under another policy than this page has now; it is not adopted. Read this "
                "page in a new run"
            )
        return sealed
    payload = reading["payload"]
    accounting = page_accounting.page_accounting(
        feed=page.feed,
        witnesses=_accounting_witnesses(state, page),
        detections=detections,
        reading={
            "parse_state": payload["parse_state"],
            "finish_reason": payload["finish_reason"],
            "answer": payload["answer"],
        },
        entry_truncation={
            plan["act"]["n"]: plan["truncation"]["classification"]
            for plan in plans
            if plan["truncation"] is not None
        },
        ink=ink,
        policy=state.accounting_policy,
        feed_ref=page.feed_ref,
        page_reading_ref=reading_ref,
    )
    context.publish(
        kind=PAGE_ACCOUNTING_KIND,
        subject_id=page.page_id,
        outcome=HELD if accounting["holds"] else READ,
        attempt=attempt,
        inputs=inputs,
        payload=accounting,
    )
    return context.tree.read_artifact(
        PERLECTOR, PAGE_ACCOUNTING_KIND, _artifact(PAGE_ACCOUNTING_KIND, page.page_id, attempt)
    )


# --- the pass ---------------------------------------------------------------------


def _pages_left(state: _PagePass, prepared: list[_Page]) -> int:
    """Count the pages this live pass will send, refusing any it cannot resume.

    Only a page it sends is counted: not one already read, not one it does not
    ask, and not one over the row's capacity. A page with sends and no reading
    is sent again only when no retained reply could be its answer.
    """
    context, hooks = state.context, state.hooks
    left, unrecorded, replies = 0, [], None
    for page in prepared:
        if not _sends(state, page):
            continue
        markers = hooks.sent_records(
            context, page.page_id, page_key(page.ordinal), PAGE_READ_ORDINAL, PAGE_READING_PASS
        )
        if markers:
            replies = hooks.unrecorded_replies(context) if replies is None else replies
            calls, unattributed = replies
            if unattributed or hooks.answers_a_send(calls, markers):
                unrecorded.append(page.page_id)
                continue
        left += 1
    if unrecorded:
        raise ContractError(
            f"pages {unrecorded} were sent in an interrupted live pass, and a reply is retained "
            "that no record names and that may be theirs; asking again would read them twice. "
            "Read these pages in a new run; the retained replies remain that pass's evidence"
        )
    return left


def read_the_pages(run, hooks: StageHooks) -> None:
    """Read every sealed Exemplar page once and publish its records, in page order."""
    context = run.context
    refuse_unsupported_settings(context)
    pages = exemplar_page_ids(context)
    if not pages:
        raise ContractError("the Exemplar sealed no page, so the page path has nothing to read")
    state = _PagePass(
        run=run,
        hooks=hooks,
        audit=audit_not_run(run.audit_policy, run.audit_sha256),
        page_chairs=declared_page_witness_chairs(context),
        testimonia=current_page_testimonia(context, run.all_proposal_regions),
        surya=sealed_surya_census(context),
        accounting_policy=page_accounting.require_page_accounting_policy(
            context, context.page_accounting_config_path
        ),
    )
    prepared = [_prepare(state, ordinal, page_id) for ordinal, page_id in pages.items()]
    if state.live and state.chair_present:
        left = _pages_left(state, prepared)
        if left:
            startup = _serving_row(state).startup_timeout_seconds
            _refuse_past_page_deadline(
                state,
                startup + left * planned_seconds_per_page(run.page_max_tokens),
                f"starting the Perlector ({startup}s) and reading {left} pages",
            )
    print(f"perlector: reading {len(pages)} pages whole", file=sys.stderr)
    hooks.in_order_window(run.concurrency, (_page_job(state, page) for page in prepared))
