"""The Perlector's page path: each sealed page read whole, its answer made into act records.

Under the sealed `reading_unit = "page"` (`protocol.py`) stage 4 does not read
the Designator's acts. It reads every sealed Exemplar page in one call, shown
the page image and every witness's page broken into that witness's own units,
and the Perlector establishes the acts itself (`page_feed.py`,
`page_prompt.py`, `page_answer.py`). Per page, in the order published:

    page-feed     (subject page_id)  what the reading is shown, every id it may cite
    reader-sent   (subject page_id)  live only, before the call leaves
    page-reading  (subject page_id)  the answer as given: parsed, or held whole
    act-region    (subject act_id)   one per entry of a parsed, valid answer
    perlectio     (subject act_id)   `perlectio.v2`, one per entry

An answer that is not the grammar, is cut off at the output cap, cites an id
the feed does not define, or could not be asked is held whole on its
`page-reading` and makes no act record: nothing is repaired, trimmed or split.
An entry of a valid answer that cites no boxed id is still published, as a
held `reading-unplaced` act with no crop. Every page has a `page-reading`,
a page the Exemplar refused included, so no page is silently absent.

Pass C, Lectio nuda and the primed-without-prior control read acts one at a
time; they do not run here. A run sealed with a blind read or either sampling
rate on refuses at stage open by name; a sealed audit policy is recorded on
every `page-reading` as not run.

The record shapes are in CONTRACT.md, "Page reading".
"""

from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable, Final

import annotations
import dossier
import page_answer
import page_feed
import page_overlay
import page_prompt
import truncation
from dissent import dissent_against
from live_reader import EngineSignalRefusal, send_page_request

import operations.serving.errors as serving_errors
from common.chairs.models import AbsentChair
from common.contracts.canonical import digest_bytes, digest_of
from common.contracts.envelope import read_verified
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal
from common.contracts.identities import act_id as derive_act_id
from common.contracts.identities import (
    artifact_id,
    attempt_id,
    perlector_attempt_id,
    region_id,
)
from common.contracts.stages import ATTESTATORES, DESIGNATOR, EXEMPLAR, PERLECTOR
from common.exemplar_boundary import cut_exemplar_crop, read_sealed_page
from common.imaging import dimensions
from common.request_capacity import RequestCapacityRefusal, page_request_capacity
from common.stage import exemplar_page_ids, latest_per_chair, stage_manifest
from common.witness_regime import witness_label
from operations.serving.assembly import bound_serving_recipes
from operations.serving.errors import ChairResponseRefusal
from operations.serving.http import EndpointUnavailable

READING_UNIT: Final = "page"

PAGE_FEED_KIND: Final = "page-feed"
PAGE_READING_KIND: Final = "page-reading"
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

# Why one entry of a parsed, valid answer is held.
UNPLACED: Final = "reading-unplaced"
DUPLICATE_REGION: Final = "duplicate-region"
READING_INCOMPLETE: Final = "reading-incomplete"
DOUBT_MARKS_MALFORMED: Final = "doubt-marks-malformed"

READING_CLASS: Final = "reading"
UNPLACED_CLASS: Final = "reading-unplaced"

# The page reading's planned time, for a live pass's deadline: the slowest
# measured act call decoded 441 answer tokens in 33 s (`run.PLANNED_SECONDS_PER_CALL`),
# and a page answer may run to the sealed page cap at that rate.
_SLOWEST_MEASURED_CALL_SECONDS: Final = 33
_SLOWEST_MEASURED_CALL_ANSWER_TOKENS: Final = 441

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

    declared_page_witness_chairs: Callable[..., set[str]]
    validate_page_testimonium_record: Callable[..., None]
    verify_page_native_capture: Callable[..., None]
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


def planned_seconds_per_page(page_max_tokens: int) -> int:
    return math.ceil(
        page_max_tokens * _SLOWEST_MEASURED_CALL_SECONDS / _SLOWEST_MEASURED_CALL_ANSWER_TOKENS
    )


# --- sealed inputs ----------------------------------------------------------------


def page_key(page_ordinal: int) -> str:
    """The key a page's `reader-sent` records name, as the Attestatores key a page."""
    return f"page-{page_ordinal}"


def current_page_testimonia(context, hooks: StageHooks, proposal_regions) -> dict[str, list]:
    """Every page's current page Testimonium per chair, each validated as the act path does."""
    by_page: dict[str, list[dict[str, Any]]] = {}
    for entry in stage_manifest(context, ATTESTATORES)["artifacts"]:
        if entry["kind"] != "page-testimonium":
            continue
        record = context.tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        hooks.validate_page_testimonium_record(context, record, proposal_regions)
        capture = record["payload"].get("native_capture")
        if capture is not None:
            hooks.verify_page_native_capture(
                context, record["subject_id"], record["payload"]["chair"], record, capture
            )
        by_page.setdefault(record["subject_id"], []).append(record)
    return {
        page_id: latest_per_chair(records, f"page Testimonium for page {page_id}")
        for page_id, records in by_page.items()
    }


def _page_witnesses(
    context, page_id: str, current: list[dict[str, Any]], page_chairs: set[str]
) -> list[dict[str, Any]]:
    """Every configured page witness's current Testimonium for one page, as the feed takes it."""
    by_chair = {record["payload"]["chair"]: record for record in current}
    missing = page_chairs - set(by_chair)
    if missing:
        raise FatalAccounting(
            f"page {page_id} has no current page Testimonium for configured page witness(es) "
            f"{sorted(missing)}; the page cannot be read over a shortened witness roster"
        )
    unsealed = set(by_chair) - page_chairs
    if unsealed:
        raise FatalAccounting(
            f"page {page_id} carries page Testimonia from chair(s) {sorted(unsealed)}, which "
            "this run did not seal as page witnesses"
        )
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


def sealed_surya_census(context) -> dict[str, dict[str, Any]] | None:
    """Every page's Surya census as the page feed takes it, or `None` when the run has none.

    Read from the Designator's `surya-page` (subject page_id), `surya-line` and
    `surya-block` records (subjects `<page_id>-surya-line-<n>` and
    `<page_id>-surya-block-<n>`, `n` from 1 in Surya's order), each payload
    carrying `box_px`, and a block also its `label` and reading-order
    `position`. This is the one place that shape is read.
    """
    entries = stage_manifest(context, DESIGNATOR)["artifacts"]
    censuses = [entry for entry in entries if entry["kind"] == SURYA_PAGE_KIND]
    if not censuses:
        return None
    by_page: dict[str, dict[str, Any]] = {
        entry["subject_id"]: {
            "census_ref": context.artifact_ref(DESIGNATOR, SURYA_PAGE_KIND, entry["artifact_id"]),
            "lines": [],
            "blocks": [],
        }
        for entry in censuses
    }
    detections: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
    for entry in entries:
        if entry["kind"] not in (SURYA_LINE_KIND, SURYA_BLOCK_KIND):
            continue
        match = _SURYA_SUBJECT.match(entry["subject_id"])
        if match is None or match["page"] not in by_page:
            raise FatalAccounting(
                f"a Surya {entry['kind']} names subject {entry['subject_id']!r}, which is no "
                "detection of a page with a Surya census"
            )
        record = context.tree.read_artifact(DESIGNATOR, entry["kind"], entry["artifact_id"])
        payload = record["payload"]
        reference = context.artifact_ref(DESIGNATOR, entry["kind"], entry["artifact_id"])
        row = (
            {"box_px": payload.get("box_px"), "ref": reference}
            if entry["kind"] == SURYA_LINE_KIND
            else {
                "box_px": payload.get("box_px"),
                "label": payload.get("label"),
                "position": payload.get("position"),
                "ref": reference,
            }
        )
        detections.setdefault((match["page"], match["kind"]), []).append((int(match["n"]), row))
    for (page, kind), rows in detections.items():
        numbers = sorted(number for number, _row in rows)
        if numbers != list(range(1, len(rows) + 1)):
            raise FatalAccounting(
                f"page {page}'s Surya {kind}s are numbered {numbers}, not 1..{len(rows)}; a "
                "detection is missing"
            )
        by_page[page][f"{kind}s"] = [row for _number, row in sorted(rows, key=lambda r: r[0])]
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


def feed_inputs(context, feed: dict[str, Any]) -> list[dict[str, str]]:
    """Every record and image the feed names, digest-checked against the bytes on disk."""
    named: list[dict[str, str] | None] = [
        witness["testimonium_ref"] for witness in feed["witnesses"]
    ]
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


# --- the answer's checks against its feed -----------------------------------------
#
# `common/page_accounting.py` owns the shared form of these two checks; the page
# path calls them through `expand_cites` and `validate_answer` alone.

_ID: Final = re.compile(r"^[A-Z][1-9][0-9]*$")
_RANGE: Final = re.compile(r"^(?P<a>[A-Z])(?P<i>[1-9][0-9]*)-(?P<b>[A-Z])(?P<j>[1-9][0-9]*)$")


def feed_boxes(feed: dict[str, Any]) -> dict[str, dict[str, int] | None]:
    """Every id the feed defines, with its sealed-page box or `None` where it has none."""
    boxes: dict[str, dict[str, int] | None] = {}
    for witness in feed["witnesses"]:
        for unit in witness["units"]:
            boxes[unit["id"]] = unit["box_px"]
    surya = feed["surya"]
    if surya is not None:
        for row in surya["lines"] + surya["blocks"]:
            boxes[row["id"]] = row["box_px"]
    return boxes


def expand_cites(cites: list[str], known: dict[str, Any]) -> tuple[list[str], list[dict]]:
    """The ids `cites` names, a range expanded inclusive, in order; and every problem."""
    ids: list[str] = []
    problems: list[dict[str, str]] = []
    for cite in cites:
        match = _RANGE.match(cite)
        if match is not None:
            start, end = int(match["i"]), int(match["j"])
            if match["a"] != match["b"] or start >= end:
                problems.append(
                    {"code": "malformed-range", "detail": f"{cite!r} is not one letter ascending"}
                )
                continue
            named = [f"{match['a']}{number}" for number in range(start, end + 1)]
        elif _ID.match(cite):
            named = [cite]
        else:
            problems.append({"code": "unknown-id", "detail": f"{cite!r} is not an id or a range"})
            continue
        for identifier in named:
            if identifier not in known:
                problems.append(
                    {"code": "unknown-id", "detail": f"{identifier!r} is not in the page feed"}
                )
            elif identifier not in ids:
                ids.append(identifier)
    return ids, problems


def validate_answer(answer: dict[str, Any], feed: dict[str, Any]) -> list[dict[str, str]]:
    """Every way a parsed answer's ids disagree with its feed; empty when it is valid."""
    known = feed_boxes(feed)
    problems: list[dict[str, str]] = []
    cited: set[str] = set()
    for act in answer["acts"]:
        ids, found = expand_cites(act["cites"], known)
        problems += found
        cited.update(ids)
    set_aside: set[str] = set()
    for entry in answer["set_aside"]:
        ids, found = expand_cites([entry["id"]], known)
        problems += found
        if not entry["reason"].strip():
            problems.append(
                {"code": "set-aside-without-reason", "detail": f"{entry['id']!r} has no reason"}
            )
        for identifier in ids:
            if identifier in set_aside:
                problems.append(
                    {"code": "set-aside-twice", "detail": f"{identifier!r} is set aside twice"}
                )
            if identifier in cited:
                problems.append(
                    {
                        "code": "cited-and-set-aside",
                        "detail": f"{identifier!r} is both cited and set aside",
                    }
                )
            set_aside.add(identifier)
    return problems


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
    images: list[bytes] | None = None
    capacity: dict[str, Any] | None = None
    refusal: RequestCapacityRefusal | None = None
    adopted: dict[str, Any] | None = None
    fixture_row: dict[str, Any] | None = None
    not_run: dict[str, str] | None = None


@dataclass
class _PagePass:
    """What every page of one pass reads under."""

    run: Any
    hooks: StageHooks
    audit: dict[str, Any]
    page_chairs: set[str]
    testimonia: dict[str, list[dict[str, Any]]]
    surya: dict[str, dict[str, Any]] | None
    row: Any = None

    @property
    def context(self):
        return self.run.context

    @property
    def live(self) -> bool:
        return self.run.serving_mode == "live"


def page_reading_attempt(page_id: str) -> str:
    return attempt_id(page_id, PAGE_READ_OPERATION, PAGE_READ_ORDINAL)


def _existing_reading(context, page_id: str) -> dict[str, Any] | None:
    identifier = _artifact(PAGE_READING_KIND, page_id, page_reading_attempt(page_id))
    if not context.tree.has_artifact(PERLECTOR, PAGE_READING_KIND, identifier):
        return None
    return context.tree.read_artifact(PERLECTOR, PAGE_READING_KIND, identifier)


def _artifact(kind: str, subject: str, attempt: str | None) -> str:
    return artifact_id(PERLECTOR, kind, subject, attempt)


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
        page.not_run = {
            "code": "page-not-sealed",
            "detail": "the Exemplar refused this page; there are no sealed pixels to read",
        }
        return page
    if isinstance(run.chair, AbsentChair):
        page.not_run = {
            "code": "chair-absent",
            "detail": "the Perlector chair is absent from this run's roster; nothing read the page",
        }
        return page
    page_record, page_bytes = read_sealed_page(context.tree, page_id, refusal=ContractError)
    width, height = dimensions(page_bytes)
    feed = page_feed.build_page_feed(
        page_id=page_id,
        page_ordinal=ordinal,
        page_size=(width, height),
        feed_switches=run.protocol_config["feed"],
        witness_regime=context.witness_context,
        roster=sorted(state.page_chairs),
        witnesses=_page_witnesses(
            context, page_id, state.testimonia.get(page_id, []), state.page_chairs
        ),
        surya=_surya_for(state.surya, page_id),
        page_render=_page_render(context, run.protocol_config, page_id, ordinal),
        serving_recipe=run.chair.serving_recipe,
        read_bytes=context.tree.read_bytes,
        fixture_placeholders=not state.hooks.real_ingress(context),
    )
    published = context.publish(
        kind=PAGE_FEED_KIND,
        subject_id=page_id,
        outcome="read",
        inputs=feed_inputs(context, feed),
        payload=feed,
    )
    page.feed, page.feed_ref = feed, context.input_ref(published.relative_path)
    page.text = page_prompt.build_page_prompt(run.chair.serving_recipe, feed)
    page.image_sha256s = [feed["page_render"]["image_sha256"]] if feed["page_render"] else []
    if feed["overlay"] is not None:
        page.image_sha256s.append(feed["overlay"]["image_sha256"])
    page.adopted = _existing_reading(context, page_id)
    if page.adopted is not None:
        return page
    if not state.live:
        page.fixture_row = _fixture_answer(context, ordinal)
        return page
    try:
        page.capacity = page_request_capacity(
            _serving_row(state),
            image_sizes=page_feed.request_image_sizes(feed),
            prompt_text=page.text,
            template_digest=page_prompt.BUILDER_SHA256,
            answer_measure=feed["answer_measure"],
            page_max_tokens=run.page_max_tokens,
        )
    except RequestCapacityRefusal as refusal:
        if refusal.capacity is None:
            raise
        page.refusal = refusal
        return page
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
    page.images = images
    return page


def _serving_row(state: _PagePass):
    if state.row is None:
        run = state.run
        state.row = bound_serving_recipes(
            run.context, run.args.serving_recipes_config
        ).for_identity(run.chair, run.args.placement_tier)
    return state.row


def _page_job(state: _PagePass, ordinal: int, page_id: str):
    """Prepare one page on the main thread; return its call, if any, and its in-order finish."""
    run, hooks = state.run, state.hooks
    page = _prepare(state, ordinal, page_id)
    finish = partial(_finish, state, page)
    if (
        page.not_run is not None
        or page.adopted is not None
        or page.refusal is not None
        or not state.live
    ):
        return None, finish
    hooks.refuse_past_deadline(
        run.args.reading_deadline,
        planned_seconds_per_page(run.page_max_tokens),
        f"reading page {ordinal}",
    )
    if run.service.client is None:
        hooks.start_chair(run)
    hooks.publish_sent(
        run, page_id, page_key(ordinal), PAGE_READ_ORDINAL, PAGE_READING_PASS, page.image_sha256s
    )
    return partial(_call, run, page), finish


def _call(run, page: _Page) -> dict[str, Any] | Exception:
    """The page's one call, safe on a worker thread: it publishes nothing."""
    try:
        return send_page_request(
            run.service.client,
            images=page.images,
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
        problems = validate_answer(answer, feed)
    return state, answer, problems


def _finish(state: _PagePass, page: _Page, result: dict[str, Any] | Exception | None) -> None:
    """Publish the page's `page-reading`, then its act records when it was read."""
    if page.adopted is not None:
        record = page.adopted
        _check_adopted(state, page, record)
    else:
        record = _publish_reading(state, page, result)
    if record["payload"]["disposition"] == READ:
        publish_act_records(state, page, record)


def _publish_reading(state: _PagePass, page: _Page, result) -> dict[str, Any]:
    run, hooks, context = state.run, state.hooks, state.context
    attempted = page.not_run is None and page.refusal is None
    engine_call = capacity = failure = answer = None
    finish_reason = stop_reason = None
    inputs: list[dict[str, str] | None] = [
        page.feed_ref,
        context.artifact_ref(EXEMPLAR, "page", page.page_record["artifact_id"]),
    ]
    receipt_ref = None
    if page.not_run is not None:
        parse_state, problems = NOT_RUN, [page.not_run]
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
            inputs += hooks.engine_call_inputs(context, engine_call)
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


# --- the answer's entries as act records --------------------------------------------


def _union(boxes: list[dict[str, int]]) -> dict[str, int] | None:
    if not boxes:
        return None
    x0 = min(box["x"] for box in boxes)
    y0 = min(box["y"] for box in boxes)
    x1 = max(box["x"] + box["w"] for box in boxes)
    y1 = max(box["y"] + box["h"] for box in boxes)
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def answer_entries(answer: dict[str, Any], feed: dict[str, Any]) -> list[dict[str, Any]]:
    """Each entry of a valid answer with its expanded ids, union box and entry holds."""
    known = feed_boxes(feed)
    entries = []
    for act in answer["acts"]:
        cited_ids, _problems = expand_cites(act["cites"], known)
        union = _union([known[identifier] for identifier in cited_ids if known[identifier]])
        entries.append(
            {
                "act": act,
                "cited_ids": cited_ids,
                "union_box_px": union,
                "holds": [] if union is not None else [UNPLACED],
            }
        )
    boxes = [digest_of(entry["union_box_px"]) for entry in entries if entry["union_box_px"]]
    for entry in entries:
        if entry["union_box_px"] and boxes.count(digest_of(entry["union_box_px"])) > 1:
            entry["holds"].append(DUPLICATE_REGION)
    return entries


def _dissent(text: str, feed: dict[str, Any], cited_ids: list[str]) -> list[dict[str, Any]]:
    """Where the entry's reading departed from each shown witness's cited units."""
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
        reported = "\n".join(unit["text"] for unit in units)
        [row] = dissent_against(
            text,
            [
                {
                    "outcome": page_feed.READ_OUTCOME,
                    "payload": {"chair": witness["letter"], "comparison_reported": reported},
                }
            ],
        )
        row.pop("chair")
        rows.append({**head, "cited_units": [unit["id"] for unit in units], **row})
    return rows


def publish_act_records(state: _PagePass, page: _Page, reading: dict[str, Any]) -> None:
    """One `act-region` and one `perlectio` per entry of a read page's answer."""
    run, hooks, context = state.run, state.hooks, state.context
    payload = reading["payload"]
    feed = page.feed
    reading_ref = context.artifact_ref(PERLECTOR, PAGE_READING_KIND, reading["artifact_id"])
    _page, page_bytes = read_sealed_page(context.tree, page.page_id, refusal=ContractError)
    page_ref = {
        "relative_path": page.page_record["payload"]["image_path"],
        "sha256": page.page_record["payload"]["source_sha256"],
    }
    page_size = feed["page_size"]
    page_pixels = page_size["w"] * page_size["h"]
    engine_inputs = hooks.engine_call_inputs(context, payload["engine_call"])
    attempt = page_reading_attempt(page.page_id)
    for entry in answer_entries(payload["answer"], feed):
        act, union = entry["act"], entry["union_box_px"]
        act_class = READING_CLASS if union is not None else UNPLACED_CLASS
        act_id = derive_act_id(
            page.page_id, act_class, {"page_reading": attempt, "n": act["n"], "union_box_px": union}
        )
        crop = (
            cut_exemplar_crop(context.retain, page_bytes, page.ordinal, page.page_id, union)
            if union is not None
            else None
        )
        holds = list(entry["holds"])
        region_payload = {
            "schema": ACT_REGION_SCHEMA,
            "page_id": page.page_id,
            "page_ordinal": page.ordinal,
            "reading_unit": READING_UNIT,
            "n": act["n"],
            "kind": act["kind"],
            "label": act.get("label"),
            "cites": list(act["cites"]),
            "cited_ids": entry["cited_ids"],
            "act_class": act_class,
            "page_reading_attempt": attempt,
            "union_box_px": union,
            "region_id": region_id(act_id, crop["transform"]) if crop else None,
            "image_path": crop["image_path"] if crop else None,
            "image_sha256": crop["image_sha256"] if crop else None,
            "transform": crop["transform"] if crop else None,
            "transform_digest": crop["transform_digest"] if crop else None,
            "page_reading_ref": reading_ref,
            "feed_ref": page.feed_ref,
            "holds": holds,
        }
        region_inputs = [reading_ref, page.feed_ref, page_ref]
        if crop:
            region_inputs.append(
                {"relative_path": crop["image_path"], "sha256": crop["image_sha256"]}
            )
        published = context.publish(
            kind=ACT_REGION_KIND,
            subject_id=act_id,
            outcome=HELD if holds else READ,
            attempt=attempt_id(act_id, ACT_REGION_OPERATION, 1),
            inputs=_distinct(region_inputs),
            payload=region_payload,
        )
        region_ref = context.input_ref(published.relative_path)
        text, assessment = annotations.read_doubt_marks(act["text"])
        if assessment["state"] == annotations.ASSESSMENT_MALFORMED:
            holds.append(DOUBT_MARKS_MALFORMED)
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
            holds.append(READING_INCOMPLETE)
        if record is not None and record["classification"] == truncation.TRUNCATED:
            outcome = "truncated"
        elif not text.strip():
            outcome = "no-readable-text"
        else:
            outcome = HELD if holds else READ
        context.publish(
            kind=PERLECTIO_KIND,
            subject_id=act_id,
            outcome=outcome,
            attempt=perlector_attempt_id(act_id, "perlegere", 1),
            inputs=_distinct([region_ref, reading_ref, page.feed_ref, *engine_inputs]),
            payload={
                "schema": PERLECTIO_SCHEMA,
                "page_id": page.page_id,
                "page_ordinal": page.ordinal,
                "reading_unit": READING_UNIT,
                "act_region_ref": region_ref,
                "page_reading_ref": reading_ref,
                "feed_ref": page.feed_ref,
                "n": act["n"],
                "kind": act["kind"],
                "label": act.get("label"),
                "text": text,
                "uncertain_spans": assessment["uncertain_spans"],
                "gaps": assessment["gaps"],
                "uncertainty_assessment": assessment,
                "dissent": _dissent(text, feed, entry["cited_ids"]),
                "truncation": record,
                "continues_from_previous_page": act["continues_from_previous_page"],
                "continues_to_next_page": act["continues_to_next_page"],
                "holds": holds,
                "engine_call": payload["engine_call"],
                "provenance": payload["provenance"],
            },
        )


# --- the pass ---------------------------------------------------------------------


def _pages_left(state: _PagePass, pages: dict[int, str]) -> int:
    """Count the pages a live pass still has to ask, refusing any it cannot resume.

    A page with a `page-reading` is never asked again. A page with sends and no
    reading is sent again only when no retained reply could be its answer.
    """
    context, hooks = state.context, state.hooks
    left, unrecorded, replies = 0, [], None
    for ordinal, page_id in pages.items():
        if _existing_reading(context, page_id) is not None:
            continue
        markers = hooks.sent_records(
            context, page_id, page_key(ordinal), PAGE_READ_ORDINAL, PAGE_READING_PASS
        )
        if markers:
            replies = hooks.unrecorded_replies(context) if replies is None else replies
            calls, unattributed = replies
            if unattributed or hooks.answers_a_send(calls, markers):
                unrecorded.append(page_id)
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
        page_chairs=hooks.declared_page_witness_chairs(context),
        testimonia=current_page_testimonia(context, hooks, run.all_proposal_regions),
        surya=sealed_surya_census(context),
    )
    if state.live and not isinstance(run.chair, AbsentChair):
        left = _pages_left(state, pages)
        if left:
            startup = _serving_row(state).startup_timeout_seconds
            hooks.refuse_past_deadline(
                run.args.reading_deadline,
                startup + left * planned_seconds_per_page(run.page_max_tokens),
                f"starting the Perlector ({startup}s) and reading {left} pages",
            )
    print(f"perlector: reading {len(pages)} pages whole", file=sys.stderr)
    hooks.in_order_window(
        run.concurrency,
        (_page_job(state, ordinal, page_id) for ordinal, page_id in pages.items()),
    )
