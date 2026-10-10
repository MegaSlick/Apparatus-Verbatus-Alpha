"""The Perlector's page path: each sealed page read whole, its answer made into act records.

Stage 4 reads every sealed Exemplar page in one call, shown
the page image and every witness's page broken into that witness's own units,
and the Perlector establishes the acts itself (`common/page_feed.py`,
`common/page_prompt.py`, `common/page_answer.py`). Per page, in the order published:

    page-feed       (subject page_id)  what the reading is shown, every id it may cite
    reader-sent     (subject page_id)  live only, before the call leaves
    page-reading    (subject page_id)  the answer as given: parsed, or held whole
    page-accounting (subject page_id)  rules a-j over the reading (`common/page_accounting.py`)
    act-region      (subject act_id)   one per entry of a parsed, valid answer
    perlectio       (subject act_id)   `perlectio.v3`, one per entry

Every feed is built and published before any page is read, so a live pass
counts exactly the pages it will send before its chair starts.

The pass runs in two phases. The first reads every page once, as above. A
page whose first accounting finds ids the reading left unaccounted for is
re-asked once, about those ids only (`common/page_reask.py`, bounded by the
sealed `[budget] page_level_reread`): its act records wait, and the second
phase sends every planned re-ask in its own window, under its own deadline
check, and publishes for each page the re-ask's `page-reading` (attempt 2),
the accounting of both readings together, and then the act records of both.
The first reading and its accounting are never changed; the re-ask only adds
entries, each of which says it was read on re-ask.

A third phase reads again each page a person's current page `re-ask` decision
asks for (`common/page_reread.py`): an operator re-read, attempt 3 and on,
asked the first reading's request over the same feed, bound to its decisions
and naming every earlier reading of the page, which it supersedes. It is
published with its own accounting and act records, and becomes the page's
current reading; the earlier readings and their records stay as read. It is
outside the sealed `page_level_reread` budget and is never re-asked.

The accounting is measured before any act record is published, so every act
record names it (`page_accounting_ref`) and carries the page's hold codes
(`page_holds`): an act on a held page is held. An answer that is not the
grammar, is cut off at the output cap, cites an id the feed does not define,
or could not be asked is held whole on its `page-reading` and makes no act
record: nothing is trimmed or split, and the one repair is quoting bare grammar
keys, recorded as `answer_repairs` (`common.page_answer`). An entry of a valid answer that
cites no placing id is still published, as a held `reading-unplaced` act with
no crop. Every page has a `page-reading`, a page the Exemplar refused
included, so no page is silently absent; every sealed page has a feed and an
accounting too, even one no witness testified to, one that shows nothing to
read, or one read in a run whose Perlector chair is absent.

The sealed Pass-C audit policy is recorded on every `page-reading` as not run.

What the records derive rather than state -- an answer's problems, each
entry's plan, the accounting's inputs -- is `common/page_path.py`, the one
derivation the page-read denominator recomputes them with, and the re-ask's
plan is `common/page_reask.py`'s. The record shapes are in CONTRACT.md.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from typing import Any, Callable, Final

import live_calls
from throughput import planned_seconds_per_page

from common import (
    page_accounting,
    page_path,
    page_reask,
    page_reread,
    replay,
)
from common.chairs.models import AbsentChair, ChairIdentity
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal
from common.contracts.identities import (
    artifact_id,
    region_id,
)
from common.contracts.stages import DESIGNATOR, EXEMPLAR, INK_MAP, PERLECTOR
from common.exemplar_boundary import cut_exemplar_crop, read_sealed_page
from common.imaging import dimensions
from common.page_edges import FIRST_READING, OPERATOR_REREAD_FIRST, REASK_READING
from common.page_path import (
    ACT_REGION_KIND,
    ACT_REGION_SCHEMA,
    ANSWER_REPAIRS_FIELD,
    CALL_FAILED,
    HELD,
    NOT_RUN,
    OPERATOR_REREAD_FIELD,
    PAGE_ACCOUNTING_KIND,
    PAGE_FEED_KIND,
    PAGE_NOT_SEALED,
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
    fixture_serving_details,
    is_real_ingress,
    stage_manifest,
)
from operations.serving.assembly import bound_serving_recipes
from operations.serving.chat_request import send_page_request

# The `reader-sent` pass a page's call is recorded under, its re-ask's, and an
# operator re-read's.
PAGE_READING_PASS: Final = "page-reading"
PAGE_REASK_PASS: Final = "page-reask"
PAGE_REREAD_PASS: Final = "page-reread"


def page_key(page_ordinal: int) -> str:
    """The key a page's `reader-sent` records name, as the Attestatores key a page."""
    return f"page-{page_ordinal}"


def provenance_for(
    context,
    resolved: ChairIdentity | AbsentChair,
    *,
    attempted: bool,
    receipt_ref: dict[str, str] | None = None,
) -> dict:
    """Project one Perlector outcome's immutable provenance.

    An outcome that attempted no reading (a page not asked, an absent chair) names what
    would have read and carries no receipt. An attempted reading re-verifies the snapshot when
    it is made. `receipt_ref` is the live chair's own receipt, passed only
    in live mode: a fixture receipt beside a real engine's reading would put a declared
    value where a measurement belongs.
    """
    if receipt_ref is not None and not attempted:
        raise SchemaRefusal(
            "a Perlector outcome that attempted no reading cannot carry a serving receipt; "
            "a page not asked and an absent chair name what would have read and stop there"
        )
    if receipt_ref is not None and isinstance(resolved, AbsentChair):
        raise SchemaRefusal(
            "an absent Perlector chair served nothing, so a receipt reference "
            "would name a serving moment this chair never had"
        )
    regime = {
        # A reading's provenance includes what its reader was shown, so every Perlectio
        # records its witness regime.
        "witness_regime": context.witness_context,
        "adapter_revision": context.adapter_revision,
    }
    if isinstance(resolved, AbsentChair):
        return {
            "chair": resolved.role,
            "chair_state": "absent",
            "absence": resolved.to_record(),
            "resolved_identity": None,
            "resolved_revision": None,
            "receipt_ref": None,
            **regime,
        }
    return {
        "chair": resolved.role,
        "chair_state": "configured",
        "resolved_identity": resolved.to_record(),
        "resolved_revision": {
            "kind": resolved.receipt_revision_kind,
            "value": resolved.receipt_revision,
        },
        "receipt_ref": (
            (
                dict(receipt_ref)
                if receipt_ref is not None
                else context.write_serving_receipt(resolved, fixture_serving_details(resolved))
            )
            if attempted
            else None
        ),
        **regime,
    }


# --- one page ---------------------------------------------------------------------


@dataclass
class _Request:
    """One reading of a page -- its first, or its re-ask -- as it is asked, or was."""

    ordinal: int
    pass_name: str
    text: str | None = None
    image_sha256s: list[str] | None = None
    capacity: dict[str, Any] | None = None
    refusal: RequestCapacityRefusal | None = None
    adopted: dict[str, Any] | None = None
    fixture_row: dict[str, Any] | None = None
    # The ids a re-ask may cite, and its `reask` record; `None` on a first reading.
    named: list[str] | None = None
    reask: dict[str, Any] | None = None
    # An operator re-read's `operator_reread` record; `None` on any other reading.
    reread: dict[str, Any] | None = None
    # A replay's re-ask that its source run never sent: recorded as not asked.
    not_replayed: bool = False
    # What the reading names beyond the feed and the page: a re-ask's trigger
    # records, or an operator re-read's decisions and the readings it supersedes.
    inputs: list[dict[str, str]] = field(default_factory=list)


@dataclass
class _Page:
    """One page's sealed inputs and its requests, resolved on the main thread."""

    page_id: str
    ordinal: int
    page_record: dict[str, Any]
    first: _Request = field(default_factory=lambda: _Request(FIRST_READING, PAGE_READING_PASS))
    reask: _Request | None = None
    feed: dict[str, Any] | None = None
    feed_ref: dict[str, str] | None = None
    # Every reason the page is not asked; empty for a page that is.
    not_run: list[dict[str, str]] = field(default_factory=list)
    page_size: tuple[int, int] | None = None
    witnesses: list[dict[str, Any]] = field(default_factory=list)
    # The first reading, its entry plans and accounting, once published or adopted.
    reading: dict[str, Any] | None = None
    plans: list[dict[str, Any]] = field(default_factory=list)
    # The page's operator re-reads in attempt order: those already read, then a new one.
    rereads: list[_Request] = field(default_factory=list)
    # Whether its first reading has been finished: published or adopted, its re-ask planned.
    first_finished: bool = False
    # The entry plans an operator re-read is planned against: the first reading's
    # with any the re-ask added, then the last operator re-read's that read anything
    # and kept every act it replaced.
    counted: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _Sealed:
    """The page records this stage already holds for one page, by reading."""

    readings: set[str] = field(default_factory=set)
    # The attempts among them that are operator re-reads: they name their decisions.
    rereads: set[str] = field(default_factory=set)
    accountings: set[str] = field(default_factory=set)
    # The reading ordinal (1, or 2 for an entry the re-ask read) of each act record.
    act_readings: set[int] = field(default_factory=set)


@dataclass
class _PagePass:
    """What every page of one pass reads under."""

    run: Any
    audit: dict[str, Any]
    page_chairs: set[str]
    testimonia: dict[str, list[dict[str, Any]]]
    surya: dict[str, dict[str, Any]] | None
    accounting_policy: Any
    reask_budget: int
    # What each page already holds, read before the pass publishes anything.
    sealed: dict[str, _Sealed] = field(default_factory=dict)
    row: Any = None
    # The chair's start on a background thread while pages are prepared, if begun.
    starting: Any = None
    # The source run a replay answers from (`common.replay`); `None` on any other run.
    replay: Any = None

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


def _existing_reading(context, page_id: str, ordinal: int) -> dict[str, Any] | None:
    return _sealed(
        context, PAGE_READING_KIND, page_id, page_path.page_reading_attempt(page_id, ordinal)
    )


def _sealed_pages(context) -> dict[str, _Sealed]:
    """Every page's page-reading and page-accounting attempts and act records, by page."""
    found: dict[str, _Sealed] = {}
    # Only attempts and pages are read here, from the records the manifest listed, so
    # a record whose input is gone is still seen; each record's inputs are checked
    # where it is used.
    for entry in context.tree.build_manifest(PERLECTOR, verify_inputs=False)["artifacts"]:
        kind = entry["kind"]
        if kind not in (PAGE_READING_KIND, PAGE_ACCOUNTING_KIND, ACT_REGION_KIND, PERLECTIO_KIND):
            continue
        record = json.loads(context.tree.read_bytes(entry["relative_path"]))
        if kind == PAGE_READING_KIND:
            sealed = found.setdefault(entry["subject_id"], _Sealed())
            sealed.readings.add(record["attempt_id"])
            if isinstance(record["payload"], dict) and OPERATOR_REREAD_FIELD in record["payload"]:
                sealed.rereads.add(record["attempt_id"])
        elif kind == PAGE_ACCOUNTING_KIND:
            found.setdefault(entry["subject_id"], _Sealed()).accountings.add(record["attempt_id"])
        else:
            payload = record["payload"]
            found.setdefault(payload.get("page_id"), _Sealed()).act_readings.add(
                payload.get("reading_attempt", FIRST_READING)
            )
    return found


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
        fixture_placeholders=not is_real_ingress(context.run),
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
    request = page.first
    request.adopted = _existing_reading(context, page_id, FIRST_READING)
    if page.not_run or request.adopted is not None:
        return page
    request.text = page_path.request_text(run.chair.serving_recipe, feed)
    request.image_sha256s = page_path.request_image_sha256s(feed)
    if not state.live:
        request.fixture_row = page_path.fixture_page_answer(context, ordinal)
        return page
    _admit(
        state,
        request,
        lambda row: page_path.request_capacity(
            row, run.chair.serving_recipe, feed, request.text, run.page_generation
        ),
    )
    _require_recorded(state, page, request)
    return page


def _recorded(state: _PagePass, page: _Page, request: _Request) -> dict[str, Any] | None:
    """The source's answered reading of exactly this request, on a replay; else `None`."""
    recorded = state.replay.reading(page.page_id, request.ordinal)
    if (
        recorded is None
        or recorded.get("engine_call") is None
        or recorded.get("request_digest")
        != page_path.request_digest(request.text, request.image_sha256s)
    ):
        return None
    return recorded


def _require_recorded(state: _PagePass, page: _Page, request: _Request) -> None:
    """On a replay, refuse a reading its source run never sent in exactly these bytes.

    A page whose reading cannot be answered could not be read at all, so the
    replay stops rather than record every such page unread.
    """
    if state.replay is None or not _sends(state, page, request):
        return
    if _recorded(state, page, request) is None:
        raise ContractError(
            f"page {page.ordinal}'s reading request is not one run {state.replay.run_id} "
            "sent and got an answer to, so no recorded reply can answer it; the code being "
            "replayed asks the Perlector something new. Read the page in a live run"
        )


def _admit(state: _PagePass, request: _Request, measure: Callable[[Any], dict[str, Any]]) -> None:
    """Admit a live request against the sealed serving row, or record the row's refusal."""
    try:
        request.capacity = measure(_serving_row(state))
    except RequestCapacityRefusal as refusal:
        if refusal.capacity is None:
            raise
        request.refusal = refusal


def _sends(state: _PagePass, page: _Page, request: _Request) -> bool:
    """Whether this pass sends the request: live, asked, not refused, not already read,
    and, on a replay, recorded."""
    return (
        state.live
        and not page.not_run
        and request.adopted is None
        and request.refusal is None
        and not request.not_replayed
    )


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
    live_calls.refuse_past_deadline(
        state.run.args.reading_deadline,
        seconds_needed,
        what,
        rate=f"{per_page}s a page (planned_seconds_per_page for the sealed page answer cap)",
        remedy="Give a later --reading-deadline; a page-read pass reads every sealed page",
    )


def _job(state: _PagePass, page: _Page, request: _Request, finish: Callable[[Any], Any]):
    """One request's call, if it is sent, and its in-order finish."""
    run = state.run
    if not _sends(state, page, request):
        return None, finish
    what = {FIRST_READING: "reading", REASK_READING: "re-asking"}.get(request.ordinal, "re-reading")
    _refuse_past_page_deadline(
        state, planned_seconds_per_page(run.page_max_tokens), f"{what} page {page.ordinal}"
    )
    images = page_path.request_images(page.feed, state.context.tree.read_bytes)
    if run.service.client is None:
        live_calls.start_chair(run)
    live_calls.publish_sent(
        run,
        page.page_id,
        page_key(page.ordinal),
        request.ordinal,
        request.pass_name,
        request.image_sha256s,
    )
    return partial(_call, run, page, request, images), finish


def _call(run, page: _Page, request: _Request, images: list[bytes]) -> dict[str, Any] | Exception:
    """The request's one call, safe on a worker thread: it publishes nothing."""
    try:
        return send_page_request(
            run.service.client,
            images=images,
            text=request.text,
            capacity=request.capacity["capacity"],
            max_tokens=request.capacity["max_tokens"],
            what=f"page {page.page_id}",
            loop_guard=run.loop_guard,
        )
    except live_calls.CALL_FAILURES as error:
        return error


def _finish(state: _PagePass, page: _Page, result: dict[str, Any] | Exception | None) -> None:
    """Publish the page's first reading and its accounting, and plan its re-ask.

    A page with no re-ask publishes its act records now; a planned page's
    wait for its re-ask, so that every act record names the page's final
    accounting.
    """
    reading = _reading(state, page, page.first, result)
    if page.feed is None:
        _refuse_stray_attempts(state, page, planned=False)
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
    named = page_reask.reask_plan(
        payload,
        accounting["payload"],
        page.feed,
        budget=state.reask_budget,
        policy=state.accounting_policy,
    )
    _refuse_stray_attempts(state, page, planned=bool(named))
    if not named:
        if state.reask_budget and not state.live:
            page_path.fixture_reask_answer(state.context, page.ordinal, planned=False)
        page_path.hold_doubtful_page(plans, state.accounting_policy)
        publish_act_records(state, page, reading, plans, accounting)
        page.counted = plans
        return
    page.reading, page.plans = reading, plans
    page.reask = _prepare_reask(state, page, reading, accounting, named)


def _refuse_stray_attempts(state: _PagePass, page: _Page, *, planned: bool) -> None:
    """Refuse a page record the plan does not account for, by name.

    A page holds its first reading and, only when its plan re-asks it, one
    re-ask. A page the plan never re-asks may hold no attempt-2 reading,
    accounting or act record, and no page any attempt past the re-ask. A
    planned page with no re-ask reading yet may hold neither the re-ask's
    accounting nor any act record: those are published only after the
    re-ask, naming its accounting.
    """
    sealed = state.sealed.get(page.page_id, _Sealed())
    first = page_path.page_reading_attempt(page.page_id, FIRST_READING)
    second = page_path.page_reading_attempt(page.page_id, REASK_READING)
    rereads = {
        page_path.page_reading_attempt(page.page_id, ordinal)
        for ordinal in _reread_ordinals(state, page)
    }
    for what, attempts in (("reading", sealed.readings), ("accounting", sealed.accountings)):
        if attempts - {first, second} - rereads:
            raise FatalAccounting(
                f"page {page.page_id} carries a page {what} attempt past its one re-ask "
                f"({sorted(attempts - {first, second} - rereads)}) that is no operator re-read "
                "of it; a page is read once, re-asked at most once and read again only as a "
                "person asks, so it is not counted. Read this page in a new run"
            )
    if not planned:
        stray = [
            what
            for what, found in (
                ("reading", second in sealed.readings),
                ("accounting", second in sealed.accountings),
                ("act records", REASK_READING in sealed.act_readings),
            )
            if found
        ]
        if stray:
            raise FatalAccounting(
                f"page {page.page_id} carries a re-ask's {', '.join(stray)} (page-read:2), but "
                "its first reading and accounting plan none under the sealed "
                "page_level_reread; it is not counted. Read this page in a new run"
            )
    elif second not in sealed.readings and (second in sealed.accountings or sealed.act_readings):
        raise FatalAccounting(
            f"page {page.page_id} is planned for a re-ask, yet its re-ask accounting or act "
            "records were published without its re-ask reading; its act records would name an "
            "accounting that is not the page's last. Read this page in a new run"
        )


def _reread_ordinals(state: _PagePass, page: _Page) -> list[int]:
    """The ordinals of the page's sealed operator re-reads: 3 on, without a gap."""
    readings = state.sealed.get(page.page_id, _Sealed()).rereads
    ordinals = []
    ordinal = OPERATOR_REREAD_FIRST
    while page_path.page_reading_attempt(page.page_id, ordinal) in readings:
        ordinals.append(ordinal)
        ordinal += 1
    return ordinals


def _plan_rereads(state: _PagePass, prepared: list[_Page]) -> None:
    """Resolve every page's operator re-reads: each one sealed, then one a decision asks for.

    A sealed re-read is adopted only when it answers stored page re-asks of
    its page and supersedes exactly the page's earlier readings
    (`page_path.require_operator_reread`). A new one is asked for each page a
    current re-ask decision no re-read answers yet (`page_reread.requested_rereads`).
    """
    context = state.context
    stored = page_reread.stored_decisions(context.tree)
    requested = page_reread.requested_rereads(context.tree, stored)
    for page in prepared:
        if page.feed is None:
            if page.page_id in requested:
                raise ContractError(
                    f"a person asked to read page {page.page_id} again, but the Exemplar "
                    "refused it; there are no sealed pixels to read"
                )
            continue
        supersedes = [
            context.artifact_ref(PERLECTOR, PAGE_READING_KIND, record["artifact_id"])
            for record in (
                _existing_reading(context, page.page_id, FIRST_READING),
                _existing_reading(context, page.page_id, REASK_READING),
            )
            if record is not None
        ]
        for ordinal in _reread_ordinals(state, page):
            record = _existing_reading(context, page.page_id, ordinal)
            block = record["payload"].get(OPERATOR_REREAD_FIELD)
            page_path.require_operator_reread(
                block,
                run_id=context.tree.run_id,
                page_id=page.page_id,
                stored=stored,
                supersedes=supersedes,
                what=f"page {page.page_id}'s operator re-read {ordinal}",
            )
            decisions = [stored[item["approval_ref"]["sha256"]] for item in block["decisions"]]
            page.rereads.append(_prepare_reread(state, page, ordinal, decisions, supersedes))
            supersedes = [
                *supersedes,
                context.artifact_ref(PERLECTOR, PAGE_READING_KIND, record["artifact_id"]),
            ]
        if page.page_id in requested:
            ordinal = OPERATOR_REREAD_FIRST + len(page.rereads)
            page.rereads.append(
                _prepare_reread(state, page, ordinal, requested[page.page_id], supersedes)
            )


def _prepare_reread(
    state: _PagePass,
    page: _Page,
    ordinal: int,
    decisions: list[page_reread.Decision],
    supersedes: list[dict[str, str]],
) -> _Request:
    """Resolve one operator re-read: the first reading's request, bound to its decisions."""
    run, context = state.run, state.context
    approvals = [reference.to_record() for reference, _record in decisions]
    request = _Request(
        ordinal,
        PAGE_REREAD_PASS,
        reread=page_path.operator_reread_record(
            [
                (reference, record)
                for reference, (_ref, record) in zip(approvals, decisions, strict=True)
            ],
            supersedes,
        ),
        inputs=[*supersedes, *approvals],
    )
    request.adopted = _existing_reading(context, page.page_id, ordinal)
    if page.not_run or request.adopted is not None:
        return request
    request.text = page_path.request_text(run.chair.serving_recipe, page.feed)
    request.image_sha256s = page_path.request_image_sha256s(page.feed)
    if not state.live:
        request.fixture_row = page_path.fixture_page_answer(context, page.ordinal)
        return request
    _admit(
        state,
        request,
        lambda row: page_path.request_capacity(
            row, run.chair.serving_recipe, page.feed, request.text, run.page_generation
        ),
    )
    _require_recorded(state, page, request)
    return request


def _finish_reread(
    state: _PagePass, page: _Page, request: _Request, result: dict[str, Any] | Exception | None
) -> None:
    """Publish an operator re-read, its own accounting, then its act records."""
    reading = _reading(state, page, request, result)
    payload = reading["payload"]
    plans = (
        page_path.entry_plans(
            payload["answer"],
            page.feed,
            page_id=page.page_id,
            stop_reason=payload["stop_reason"],
            truncation_policy=state.run.protocol_config["truncation"],
            accounting_policy=state.accounting_policy,
            attempt=request.ordinal,
            superseded=page.counted,
        )
        if payload["disposition"] == READ
        else []
    )
    accounting = publish_page_accounting(state, page, reading, plans, attempt=request.ordinal)
    page_path.hold_doubtful_page(plans, state.accounting_policy)
    publish_act_records(state, page, reading, plans, accounting)
    if page_path.keeps_counted(plans):
        page.counted = plans


def _prepare_reask(
    state: _PagePass,
    page: _Page,
    reading: dict[str, Any],
    accounting: dict[str, Any],
    named: list[dict[str, Any]],
) -> _Request:
    """Resolve one planned page's re-ask: what it shows, its request and its admission."""
    run, context = state.run, state.context
    reading_ref = context.artifact_ref(PERLECTOR, PAGE_READING_KIND, reading["artifact_id"])
    accounting_ref = context.artifact_ref(
        PERLECTOR, PAGE_ACCOUNTING_KIND, accounting["artifact_id"]
    )
    shown = page_reask.render_reask(reading["payload"]["answer"], named)
    request = _Request(
        REASK_READING,
        PAGE_REASK_PASS,
        named=page_reask.named_ids(named),
        reask=page_path.reask_record(
            reading_ref=reading_ref,
            accounting_ref=accounting_ref,
            shown=shown,
            budget=state.reask_budget,
            serving_recipe=run.chair.serving_recipe,
            feed=page.feed,
        ),
        inputs=[reading_ref, accounting_ref],
    )
    request.adopted = _existing_reading(context, page.page_id, REASK_READING)
    if request.adopted is not None:
        return request
    request.text = page_path.reask_request_text(run.chair.serving_recipe, page.feed, shown)
    request.image_sha256s = page_path.request_image_sha256s(page.feed)
    if not state.live:
        request.fixture_row = page_path.fixture_reask_answer(context, page.ordinal, planned=True)
        return request
    _admit(
        state,
        request,
        lambda row: page_path.reask_request_capacity(
            row, run.chair.serving_recipe, page.feed, shown, request.text, run.page_generation
        ),
    )
    # A replay's re-ask the source never sent cannot be answered: it is recorded not asked.
    if state.replay is not None and _sends(state, page, request):
        request.not_replayed = _recorded(state, page, request) is None
    return request


def _finish_reask(state: _PagePass, page: _Page, result: dict[str, Any] | Exception | None) -> None:
    """Publish the re-ask's reading, the accounting of both readings, then every act record."""
    request = page.reask
    second = _reading(state, page, request, result)
    payload = second["payload"]
    plans = (
        page_path.entry_plans(
            payload["answer"],
            page.feed,
            page_id=page.page_id,
            stop_reason=payload["stop_reason"],
            truncation_policy=state.run.protocol_config["truncation"],
            accounting_policy=state.accounting_policy,
            attempt=REASK_READING,
            named=request.named,
            first_count=len(page.plans),
        )
        if payload["disposition"] == READ
        else []
    )
    accounting = publish_page_accounting(
        state,
        page,
        page.reading,
        page.plans,
        reask={
            "reading": payload,
            "reading_ref": state.context.artifact_ref(
                PERLECTOR, PAGE_READING_KIND, second["artifact_id"]
            ),
            "plans": plans,
            "named": request.named,
        },
    )
    # A re-ask the accounting does not count adds no act, and the page stands on
    # its first reading; one it counts adds exactly the entries it measured.
    counted = page_path.reask_act_plans(
        accounting["payload"], page.plans, plans, f"page {page.page_id}"
    )
    # The page's doubt is the first reading's and the counted re-ask's together.
    page_path.hold_doubtful_page(counted, state.accounting_policy)
    recovered = counted[len(page.plans) :]
    publish_act_records(state, page, page.reading, page.plans, accounting)
    publish_act_records(state, page, second, recovered, accounting)
    page.counted = page.plans + recovered


def _reading(state: _PagePass, page: _Page, request: _Request, result) -> dict[str, Any]:
    """The request's `page-reading`: the one already sealed, checked, or the one published now."""
    if request.adopted is not None:
        _check_adopted(state, page, request, request.adopted)
        return request.adopted
    return _publish_reading(state, page, request, result)


def _publish_reading(state: _PagePass, page: _Page, request: _Request, result) -> dict[str, Any]:
    run, context = state.run, state.context
    not_run = page.not_run if page_path.is_whole_page_reading(request.ordinal) else []
    attempted = not not_run and request.refusal is None and not request.not_replayed
    engine_call = capacity = failure = answer = None
    repairs: list[dict[str, Any]] = []
    finish_reason = stop_reason = None
    inputs: list[dict[str, str] | None] = [
        page.feed_ref,
        context.artifact_ref(EXEMPLAR, "page", page.page_record["artifact_id"]),
        *request.inputs,
    ]
    receipt_ref = None
    if not_run:
        parse_state, problems = NOT_RUN, [dict(problem) for problem in not_run]
    elif request.not_replayed:
        parse_state, problems = NOT_RUN, [replay.not_replayed_problem(context.run)]
    elif request.refusal is not None:
        parse_state = REFUSED_CAPACITY
        capacity = {
            "capacity": request.refusal.capacity,
            "answer_reserve": None,
            "max_tokens": None,
        }
        problems = [{"code": REFUSED_CAPACITY, "detail": str(request.refusal)}]
    elif not state.live:
        row = request.fixture_row
        finish_reason = stop_reason = row.get("stop_reason", "stop")
        parse_state, answer, problems, repairs = page_path.read_reply(
            row["answer"], stop_reason, page.feed, state.accounting_policy, request.named
        )
    else:
        receipt_ref = run.receipt_ref
        capacity = request.capacity
        inputs += live_calls.sent_refs(
            context, page.page_id, page_key(page.ordinal), request.ordinal, request.pass_name
        )
        if isinstance(result, Exception):
            failure = live_calls.failure_record(result, phase=request.pass_name)
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
            inputs += live_calls.engine_call_inputs(context, engine_call)
            parse_state, answer, problems, repairs = page_path.read_reply(
                result["content"], stop_reason, page.feed, state.accounting_policy, request.named
            )
    disposition = READ if parse_state == PARSED and not problems else HELD
    payload = {
        "schema": PAGE_READING_SCHEMA,
        "page_id": page.page_id,
        "page_ordinal": page.ordinal,
        "attempt_ordinal": request.ordinal,
        "feed_ref": page.feed_ref,
        "request_digest": (
            page_path.request_digest(request.text, request.image_sha256s) if attempted else None
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
        "reask": request.reask,
        **({} if request.reread is None else {OPERATOR_REREAD_FIELD: request.reread}),
        **({ANSWER_REPAIRS_FIELD: repairs} if repairs else {}),
        "audit": state.audit,
        "provenance": provenance_for(
            context, run.chair, attempted=attempted, receipt_ref=receipt_ref
        ),
    }
    attempt = page_path.page_reading_attempt(page.page_id, request.ordinal)
    context.publish(
        kind=PAGE_READING_KIND,
        subject_id=page.page_id,
        outcome=page_path.reading_outcome(parse_state, disposition),
        attempt=attempt,
        inputs=page_path.distinct_refs(inputs),
        payload=payload,
    )
    return context.tree.read_artifact(
        PERLECTOR, PAGE_READING_KIND, _artifact(PAGE_READING_KIND, page.page_id, attempt)
    )


def _check_adopted(
    state: _PagePass, page: _Page, request: _Request, record: dict[str, Any]
) -> None:
    """Refuse a retained page reading this run could not have made from this page's feed.

    A re-ask is adopted only when it names exactly the re-ask this page's
    first reading plans now -- its trigger records, named ids, prior
    entries, budget and prompt -- and is refused by name otherwise.
    """
    payload = record["payload"]
    schema = payload.get("schema") if isinstance(payload, dict) else None
    if (
        schema != PAGE_READING_SCHEMA
        or payload.get("feed_ref") != page.feed_ref
        or payload.get("attempt_ordinal") != request.ordinal
        or payload.get("disposition") not in (READ, HELD)
    ):
        raise ContractError(
            f"page {page.page_id}'s retained page reading (schema {schema!r}) was made from "
            "another feed or configuration than this page has now, or under another schema "
            f"than {PAGE_READING_SCHEMA}; it is not adopted and the page is not asked again. "
            "Read this page in a new run"
        )
    if payload.get(OPERATOR_REREAD_FIELD) != request.reread:
        raise FatalAccounting(
            f"page {page.page_id}'s retained page reading answers other operator decisions, or "
            "supersedes other readings, than this page's re-read names now; it is not adopted. "
            "Read this page in a new run"
        )
    if payload.get("reask") != request.reask:
        raise FatalAccounting(
            f"page {page.page_id}'s retained page reading names another re-ask (its trigger "
            "records, named ids, prior entries, budget or prompt) than its first reading "
            "plans now; it is not adopted and the page is not asked again. Read this page in "
            "a new run"
        )
    if payload.get("engine_call") is not None:
        # The retained call record is held to the sealed row it was sent under.
        live_calls.engine_call_inputs(state.context, payload["engine_call"])
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

    Every record carries `page_accounting_ref` -- the page's last accounting,
    the re-ask's on a re-asked page -- and the page's hold codes as
    `page_holds`, and is held when those or its own holds are non-empty.
    Each record's `n` is the number the accounting names the entry by; an
    entry the re-ask read carries `reading_attempt: 2` and `reading_n`, its
    number in the re-ask's answer, on both records. A
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
    engine_inputs = live_calls.engine_call_inputs(context, payload["engine_call"])
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
            "n": plan["n"],
            "kind": act["kind"],
            "label": act.get("label"),
            "cites": list(act["cites"]),
            "cited_ids": list(plan["cited_ids"]),
            "act_class": plan["act_class"],
            "page_reading_attempt": page_path.page_reading_attempt(
                page.page_id, plan["reading_attempt"]
            ),
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
            **page_path.recovered_fields(plan),
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
    state: _PagePass,
    page: _Page,
    reading: dict[str, Any],
    plans: list[dict[str, Any]],
    reask: dict[str, Any] | None = None,
    attempt: int = FIRST_READING,
) -> dict[str, Any]:
    """The page's `page-accounting`: rules a-j over its reading, witnesses and detections.

    `reading` and `plans` are the page's first reading, or with `attempt` an
    operator re-read's, accounted alone and bound to its attempt; with `reask`
    (`{reading, reading_ref, plans, named}`, its re-ask's) the accounting is
    the re-ask's, measuring both readings together, bound to the re-ask's
    attempt. Published before any act record, which names it. A resumed pass
    adopts the one already sealed for the reading when it was measured from
    exactly these inputs under this policy, and refuses by name when any
    input differs; a re-ask's is measured again and adopted only when it is
    exactly what the two readings give, since it restates the first
    reading's entries.
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
        fixture_placeholders=not is_real_ingress(context.run),
        reask=reask,
        attempt=attempt,
    )
    # Bound to the reading's attempt: each reading of a page is accounted apart.
    ordinal = attempt if reask is None else REASK_READING
    attempt = page_path.page_reading_attempt(page.page_id, ordinal)
    sealed = _sealed(context, PAGE_ACCOUNTING_KIND, page.page_id, attempt)
    if sealed is not None:
        payload = sealed["payload"]
        if (
            page_path.refs_by_path(sealed["inputs"]) != page_path.refs_by_path(inputs)
            or not isinstance(payload, dict)
            or payload.get("schema") != page_accounting.SCHEMA
            or payload.get("feed_ref") != page.feed_ref
            or payload.get("page_reading_ref") != measured["page_reading_ref"]
            or payload.get("policy_sha256") != state.accounting_policy.sha256
        ):
            raise ContractError(
                f"page {page.page_id}'s retained page accounting was measured from other inputs "
                "or under another policy than this page has now; it is not adopted. Read this "
                "page in a new run"
            )
        if reask is not None and payload != page_accounting.page_accounting(
            **measured, policy=state.accounting_policy
        ):
            raise FatalAccounting(
                f"page {page.page_id}'s retained re-ask accounting is not what its two readings "
                "give (its entries, findings or holds differ); it is not adopted. Read this page "
                "in a new run"
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


def _first_requests(page: _Page) -> list[_Request]:
    """The page's first reading request."""
    return [page.first]


def _reask_requests(page: _Page) -> list[_Request]:
    """The page's re-ask request, if it has one."""
    return [] if page.reask is None else [page.reask]


def _reread_requests(page: _Page) -> list[_Request]:
    """Every operator re-read request of the page, each of which the window sends."""
    return list(page.rereads)


def _left_to_send(
    state: _PagePass, prepared: list[_Page], select: Callable[[_Page], list[_Request]]
) -> int:
    """Count the requests this live pass will send, refusing any it cannot resume.

    `select` gives a page's requests of this phase. Only a request it sends is
    counted: not one already read, not one it does not ask, and not one over
    the row's capacity. A request with sends and no reading is sent again only
    when no retained reply could be its answer.
    """
    context = state.context
    left, unrecorded, replies = 0, [], None
    chosen = [(page, request) for page in prepared for request in select(page)]
    for page, request in chosen:
        if not _sends(state, page, request):
            continue
        markers = live_calls.sent_records(
            context, page.page_id, page_key(page.ordinal), request.ordinal, request.pass_name
        )
        if markers:
            replies = live_calls.unrecorded_replies(context) if replies is None else replies
            calls, unattributed = replies
            if unattributed or live_calls.answers_a_send(calls, markers):
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


def _startup_left(state: _PagePass) -> int:
    """The seconds the chair's start may still take: its whole timeout when not begun,
    what is left of it while it runs in the background, none once it is up."""
    timeout = _serving_row(state).startup_timeout_seconds
    if state.starting is not None and not state.starting.done:
        return max(0, timeout - state.starting.elapsed_seconds())
    return timeout if state.run.service.client is None else 0


def _refuse_past_phase_deadline(state: _PagePass, left: int, what: str) -> None:
    """The deadline check before a phase's window: its sends, and the chair's start if due."""
    startup = _startup_left(state)
    _refuse_past_page_deadline(
        state,
        startup + left * planned_seconds_per_page(state.run.page_max_tokens),
        f"starting the Perlector ({startup}s) and {what} {left} pages",
    )


def _starts_early(state: _PagePass, pages: dict[int, str]) -> bool:
    """Whether the chair may start while pages are prepared: some page has no sealed
    first reading, none was sent by an interrupted pass, and the reading deadline
    admits the start and every such page, read from the records alone.

    It is decided before any feed is built, so a page the pass will not send after
    all (refused by the Exemplar, over capacity) still counts: at worst the chair
    starts for a pass that sends nothing, and is stopped with it. Otherwise the chair
    starts lazily, as before: a resumed pass with an earlier send waits for
    `_left_to_send`, which may refuse it, and a tight deadline is judged on the exact
    pages to send, which may refuse the pass before anything starts.
    """
    unread = [
        (ordinal, page_id)
        for ordinal, page_id in pages.items()
        if page_path.page_reading_attempt(page_id, FIRST_READING)
        not in state.sealed.get(page_id, _Sealed()).readings
    ]
    if not unread or any(
        live_calls.sent_records(
            state.context, page_id, page_key(ordinal), FIRST_READING, PAGE_READING_PASS
        )
        for ordinal, page_id in unread
    ):
        return False
    deadline = state.run.args.reading_deadline
    if deadline is None:
        return True
    needed = _serving_row(state).startup_timeout_seconds + len(unread) * planned_seconds_per_page(
        state.run.page_max_tokens
    )
    return (deadline - datetime.now(timezone.utc)).total_seconds() >= needed


def _prepare_all(state: _PagePass, pages: dict[int, str]) -> list[_Page]:
    """Prepare every page, starting a live chair in the background meanwhile.

    The chair loads while feeds are built, so its cold start overlaps the
    preparation instead of following it. It starts only when a page is unread and
    the deadline admits its start and every unread page; the start is joined before this
    returns, so the first window finds it up, and a failure on either thread is
    raised here. A preparation that fails does not wait for the start.
    """
    run = state.run
    if state.live and state.chair_present and _starts_early(state, pages):
        # On the resident chair too, so `close` hands a still-starting chair to its
        # thread rather than waiting for it or stopping it mid-start.
        state.starting = run.service.starting = live_calls.BackgroundStart(run)
    try:
        prepared = [_prepare(state, ordinal, page_id) for ordinal, page_id in pages.items()]
        if state.live and state.chair_present:
            left = _left_to_send(state, prepared, _first_requests)
            if left:
                _refuse_past_phase_deadline(state, left, "reading")
    except BaseException as raised:
        # Not waited for: `ResidentChair.close` leaves a chair still starting to its
        # thread, which stops it once its start returns.
        if state.starting is not None:
            state.starting.note_failure(raised)
        raise
    if state.starting is not None:
        state.starting.join()
    return prepared


def _joins_early(state: _PagePass, page: _Page) -> bool:
    """Whether a page's planned re-ask may join the first readings' window.

    Only one no earlier pass sent: deciding whether a retained reply could answer an
    earlier send (`_left_to_send`) reads every reply the run holds, and while this
    pass's own calls are out their replies would count as unaccounted for. Those
    re-asks wait for the first readings to finish and are judged then, as before.
    """
    return not state.live or not live_calls.sent_records(
        state.context, page.page_id, page_key(page.ordinal), REASK_READING, PAGE_REASK_PASS
    )


def _deadline_admits(state: _PagePass, sends: int) -> bool:
    """Whether the reading deadline holds the chair's start, if still due, and `sends`
    more calls at the planned rate; always, with no deadline."""
    deadline = state.run.args.reading_deadline
    if deadline is None:
        return True
    needed = _startup_left(state) + sends * planned_seconds_per_page(state.run.page_max_tokens)
    return (deadline - datetime.now(timezone.utc)).total_seconds() >= needed


def _first_and_early_reask_jobs(state: _PagePass, prepared: list[_Page], joined: set[str]):
    """Every page's first reading in page order, each re-ask joining as soon as it is planned.

    A page's re-ask is planned by its first reading's finish, so it is drawn only
    after that reading is published, before the next first reading is drawn. The
    window still finishes jobs strictly in the order drawn. A re-ask that would be
    sent joins only when the reading deadline holds it together with every call
    this window has still to finish: each first reading not yet finished and each
    re-ask already joined. A re-ask planned after the last first reading was
    drawn, one `_joins_early` keeps back, or one the deadline does not hold here is
    left for the re-ask window, which judges it against the deadline as before;
    `joined` names the pages whose re-ask was drawn here.
    """
    seen = 0
    # The re-asks this window sends whose replies are not yet finished.
    reasks_out: set[str] = set()

    def calls_left() -> int:
        firsts = sum(
            1 for page in prepared if not page.first_finished and _sends(state, page, page.first)
        )
        return firsts + len(reasks_out)

    def joins(page: _Page) -> bool:
        if page.reask is None or not _joins_early(state, page):
            return False
        return not _sends(state, page, page.reask) or _deadline_admits(state, calls_left() + 1)

    def planned_since():
        nonlocal seen
        # Pages are finished in page order, so their re-asks are planned in it too.
        while seen < len(prepared) and prepared[seen].first_finished:
            page = prepared[seen]
            seen += 1
            if joins(page):
                joined.add(page.page_id)
                if _sends(state, page, page.reask):
                    reasks_out.add(page.page_id)
                yield _job(state, page, page.reask, partial(finish_reask, page))

    def finish_first(page: _Page, result) -> None:
        _finish(state, page, result)
        page.first_finished = True

    def finish_reask(page: _Page, result) -> None:
        reasks_out.discard(page.page_id)
        _finish_reask(state, page, result)

    for page in prepared:
        yield from planned_since()
        yield _job(state, page, page.first, partial(finish_first, page))
    yield from planned_since()


def read_the_pages(run) -> None:
    """Read every sealed Exemplar page once, re-ask the planned ones, then read again
    each page a person asked for.

    A page's re-ask joins the first readings' window as soon as its first reading is
    published, so the card is not left idle between the phases; a page's attempt-2
    records always follow its own first reading, and may come before later pages'
    first readings. Nothing reads the records in the order written: each names its
    inputs, and every reader orders by page.
    """
    context = run.context
    pages = exemplar_page_ids(context)
    if not pages:
        raise ContractError("the Exemplar sealed no page, so the page path has nothing to read")
    state = _PagePass(
        run=run,
        audit=audit_not_run(run.audit_policy, run.audit_sha256),
        page_chairs=declared_page_witness_chairs(context),
        testimonia=current_page_testimonia(context),
        surya=page_path.sealed_surya_census(
            context, stage_manifest(context, DESIGNATOR)["artifacts"]
        ),
        accounting_policy=page_accounting.require_page_accounting_policy(
            context, context.page_accounting_config_path
        ),
        reask_budget=page_reask.reask_budget(context.recovery_policy),
        sealed=_sealed_pages(context),
    )
    if run.serving_mode == "live" and replay.replay_of(context.run) is not None:
        # A replay's live chair answers from its source run, which says what it recorded.
        state.replay = replay.open_source(context.run)
    prepared = _prepare_all(state, pages)
    print(f"perlector: reading {len(pages)} pages whole", file=sys.stderr)
    joined: set[str] = set()
    live_calls.in_order_window(
        run.concurrency, _first_and_early_reask_jobs(state, prepared, joined)
    )
    planned = [page for page in prepared if page.reask is not None and page.page_id not in joined]
    if planned:
        if state.live:
            left = _left_to_send(state, planned, _reask_requests)
            if left:
                _refuse_past_phase_deadline(state, left, "re-asking")
        print(f"perlector: re-asking {len(planned)} pages", file=sys.stderr)
        live_calls.in_order_window(
            run.concurrency,
            (
                _job(state, page, page.reask, partial(_finish_reask, state, page))
                for page in planned
            ),
        )
    _plan_rereads(state, prepared)
    reread = [page for page in prepared if page.rereads]
    if not reread:
        return
    if state.live:
        left = _left_to_send(state, reread, _reread_requests)
        if left:
            _refuse_past_phase_deadline(state, left, "re-reading")
    print(f"perlector: reading {len(reread)} pages again as a person asked", file=sys.stderr)
    live_calls.in_order_window(
        run.concurrency,
        (
            _job(state, page, request, partial(_finish_reread, state, page, request))
            for page in reread
            for request in _reread_requests(page)
        ),
    )
