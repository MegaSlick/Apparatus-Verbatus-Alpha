"""The Coniector's records: their closed shapes, how they are derived, and how they are verified.

The Coniector (`pipeline/4b_coniector`) publishes, in order:

    reconstruction-plan  (subject "coniector")  the sealed switches and every call asked
    reconstruction-call  (subject page_id)      one per call: the reply as given, parsed
    reconstruction       (subject act_id)       one per subject act and one per join

A pass over readings an operator re-read changed (`common.page_path`, "an
operator re-read") cannot file new records under the identities of the old:
it publishes the new plan as the next generation, superseding the last
(`supersedes`, its reference), and each call or reconstruction that differs
from every one sealed for its page or subject as that subject's next
generation (`generation_attempt`). The current plan is the last of the chain;
the current call of a page and reconstruction of a subject are the ones the
current plan and readings give. The rest stay as made, superseded.

A reconstruction is labelled, unconfirmed and never established: it is not an
act, is counted in no denominator, and no stage but the Armarium reads it. One
that could not be made leaves the diplomatic reading delivered as it is, and
says why (`not_made`).

Everything a record derives is derived here, once, so the stage that writes it
and the Armarium that reads it cannot derive it two ways: the diplomatic
entries (`diplomatic_entries`), each call's reconstructions from its reply
(`derive_reconstructions`), and the verification that recomputes every record
from the Perlector's sealed readings and the reply retained for each call
(`verified_reconstructions`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from itertools import pairwise
from typing import Any, Final

from common import page_path
from common.chair_wire import chat_template_kwargs_for
from common.chairs.models import ChairIdentity
from common.contracts.canonical import digest_bytes, text_sha256
from common.contracts.errors import ContractError, FatalAccounting
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.stages import CONIECTOR, PERLECTOR
from common.contracts.uncertainty import from_page_perlectio
from common.decoding import chair_decoding
from common.reading_annotations import (
    ASSESSMENT_ASSESSED,
    read_doubt_marks,
    render_doubt_marks,
)
from common.reconstruction import (
    ReconstructionPolicy,
    apply_departures,
    apply_join,
    load_reconstruction_policy,
    reconstruction_plan,
)
from common.reconstruction_answer import PARSED, parse_reconstruction_answer
from common.reconstruction_prompt import (
    PROMPT_VERSION,
    build_reconstruction_prompt,
    shown_keys,
    shown_texts,
)
from common.replay import not_replayed_problem, replay_of
from common.stage import (
    RECONSTRUCTOR_CHAIR,
    real_page_entries,
    sealed_decoding_policy,
    serving_reader,
    verify_retained_call_sampling,
)

PLAN_KIND: Final = "reconstruction-plan"
PLAN_SUBJECT: Final = "coniector"
CALL_KIND: Final = "reconstruction-call"
RECONSTRUCTION_KIND: Final = "reconstruction"
CONIECTOR_KINDS: Final = frozenset({PLAN_KIND, CALL_KIND, RECONSTRUCTION_KIND})

# How a later generation of each record is named, and the field a later plan
# names the plan it supersedes in.
GENERATION_OPERATIONS: Final = {
    PLAN_KIND: "replan",
    CALL_KIND: "recall",
    RECONSTRUCTION_KIND: "remake",
}
SUPERSEDES_FIELD: Final = "supersedes"

PLAN_SCHEMA: Final = "coniector-plan.v2"
CALL_SCHEMA: Final = "coniector-call.v1"
RECONSTRUCTION_SCHEMA: Final = "coniector-reconstruction.v1"

UNIT_ACT: Final = "act"
UNIT_JOIN: Final = "join"
MAKER_MODEL: Final = "model"
MAKER_PERSON: Final = "person"

SERVING_FIXTURE: Final = "fixture"
SERVING_LIVE: Final = "live"

LABEL: Final = (
    "reconstruction: unconfirmed, proposed from the text around the act, never from the "
    "page image; the diplomatic reading is the established text"
)

# Why a whole call's reconstructions were not made; each act's own reasons are
# `common.reconstruction.NOT_MADE_CODES`.
CHAIR_ABSENT: Final = "chair-absent"
REQUEST_OVER_CAPACITY: Final = "request-over-capacity"
CALL_FAILED: Final = "call-failed"
REPLY_CUT_OFF: Final = "reply-cut-off"
REPLY_MALFORMED: Final = "reply-malformed"
REPLY_ANSWER_INVALID: Final = "reply-answer-invalid"
# A replay's call its source run never sent (`common.replay`).
NOT_REPLAYED: Final = "not-replayed"
NOT_ASKED_CODES: Final = frozenset({CHAIR_ABSENT, REQUEST_OVER_CAPACITY, CALL_FAILED, NOT_REPLAYED})
# A join the Coniector says does not continue: nothing to reconstruct.
DOES_NOT_CONTINUE: Final = "does-not-continue"

# The call's parse state beyond the grammar's own: no reply to parse.
NOT_ASKED: Final = "not-asked"

PLAN_FIELDS: Final = frozenset(
    {
        "schema",
        "mode",
        "pages_are_consecutive",
        "policy_sha256",
        "calls",
    }
)
CALL_FIELDS: Final = frozenset(
    {
        "schema",
        "page_id",
        "page_ordinal",
        "call",
        "prompt_version",
        "prompt_sha256",
        "shown",
        "serving_mode",
        "capacity",
        "engine_call",
        "failure",
        "reply_text",
        "finish_reason",
        "stop_reason",
        "parse_state",
        "problems",
        "maker",
    }
)
MAKER_FIELDS: Final = frozenset(
    {"kind", "chair", "chair_state", "resolved_identity", "resolved_revision", "receipt_ref"}
)


# --- the diplomatic entries ------------------------------------------------------------


def diplomatic_entries(
    context, rows: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """`(plan entries, shown entries by act_key)` of a page-read run's readings.

    `rows` are `common.stage.reading_acts(context)`. Only the entries of a read
    answer have a reading (classes `reading` and `reading-unplaced`); each is
    read through its Perlectio, and its diplomatic text is that reading with its
    doubt marks rendered back (`render_doubt_marks`), exactly what the Perlector
    returned. A plan entry carries what `reconstruction_plan` reads; a shown
    entry carries what the prompt shows, with the entry's `act_id`. A canary
    page's readings are never entries: no reconstruction is made over one, and
    none is shown as another page's context or chain piece.
    """
    plan_entries: list[dict[str, Any]] = []
    shown: dict[str, dict[str, Any]] = {}
    for row in real_page_entries(context.run, rows):
        if row["class"] not in (page_path.READING_CLASS, page_path.UNPLACED_CLASS):
            continue
        if row["act_key"] in shown:
            raise FatalAccounting(f"two readings of this run are keyed {row['act_key']}")
        record = context.tree.read_artifact_reference(
            row["perlectio_ref"],
            stage=PERLECTOR,
            kind=page_path.PERLECTIO_KIND,
            subject_id=row["act_id"],
        )
        payload = record["payload"]
        from_page_perlectio(payload)
        assessment = payload["uncertainty_assessment"]
        raw = render_doubt_marks(payload["text"], assessment)
        if (
            assessment["state"] == ASSESSMENT_ASSESSED
            and read_doubt_marks(raw)[0] != payload["text"]
        ):
            raise FatalAccounting(
                f"the reading of {row['act_key']} does not render back to the text it was read as"
            )
        plan_entries.append(
            {
                "act_key": row["act_key"],
                "act_id": row["act_id"],
                "page_id": row["page_id"],
                "page_ordinal": row["page_ordinal"],
                "n": row["n"],
                "kind": row["kind"],
                "continues_from_previous_page": row["continues_from_previous_page"],
                "continues_to_next_page": row["continues_to_next_page"],
                "reading_attempt": row["reading_attempt"],
            }
        )
        shown[row["act_key"]] = {
            "act_key": row["act_key"],
            "act_id": row["act_id"],
            "page_id": row["page_id"],
            "page_ordinal": row["page_ordinal"],
            "n": row["n"],
            "kind": row["kind"],
            "label": payload.get("label"),
            "text": raw,
            "clean_text": payload["text"],
            "perlectio_ref": dict(row["perlectio_ref"]),
        }
    return plan_entries, shown


def plan_payload(
    policy: ReconstructionPolicy, plan_entries: list[dict[str, Any]]
) -> dict[str, Any]:
    """The `reconstruction-plan` payload: the switches and every call."""
    return {
        "schema": PLAN_SCHEMA,
        "mode": policy.mode,
        "pages_are_consecutive": policy.pages_are_consecutive,
        "policy_sha256": policy.sha256,
        "calls": reconstruction_plan(
            plan_entries, mode=policy.mode, pages_are_consecutive=policy.pages_are_consecutive
        ),
    }


def call_page_id(call: Mapping[str, Any], shown: Mapping[str, Mapping[str, Any]]) -> str:
    """The page a call reconstructs, named by its page id."""
    ordinal = call["page_ordinal"]
    pages = {entry["page_id"] for entry in shown.values() if entry["page_ordinal"] == ordinal}
    if len(pages) != 1:
        raise FatalAccounting(f"reconstruction call on page {ordinal} names {len(pages)} pages")
    return pages.pop()


def call_prompt(
    call: Mapping[str, Any], shown: Mapping[str, Mapping[str, Any]], policy: ReconstructionPolicy
) -> str:
    return build_reconstruction_prompt(call, shown, policy)


# --- one call's reconstructions --------------------------------------------------------


def reply_state(
    reply_text: str | None, stop_reason: str | None, call: Mapping[str, Any]
) -> tuple[str, dict[str, Any] | None, list[dict[str, str]]]:
    """`(parse_state, answer | None, problems)` of a reply the engine finished or was cut on.

    A reply cut at the output cap is never read, even when what arrived parses.
    """
    if reply_text is None:
        return NOT_ASKED, None, []
    if stop_reason == "length":
        return (
            REPLY_CUT_OFF,
            None,
            [{"code": REPLY_CUT_OFF, "detail": "the reply stopped at the output cap"}],
        )
    return parse_reconstruction_answer(reply_text, call)


def call_not_made(parse_state: str, problems: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Why a call that has no usable answer leaves each of its reconstructions not made."""
    if parse_state == NOT_ASKED:
        (problem,) = problems
        return {"code": problem["code"], "detail": problem["detail"]}
    code = {REPLY_CUT_OFF: REPLY_CUT_OFF, "malformed": REPLY_MALFORMED}.get(
        parse_state, REPLY_ANSWER_INVALID
    )
    detail = "; ".join(f"{problem['code']}: {problem['detail']}" for problem in problems)
    return {"code": code, "detail": detail or parse_state}


def _clean(text: str | None) -> tuple[str | None, dict[str, Any] | None]:
    if text is None:
        return None, None
    return read_doubt_marks(text)


def _record(
    *,
    unit: str,
    keys: Sequence[str],
    shown: Mapping[str, Mapping[str, Any]],
    page_ordinal: int,
    base_raw: str,
    text: str | None,
    applied: list[dict[str, Any]],
    not_made: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    continues: bool | None,
    call_ref: Mapping[str, str],
    maker: Mapping[str, Any],
) -> dict[str, Any]:
    clean, uncertainty = _clean(text)
    return {
        "schema": RECONSTRUCTION_SCHEMA,
        "unit": unit,
        "act_ids": [shown[key]["act_id"] for key in keys],
        "act_keys": list(keys),
        "page_ordinal": page_ordinal,
        "call_ref": dict(call_ref),
        "label": LABEL,
        "made": text is not None,
        "maker": dict(maker),
        "diplomatic_raw_sha256": text_sha256(base_raw),
        "diplomatic_clean_sha256s": [text_sha256(shown[key]["clean_text"]) for key in keys],
        "reconstruction_raw": text,
        "reconstruction_text": clean,
        "reconstruction_uncertainty": uncertainty,
        "continues": continues,
        "departures": applied,
        "findings": [dict(finding) for finding in findings],
        "not_made": [dict(reason) for reason in not_made],
    }


def derive_reconstructions(
    call: Mapping[str, Any],
    shown: Mapping[str, Mapping[str, Any]],
    *,
    parse_state: str,
    answer: Mapping[str, Any] | None,
    problems: Sequence[Mapping[str, Any]],
    policy: ReconstructionPolicy,
    call_ref: Mapping[str, str],
    maker: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Every `reconstruction` payload one call gives: its subjects in order, then its joins.

    With no parsed answer, each is not made for the call's reason. With one,
    each act's departures and each join's are applied to the diplomatic text
    (`common.reconstruction`); one that cannot be applied leaves only its own
    act, or join, not made.
    """
    page_ordinal = call["page_ordinal"]
    texts = shown_texts(call, shown)
    records: list[dict[str, Any]] = []
    shared = {"shown": shown, "page_ordinal": page_ordinal, "call_ref": call_ref, "maker": maker}
    if parse_state != PARSED or answer is None:
        reason = call_not_made(parse_state, problems)
        for key in call["subjects"]:
            records.append(
                _record(
                    unit=UNIT_ACT,
                    keys=[key],
                    base_raw=shown[key]["text"],
                    text=None,
                    applied=[],
                    not_made=[reason],
                    findings=[],
                    continues=None,
                    **shared,
                )
            )
        for chain in call["chains"]:
            records.append(
                _record(
                    unit=UNIT_JOIN,
                    keys=chain,
                    base_raw="\n".join(shown[key]["text"] for key in chain),
                    text=None,
                    applied=[],
                    not_made=[reason],
                    findings=[],
                    continues=None,
                    **shared,
                )
            )
        return records
    for item in answer["acts"]:
        key = item["act"]
        base = shown[key]["text"]
        text, applied, not_made = apply_departures(base, item["departures"], texts, policy)
        records.append(
            _record(
                unit=UNIT_ACT,
                keys=[key],
                base_raw=base,
                text=text,
                applied=applied,
                not_made=not_made,
                findings=item["findings"],
                continues=None,
                **shared,
            )
        )
    for join in answer["joins"]:
        chain = join["acts"]
        pieces = [shown[key]["text"] for key in chain]
        text, applied, not_made = apply_join(pieces, join, texts, policy)
        if text is None and not not_made:
            not_made = [
                {
                    "code": DOES_NOT_CONTINUE,
                    "departure": None,
                    "detail": "the Coniector read the pieces as not one act",
                }
            ]
        records.append(
            _record(
                unit=UNIT_JOIN,
                keys=chain,
                base_raw="\n".join(pieces),
                text=text,
                applied=applied,
                not_made=not_made,
                findings=[],
                continues=join["continues"],
                **shared,
            )
        )
    return records


def reconstruction_subject(record: Mapping[str, Any]) -> str:
    """An act's reconstruction is filed under the act; a join's under its first piece,
    which is never a subject of its own."""
    return record["act_ids"][0]


def reconstruction_outcome(record: Mapping[str, Any]) -> str:
    return "made" if record["made"] else "not-made"


def call_outcome(parse_state: str) -> str:
    return "answered" if parse_state == PARSED else "not-answered"


# --- generations ---------------------------------------------------------------------------


def generation_attempt(kind: str, subject: str, generation: int) -> str | None:
    """The attempt a record's `generation` is filed under: none for the first, then numbered."""
    return None if generation == 1 else attempt_id(subject, GENERATION_OPERATIONS[kind], generation)


def sealed_generations(tree, kind: str, subject: str) -> list[dict[str, Any]]:
    """Every generation of one subject's record of `kind`, in order, as sealed."""
    found = []
    while True:
        identifier = artifact_id(
            CONIECTOR, kind, subject, generation_attempt(kind, subject, len(found) + 1)
        )
        if not tree.has_artifact(CONIECTOR, kind, identifier):
            return found
        found.append(tree.read_artifact(CONIECTOR, kind, identifier))


def plan_chain(context) -> list[dict[str, Any]]:
    """The sealed plans in order, each later one superseding the one before it."""
    chain = sealed_generations(context.tree, PLAN_KIND, PLAN_SUBJECT)
    for earlier, later in pairwise(chain):
        if later["payload"].get(SUPERSEDES_FIELD) != context.artifact_ref(
            CONIECTOR, PLAN_KIND, earlier["artifact_id"]
        ):
            raise FatalAccounting(
                "a later reconstruction plan does not supersede the one before it"
            )
    if chain and SUPERSEDES_FIELD in chain[0]["payload"]:
        raise FatalAccounting("the first reconstruction plan names a plan it supersedes")
    return chain


def plan_of(record: Mapping[str, Any]) -> dict[str, Any]:
    """A sealed plan's payload without the plan it supersedes: what the switches and readings give."""
    return {key: value for key, value in record["payload"].items() if key != SUPERSEDES_FIELD}


# --- the fixture's declared replies ------------------------------------------------------


def fixture_reply(context, call: Mapping[str, Any], pages_are_consecutive: bool) -> dict[str, Any]:
    """The synthetic fixture's reply to this call under this scenario.

    A scenario declares at most one `[[reconstruction_answer]]` per page and
    switch. Where it declares none, the reconstructor proposes nothing: every
    subject is answered with no finding and no departure, and every chain is
    one act, as the Recensor's agreed break says.
    """
    page_ordinal = call["page_ordinal"]
    rows = [
        row
        for row in context.fixture.get("reconstruction_answer", [])
        if row.get("scenario") == context.scenario
        and row.get("page_ordinal") == page_ordinal
        and row.get("pages_are_consecutive") is pages_are_consecutive
    ]
    if not rows:
        answer = {
            "acts": [{"act": act, "findings": [], "departures": []} for act in call["subjects"]],
            "joins": [
                {"acts": list(chain), "continues": True, "departures": []}
                for chain in call["chains"]
            ],
        }
        return {"content": json.dumps(answer), "stop_reason": "stop"}
    if len(rows) != 1:
        raise ContractError(
            f"the fixture declares {len(rows)} reconstruction answers for scenario "
            f"{context.scenario!r}, page {page_ordinal}, pages_are_consecutive = "
            f"{str(pages_are_consecutive).lower()}; a page reconstructed offline needs at most one"
        )
    row = rows[0]
    if not isinstance(row.get("answer"), str) or row.get("stop_reason", "stop") not in (
        "stop",
        "length",
    ):
        raise ContractError(
            f"the fixture's reconstruction answer for {context.scenario!r}, page {page_ordinal} "
            "is not an answer string with a stop reason of stop or length"
        )
    return {"content": row["answer"], "stop_reason": row.get("stop_reason", "stop")}


# --- verification ------------------------------------------------------------------------


def _closed(payload: Any, fields: frozenset[str], schema: str, what: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != fields or payload["schema"] != schema:
        raise FatalAccounting(f"{what} is not a {schema} record")
    return payload


def _live_request_is_this_prompt(context, payload: Mapping[str, Any], text: str, what: str) -> None:
    """The retained call record must be the reconstructor's text-only request for this prompt.

    Its wire body is rendered again as the serving client renders it, from this
    prompt, the admitted cap, the chair's sealed sampling row and the receipt's
    seed, and must digest to the request the engine was sent.
    """
    reader = serving_reader(context, what)
    record = json.loads(
        context.tree.read_bytes(payload["engine_call"]["call_record_ref"]["relative_path"])
    )
    verify_retained_call_sampling(context, record, RECONSTRUCTOR_CHAIR)
    if (
        record.get("chair") != RECONSTRUCTOR_CHAIR
        or record.get("image_sha256s") != []
        or record.get("receipt_ref") != payload["maker"]["receipt_ref"]
        or record.get("resolved_identity") != payload["maker"]["resolved_identity"]
        or record.get("served_model_id") != payload["engine_call"]["served_model_id"]
    ):
        raise FatalAccounting(f"{what} names a call record of another chair or request")
    policy, _digest = sealed_decoding_policy(context)
    seed = context.tree.read_run_receipt(dict(record["receipt_ref"])).get("seed")
    try:
        body = reader.request_bytes(
            {
                "chat_template_kwargs": chat_template_kwargs_for(RECONSTRUCTOR_CHAIR),
                "max_tokens": payload["capacity"]["max_tokens"],
                "messages": [{"role": "user", "content": text}],
            },
            model_id=record["served_model_id"],
            seed=seed,
            sampling=chair_decoding(policy, RECONSTRUCTOR_CHAIR),
        )
    except ContractError as error:
        raise FatalAccounting(f"{what}'s request cannot be rendered again: {error}") from error
    if digest_bytes(body) != record.get("request_sha256"):
        raise FatalAccounting(f"{what} was answered for a request other than this prompt")


def _reply_as_given(context, payload: Mapping[str, Any], text: str, what: str) -> None:
    """The reply the record holds must be the fixture's declared one or the retained one."""
    if payload["reply_text"] is None:
        return
    if payload["serving_mode"] == SERVING_FIXTURE:
        if payload["engine_call"] is not None or payload["capacity"] is not None:
            raise FatalAccounting(f"{what} is a fixture reply carrying live call evidence")
        declared = fixture_reply(
            context, payload["call"], _sealed_policy(context).pages_are_consecutive
        )
        if (declared["content"], declared["stop_reason"]) != (
            payload["reply_text"],
            payload["stop_reason"],
        ):
            raise FatalAccounting(f"{what} holds a reply the fixture never declared")
        return
    if payload["serving_mode"] != SERVING_LIVE or not isinstance(payload["engine_call"], dict):
        raise FatalAccounting(f"{what} names serving mode {payload['serving_mode']!r}")
    retained = page_path.retained_reply(
        context.tree.read_bytes, payload["engine_call"], serving_reader(context, what)
    )
    if (retained["content"], retained["finish_reason"], retained["stop_reason"]) != (
        payload["reply_text"],
        payload["finish_reason"],
        payload["stop_reason"],
    ):
        raise FatalAccounting(f"{what} holds a reply other than the one its engine returned")
    _live_request_is_this_prompt(context, payload, text, what)


def _sealed_policy(context) -> ReconstructionPolicy:
    policy = load_reconstruction_policy(context.args.reconstruction_config)
    context.require_sealed_config("reconstruction", policy.sha256)
    return policy


def expected_maker(identity: Any, receipt_ref: Mapping[str, str] | None) -> dict[str, Any]:
    """The maker a call's record names: the reconstructor chair as resolved, and its receipt."""
    if not isinstance(identity, ChairIdentity):
        return {
            "kind": MAKER_MODEL,
            "chair": RECONSTRUCTOR_CHAIR,
            "chair_state": "absent",
            "resolved_identity": None,
            "resolved_revision": None,
            "receipt_ref": None,
        }
    return {
        "kind": MAKER_MODEL,
        "chair": RECONSTRUCTOR_CHAIR,
        "chair_state": "configured",
        "resolved_identity": identity.to_record(),
        "resolved_revision": {
            "kind": identity.receipt_revision_kind,
            "value": identity.receipt_revision,
        },
        "receipt_ref": dict(receipt_ref) if receipt_ref is not None else None,
    }


def _require_maker(context, payload: Mapping[str, Any], what: str) -> None:
    """The maker is the roster's reconstructor, with a receipt exactly when it was asked."""
    maker = payload["maker"]
    identity = context.registry.resolve(RECONSTRUCTOR_CHAIR)
    receipt = maker.get("receipt_ref") if isinstance(maker, Mapping) else None
    if maker != expected_maker(identity, receipt):
        raise FatalAccounting(f"{what} names a maker other than the run's reconstructor chair")
    if (receipt is not None) != (payload["reply_text"] is not None):
        raise FatalAccounting(f"{what} carries a serving receipt exactly when it was not asked")
    if receipt is not None:
        if context.tree.read_run_receipt(dict(receipt))["chair"] != RECONSTRUCTOR_CHAIR:
            raise FatalAccounting(f"{what} names the receipt of another chair")


def _require_not_asked_evidence(context, payload: Mapping[str, Any], what: str) -> None:
    """A call not asked names one reason and exactly the evidence that reason leaves."""
    if payload["reply_text"] is not None:
        if (
            payload["failure"] is not None
            or payload["problems"]
            != (reply_state(payload["reply_text"], payload["stop_reason"], payload["call"])[2])
        ):
            raise FatalAccounting(f"{what} has a reply and a reason it was not asked")
        return
    problems = payload["problems"]
    if not isinstance(problems, list) or len(problems) != 1 or not isinstance(problems[0], dict):
        raise FatalAccounting(f"{what} has no reply and not exactly one reason it was not asked")
    code = problems[0].get("code")
    if code not in NOT_ASKED_CODES:
        raise FatalAccounting(f"{what} was not asked for a reason no call records ({code!r})")
    if any(payload[name] is not None for name in ("finish_reason", "stop_reason", "engine_call")):
        raise FatalAccounting(f"{what} was not asked but records an engine's answer")
    present = isinstance(context.registry.resolve(RECONSTRUCTOR_CHAIR), ChairIdentity)
    if (code == CHAIR_ABSENT) == present:
        raise FatalAccounting(f"{what} says whether its chair is absent against the roster")
    capacity, failure = payload["capacity"], payload["failure"]
    if code == CHAIR_ABSENT and (capacity is not None or failure is not None):
        raise FatalAccounting(f"{what} names an absent chair and a call")
    if code == NOT_REPLAYED and (
        replay_of(context.run) is None
        or capacity is not None
        or failure is not None
        or problems[0] != not_replayed_problem(context.run)
    ):
        raise FatalAccounting(
            f"{what} says its source run never sent it, but this run replays none, or it "
            "names a call"
        )
    if code == REQUEST_OVER_CAPACITY and (
        failure is not None
        or not isinstance(capacity, Mapping)
        or not isinstance(capacity.get("capacity"), Mapping)
        or capacity["capacity"].get("fits") is not False
    ):
        raise FatalAccounting(f"{what} was refused for capacity with no record that it did not fit")
    if code == CALL_FAILED:
        if not isinstance(failure, Mapping) or not isinstance(capacity, Mapping):
            raise FatalAccounting(f"{what} failed with no failure or admission recorded")
        for name in ("raw_response_ref", "call_record_ref"):
            if name in failure and context.input_ref(failure[name]["relative_path"]) != dict(
                failure[name]
            ):
                raise FatalAccounting(f"{what} names retained bytes that are not on disk as named")


def verified_reconstructions(context, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Every Coniector record, each recomputed and proven, for the Armarium to show.

    `rows` are the run's `reading_acts` rows.
    The plan is derived again from the Perlector's sealed readings under the
    sealed switches; each call's reply must be the one the fixture declared or
    the engine returned (read again from its retained bytes), and parse as
    recorded; each reconstruction must be exactly what that reply gives. A
    record the recomputation does not give, or one it gives that is missing, is
    refused. Returns `{"plan": payload, "acts": {act_id: payload}, "joins":
    [payload], "calls": {page_ordinal: payload}, "refs": {subject: reference},
    "diplomatic_raw": {act_id: marked text}}`.
    """
    tree = context.tree
    policy = _sealed_policy(context)
    manifest = tree.build_manifest(CONIECTOR, verify_inputs=False)
    records: dict[str, list[dict[str, Any]]] = {kind: [] for kind in CONIECTOR_KINDS}
    for entry in manifest["artifacts"]:
        if entry["kind"] in ("stage-seal", "decode-environment"):
            continue
        if entry["kind"] not in CONIECTOR_KINDS:
            raise FatalAccounting(
                f"the Coniector published a {entry['kind']!r}, which it never writes"
            )
        records[entry["kind"]].append(
            tree.read_artifact(CONIECTOR, entry["kind"], entry["artifact_id"])
        )
    chain = plan_chain(context)
    if not chain or len(chain) != len(records[PLAN_KIND]):
        raise FatalAccounting(
            "the Coniector's reconstruction plans are not one chain, each later one superseding "
            "the one before it"
        )
    plan_record = chain[-1]
    _closed(
        plan_record["payload"],
        PLAN_FIELDS | ({SUPERSEDES_FIELD} if len(chain) > 1 else set()),
        PLAN_SCHEMA,
        "the reconstruction plan",
    )
    plan = plan_of(plan_record)
    plan_entries, shown = diplomatic_entries(context, rows)
    expected_plan = plan_payload(policy, plan_entries)
    if dict(plan) != expected_plan:
        raise FatalAccounting(
            "the reconstruction plan is not the one the sealed switches and the Perlector's "
            "readings give"
        )
    # A plan superseded another only after an operator re-read changed the readings;
    # until then every call and reconstruction is current.
    superseded_allowed = len(chain) > 1
    calls_by_page: dict[str, list[dict[str, Any]]] = {}
    for record in records[CALL_KIND]:
        calls_by_page.setdefault(record["subject_id"], []).append(record)
    expected_pages = [call_page_id(call, shown) for call in plan["calls"]]
    if len(set(expected_pages)) != len(expected_pages):
        raise FatalAccounting("two planned reconstruction calls name one page")
    if not set(expected_pages) <= set(calls_by_page) or (
        not superseded_allowed
        and (
            sorted(calls_by_page) != sorted(expected_pages)
            or any(len(found) != 1 for found in calls_by_page.values())
        )
    ):
        raise FatalAccounting("the reconstruction calls are not the planned calls")
    expected: dict[str, dict[str, Any]] = {}
    calls: dict[int, dict[str, Any]] = {}
    current_calls: set[str] = set()
    for call, page_id in zip(plan["calls"], expected_pages, strict=True):
        what = f"the reconstruction call of page {call['page_ordinal']}"
        matching = [
            record
            for record in calls_by_page[page_id]
            if isinstance(record["payload"], Mapping) and record["payload"].get("call") == call
        ]
        if len(matching) != 1:
            raise FatalAccounting(f"{what} is not the call its plan and readings give")
        (record,) = matching
        payload = _closed(record["payload"], CALL_FIELDS, CALL_SCHEMA, what)
        text = call_prompt(call, shown, policy)
        if (
            payload["call"] != call
            or payload["page_id"] != page_id
            or payload["page_ordinal"] != call["page_ordinal"]
            or payload["prompt_version"] != PROMPT_VERSION
            or payload["prompt_sha256"] != text_sha256(text)
            or payload["shown"] != shown_keys(call, shown)
            or not isinstance(payload["maker"], Mapping)
            or set(payload["maker"]) != MAKER_FIELDS
        ):
            raise FatalAccounting(f"{what} is not the call its plan and readings give")
        _require_maker(context, payload, what)
        _require_not_asked_evidence(context, payload, what)
        _reply_as_given(context, payload, text, what)
        state, answer, problems = reply_state(payload["reply_text"], payload["stop_reason"], call)
        if payload["reply_text"] is not None and (
            state != payload["parse_state"] or problems != payload["problems"]
        ):
            raise FatalAccounting(f"{what} records a parse its reply does not give")
        if payload["reply_text"] is None and payload["parse_state"] != NOT_ASKED:
            raise FatalAccounting(f"{what} has no reply but records a parse")
        if record["outcome"] != call_outcome(payload["parse_state"]):
            raise FatalAccounting(f"{what} carries outcome {record['outcome']!r}")
        call_ref = context.artifact_ref(CONIECTOR, CALL_KIND, record["artifact_id"])
        current_calls.add(call_ref["relative_path"])
        for derived in derive_reconstructions(
            call,
            shown,
            parse_state=payload["parse_state"],
            answer=answer,
            problems=payload["problems"],
            policy=policy,
            call_ref=call_ref,
            maker=payload["maker"],
        ):
            expected[reconstruction_subject(derived)] = derived
        calls[call["page_ordinal"]] = dict(payload)
    acts: dict[str, dict[str, Any]] = {}
    joins: list[dict[str, Any]] = []
    refs: dict[str, dict[str, str]] = {}
    seen = set()
    for record in records[RECONSTRUCTION_KIND]:
        subject = record["subject_id"]
        derived = expected.get(subject)
        made_by = (
            record["payload"].get("call_ref") if isinstance(record["payload"], Mapping) else None
        )
        if (
            superseded_allowed
            and isinstance(made_by, Mapping)
            and made_by.get("relative_path") not in current_calls
        ):
            # Made from a call the current plan superseded: kept as made, not shown.
            continue
        if derived is None or dict(record["payload"]) != derived or subject in seen:
            raise FatalAccounting(
                f"the reconstruction filed under {subject} is not the one its call's reply gives"
            )
        if record["outcome"] != reconstruction_outcome(derived):
            raise FatalAccounting(f"the reconstruction of {subject} carries the wrong outcome")
        seen.add(subject)
        refs[subject] = context.artifact_ref(CONIECTOR, RECONSTRUCTION_KIND, record["artifact_id"])
        if derived["unit"] == UNIT_ACT:
            acts[subject] = derived
        else:
            joins.append(derived)
    if seen != set(expected):
        raise FatalAccounting(
            f"the Coniector published no reconstruction for {sorted(set(expected) - seen)}"
        )
    return {
        "plan": dict(plan),
        "acts": acts,
        "joins": sorted(joins, key=lambda join: join["act_keys"]),
        "calls": calls,
        "refs": refs,
        "diplomatic_raw": {entry["act_id"]: entry["text"] for entry in shown.values()},
    }
