"""The Perlector's page path: each sealed page read whole, its answer made into act records.

Stage 4 reads every sealed Exemplar page in one call, shown
the page image and every witness's page broken into that witness's own units,
and the Perlector establishes the acts itself (`common/page_feed.py`,
`common/page_prompt.py`, `common/page_answer.py`). Per page, in the order published:

    page-feed       (subject page_id)  what the reading is shown, every id it may cite
    reader-sent     (subject page_id)  live only, before the call leaves
    page-reading    (subject page_id)  the answer as given: parsed, or held whole
    page-accounting (subject page_id)  rules a-i over the reading (`common/page_accounting.py`)
    act-region      (subject act_id)   one per entry of a parsed, valid answer
    perlectio       (subject act_id)   `perlectio.v3`, one per entry

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

The sealed Pass-C audit policy is recorded on every `page-reading` as not run.

What the records derive rather than state -- an answer's problems, each
entry's plan, the accounting's inputs -- is `common/page_path.py`, the one
derivation the page-read denominator recomputes them with. The record shapes
are in CONTRACT.md, "Page reading".
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable, Final

from live_reader import EngineSignalRefusal, send_page_request
from throughput import planned_seconds_per_page

import operations.serving.errors as serving_errors
from common import (
    page_accounting,
    page_path,
)
from common.chairs.models import AbsentChair, ChairIdentity
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import (
    artifact_id,
    region_id,
)
from common.contracts.stages import DESIGNATOR, EXEMPLAR, INK_MAP, PERLECTOR
from common.exemplar_boundary import cut_exemplar_crop, read_sealed_page
from common.imaging import dimensions
from common.page_path import (
    ACT_REGION_KIND,
    ACT_REGION_SCHEMA,
    CALL_FAILED,
    HELD,
    NOT_RUN,
    PAGE_ACCOUNTING_KIND,
    PAGE_FEED_KIND,
    PAGE_NOT_SEALED,
    PAGE_READ_ORDINAL,
    PAGE_READING_KIND,
    PAGE_READING_SCHEMA,
    PARSED,
    PERLECTIO_KIND,
    READ,
    REFUSED_CAPACITY,
)
from common.page_testimonia import (
    current_page_testimonia,
    declared_page_witness_chairs,
)
from common.perlector_audit import audit_not_run
from common.request_capacity import RequestCapacityRefusal
from common.stage import (
    SECONDARY_PROPOSER_CHAIR,
    exemplar_page_ids,
    stage_manifest,
)
from operations.serving.assembly import bound_serving_recipes
from operations.serving.errors import ChairResponseRefusal
from operations.serving.http import EndpointUnavailable

# The `reader-sent` pass a page's call is recorded under.
PAGE_READING_PASS: Final = "page-reading"

_PAGE_LOCAL_CALL_FAILURES: Final = (
    EngineSignalRefusal,
    ChairResponseRefusal,
    EndpointUnavailable,
    serving_errors.ChairTransportFailure,
)


@dataclass(frozen=True)
class StageHooks:
    """The helpers of `run.py` the page path reads its evidence through."""

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


# --- sealed inputs ----------------------------------------------------------------


def page_key(page_ordinal: int) -> str:
    """The key a page's `reader-sent` records name, as the Attestatores key a page."""
    return f"page-{page_ordinal}"


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


def _artifact(kind: str, subject: str, attempt: str | None) -> str:
    return artifact_id(PERLECTOR, kind, subject, attempt)


def _sealed(context, kind: str, subject: str, attempt: str | None) -> dict[str, Any] | None:
    """The record this stage already published under that identity, or `None`."""
    identifier = _artifact(kind, subject, attempt)
    if not context.tree.has_artifact(PERLECTOR, kind, identifier):
        return None
    return context.tree.read_artifact(PERLECTOR, kind, identifier)


def _existing_reading(context, page_id: str) -> dict[str, Any] | None:
    return _sealed(context, PAGE_READING_KIND, page_id, page_path.page_reading_attempt(page_id))


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
    feed, page.witnesses, inputs = page_path.page_feed_of(
        context,
        page_id=page_id,
        ordinal=ordinal,
        page_size=(width, height),
        protocol_config=run.protocol_config,
        page_chairs=state.page_chairs,
        current=current,
        surya_census=state.surya,
        serving_recipe=run.chair.serving_recipe if state.chair_present else None,
        fixture_placeholders=not state.hooks.real_ingress(context),
    )
    published = context.publish(
        kind=PAGE_FEED_KIND,
        subject_id=page_id,
        outcome="read",
        inputs=inputs,
        payload=feed,
    )
    page.feed, page.feed_ref = feed, context.input_ref(published.relative_path)
    page.not_run = page_path.not_run_problems(
        feed, chair_present=state.chair_present, no_testimony=no_testimony
    )
    page.adopted = _existing_reading(context, page_id)
    if page.not_run or page.adopted is not None:
        return page
    page.text = page_path.request_text(run.chair.serving_recipe, feed)
    page.image_sha256s = page_path.request_image_sha256s(feed)
    if not state.live:
        page.fixture_row = page_path.fixture_page_answer(context, ordinal)
        return page
    try:
        page.capacity = page_path.request_capacity(
            _serving_row(state),
            run.chair.serving_recipe,
            feed,
            page.text,
            run.page_max_tokens,
        )
    except RequestCapacityRefusal as refusal:
        if refusal.capacity is None:
            raise
        page.refusal = refusal
    return page


def _sends(state: _PagePass, page: _Page) -> bool:
    """Whether this pass sends the page's call: live, asked, not refused, not already read."""
    return state.live and not page.not_run and page.adopted is None and page.refusal is None


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
    images = page_path.request_images(page.feed, state.context.tree.read_bytes)
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


def _finish(state: _PagePass, page: _Page, result: dict[str, Any] | Exception | None) -> None:
    """Publish the page's reading, then its accounting, then its act records."""
    if page.adopted is not None:
        reading = page.adopted
        _check_adopted(state, page, reading)
    else:
        reading = _publish_reading(state, page, result)
    if page.feed is None:
        return
    payload = reading["payload"]
    plans = (
        page_path.entry_plans(
            payload["answer"],
            page.feed,
            page_id=page.page_id,
            stop_reason=payload["stop_reason"],
            truncation_policy=state.run.protocol_config["truncation"],
            accounting_policy=state.accounting_policy,
        )
        if payload["disposition"] == READ
        else []
    )
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
        parse_state, answer, problems = page_path.read_reply(
            row["answer"], stop_reason, page.feed, state.accounting_policy
        )
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
            parse_state, answer, problems = page_path.read_reply(
                result["content"], stop_reason, page.feed, state.accounting_policy
            )
    disposition = READ if parse_state == PARSED and not problems else HELD
    payload = {
        "schema": PAGE_READING_SCHEMA,
        "page_id": page.page_id,
        "page_ordinal": page.ordinal,
        "feed_ref": page.feed_ref,
        "request_digest": (
            page_path.request_digest(page.text, page.image_sha256s) if attempted else None
        ),
        "engine_call": engine_call,
        "sampling": page_path.page_sampling(run.decoding_policy, run.chair.role)
        if receipt_ref is not None
        else None,
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
    attempt = page_path.page_reading_attempt(page.page_id)
    context.publish(
        kind=PAGE_READING_KIND,
        subject_id=page.page_id,
        outcome=disposition,
        attempt=attempt,
        inputs=page_path.distinct_refs(inputs),
        payload=payload,
    )
    return context.tree.read_artifact(
        PERLECTOR, PAGE_READING_KIND, _artifact(PAGE_READING_KIND, page.page_id, attempt)
    )


def _check_adopted(state: _PagePass, page: _Page, record: dict[str, Any]) -> None:
    """Refuse a retained page reading this run could not have made from this page's feed."""
    payload = record["payload"]
    if (
        not isinstance(payload, dict)
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
        state.hooks.engine_call_inputs(state.context, payload["engine_call"])
        if payload.get("sampling") != page_path.page_sampling(
            state.run.decoding_policy, state.run.chair.role
        ):
            raise ContractError(
                f"page {page.page_id}'s retained page reading names sampling other than the "
                "sealed Perlector row; it is not adopted and the page is not asked again. "
                "Read this page in a new run"
            )


# --- the answer's entries -----------------------------------------------------------


def _check_adopted_perlectio(record: dict[str, Any], expected: dict[str, Any], act_id: str) -> None:
    """Refuse a retained Perlectio other than the one this entry and its page records give.

    Every field but its dissent must be `page_path.expected_perlectio`'s; the
    dissent is adopted as sealed rather than aligned again, and the page-read
    denominator measures it again exactly (`page_path.dissent_holds`).
    """
    payload = record["payload"]
    if (
        not isinstance(payload, dict)
        or set(payload) != set(expected) | {"dissent"}
        or any(payload[name] != value for name, value in expected.items())
    ):
        raise ContractError(
            f"act {act_id}'s retained perlectio names another region, reading, feed, "
            "accounting or entry than this page has now; it is not adopted. Read this page in "
            "a new run"
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
    `perlectio` already sealed for the entry is adopted when every field but
    its dissent is the expected one and its dissent is a valid page dissent under
    the run's sealed budget, so a resumed pass aligns nothing again.
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
    engine_inputs = state.hooks.engine_call_inputs(context, payload["engine_call"])
    attempt = page_path.page_reading_attempt(page.page_id)
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
            "n": act["n"],
            "kind": act["kind"],
            "label": act.get("label"),
            "cites": list(act["cites"]),
            "cited_ids": list(plan["cited_ids"]),
            "act_class": plan["act_class"],
            "page_reading_attempt": attempt,
            "region_boxes_px": list(plan["region_boxes_px"]),
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
            attempt=page_path.region_attempt(act_id),
            inputs=page_path.distinct_refs(region_inputs),
            payload=region_payload,
        )
        region_ref = context.input_ref(published.relative_path)
        refs = {
            "act_region_ref": region_ref,
            "page_reading_ref": reading_ref,
            "page_accounting_ref": accounting_ref,
            "feed_ref": page.feed_ref,
        }
        expected = page_path.expected_perlectio(
            page_id=page.page_id,
            ordinal=page.ordinal,
            plan=plan,
            refs=refs,
            page_holds=page_holds,
            reading=payload,
        )
        perlectio_attempt = page_path.perlectio_attempt(act_id)
        adopted = _sealed(context, PERLECTIO_KIND, act_id, perlectio_attempt)
        if adopted is not None:
            _check_adopted_perlectio(adopted, expected, act_id)
            try:
                page_path.validate_page_dissent(
                    adopted["payload"].get("dissent"),
                    text=adopted["payload"].get("text"),
                    feed=page.feed,
                    cited_ids=plan["cited_ids"],
                    max_comparison_steps=state.run.dissent_steps,
                )
            except SchemaRefusal as error:
                raise ContractError(
                    f"act {act_id}'s retained perlectio carries a dissent record this run "
                    f"cannot stand behind ({error}); it is not adopted. Read this page in a "
                    "new run"
                ) from error
            continue
        context.publish(
            kind=PERLECTIO_KIND,
            subject_id=act_id,
            outcome=HELD if plan["reading_holds"] or page_holds else READ,
            attempt=perlectio_attempt,
            inputs=page_path.distinct_refs(
                [region_ref, reading_ref, accounting_ref, page.feed_ref, *engine_inputs]
            ),
            payload={
                **expected,
                "dissent": page_path.page_dissent(
                    plan["text"],
                    page.feed,
                    plan["cited_ids"],
                    page.witnesses,
                    state.run.dissent_steps,
                ),
            },
        )


# --- the page accounting -----------------------------------------------------------


def publish_page_accounting(
    state: _PagePass, page: _Page, reading: dict[str, Any], plans: list[dict[str, Any]]
) -> dict[str, Any]:
    """The page's `page-accounting`: rules a-i over its reading, witnesses and detections.

    Published before any act record, which names it. A resumed pass adopts the
    one already sealed for the page when it was measured from exactly these
    inputs under this policy, and refuses by name when any input differs.
    """
    context = state.context
    reading_ref = context.artifact_ref(PERLECTOR, PAGE_READING_KIND, reading["artifact_id"])
    measured, inputs = page_path.accounting_inputs(
        context,
        feed=page.feed,
        feed_ref=page.feed_ref,
        reading=reading["payload"],
        reading_ref=reading_ref,
        witnesses=page.witnesses,
        plans=plans,
        surya_census=state.surya,
        designator_entries=stage_manifest(context, DESIGNATOR)["artifacts"],
        ink_entries=stage_manifest(context, INK_MAP)["artifacts"],
        record_detector_configured=isinstance(
            context.registry.resolve(SECONDARY_PROPOSER_CHAIR), ChairIdentity
        ),
        fixture_placeholders=not state.hooks.real_ingress(context),
    )
    # Bound to the reading's attempt: a later reading of the page is accounted apart.
    attempt = page_path.page_reading_attempt(page.page_id)
    sealed = _sealed(context, PAGE_ACCOUNTING_KIND, page.page_id, attempt)
    if sealed is not None:
        payload = sealed["payload"]
        if (
            page_path.refs_by_path(sealed["inputs"]) != page_path.refs_by_path(inputs)
            or not isinstance(payload, dict)
            or payload.get("schema") != page_accounting.SCHEMA
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
    accounting = page_accounting.page_accounting(**measured, policy=state.accounting_policy)
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
    pages = exemplar_page_ids(context)
    if not pages:
        raise ContractError("the Exemplar sealed no page, so the page path has nothing to read")
    state = _PagePass(
        run=run,
        hooks=hooks,
        audit=audit_not_run(run.audit_policy, run.audit_sha256),
        page_chairs=declared_page_witness_chairs(context),
        testimonia=current_page_testimonia(context),
        surya=page_path.sealed_surya_census(
            context, stage_manifest(context, DESIGNATOR)["artifacts"]
        ),
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
