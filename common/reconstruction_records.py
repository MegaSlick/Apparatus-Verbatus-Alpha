"""The Coniector's records: their closed shapes, how they are derived, and how they are verified.

The Coniector (`pipeline/4b_coniector`) publishes, in order:

    reconstruction-plan  (subject "coniector")  the sealed switches and every call asked
    reconstruction-call  (subject page_id)      one per call: the reply as given, parsed
    reconstruction       (subject act_id)       one per subject act and one per join

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
from typing import Any, Final

from common import page_path
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError, FatalAccounting
from common.contracts.stages import CONIECTOR, PERLECTOR
from common.page_edges import FIRST_READING_ATTEMPT
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
from common.stage import RECONSTRUCTOR_CHAIR, verify_retained_call_sampling

PLAN_KIND: Final = "reconstruction-plan"
PLAN_SUBJECT: Final = "coniector"
CALL_KIND: Final = "reconstruction-call"
RECONSTRUCTION_KIND: Final = "reconstruction"
CONIECTOR_KINDS: Final = frozenset({PLAN_KIND, CALL_KIND, RECONSTRUCTION_KIND})

PLAN_SCHEMA: Final = "coniector-plan.v1"
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
NOT_ASKED_CODES: Final = frozenset({CHAIR_ABSENT, REQUEST_OVER_CAPACITY, CALL_FAILED})
# A join the Coniector says does not continue: nothing to reconstruct.
DOES_NOT_CONTINUE: Final = "does-not-continue"

# The call's parse state beyond the grammar's own: no reply to parse.
NOT_ASKED: Final = "not-asked"

# The reading unit a Coniector reconstructs over: each page read whole.
NOT_APPLICABLE_ACT_READ: Final = (
    "the run read Designator acts one at a time; the Coniector reconstructs over whole-page "
    "readings, whose entries carry their answer order and continuation flags"
)

PLAN_FIELDS: Final = frozenset(
    {
        "schema",
        "mode",
        "pages_are_consecutive",
        "policy_sha256",
        "reading_unit",
        "not_applicable",
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
RECONSTRUCTION_FIELDS: Final = frozenset(
    {
        "schema",
        "unit",
        "act_ids",
        "act_keys",
        "page_ordinal",
        "call_ref",
        "label",
        "made",
        "maker",
        "diplomatic_raw_sha256",
        "diplomatic_clean_sha256s",
        "reconstruction_raw",
        "reconstruction_text",
        "reconstruction_uncertainty",
        "continues",
        "departures",
        "findings",
        "not_made",
    }
)
MAKER_FIELDS: Final = frozenset(
    {"kind", "chair", "chair_state", "resolved_identity", "resolved_revision", "receipt_ref"}
)


def text_sha256(text: str) -> str:
    return digest_bytes(text.encode("utf-8"))


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
    entry carries what the prompt shows, with the entry's `act_id`.
    """
    plan_entries: list[dict[str, Any]] = []
    shown: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["class"] not in (page_path.READING_CLASS, page_path.UNPLACED_CLASS):
            continue
        record = context.tree.read_artifact_reference(
            row["perlectio_ref"],
            stage=PERLECTOR,
            kind=page_path.PERLECTIO_KIND,
            subject_id=row["act_id"],
        )
        payload = record["payload"]
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
                "reading_attempt": FIRST_READING_ATTEMPT,
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
    policy: ReconstructionPolicy, reading_unit: str, plan_entries: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """The `reconstruction-plan` payload: the switches and every call, or why none applies."""
    return {
        "schema": PLAN_SCHEMA,
        "mode": policy.mode,
        "pages_are_consecutive": policy.pages_are_consecutive,
        "policy_sha256": policy.sha256,
        "reading_unit": reading_unit,
        "not_applicable": NOT_APPLICABLE_ACT_READ if plan_entries is None else None,
        "calls": []
        if plan_entries is None
        else reconstruction_plan(
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


def call_prompt(call: Mapping[str, Any], shown: Mapping[str, Mapping[str, Any]]) -> str:
    return build_reconstruction_prompt(call, shown)


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


# --- the fixture's declared replies ------------------------------------------------------


def fixture_reply(context, page_ordinal: int, pages_are_consecutive: bool) -> dict[str, Any]:
    """The synthetic fixture's one declared reply to this page under this scenario."""
    rows = [
        row
        for row in context.fixture.get("reconstruction_answer", [])
        if row.get("scenario") == context.scenario
        and row.get("page_ordinal") == page_ordinal
        and row.get("pages_are_consecutive") is pages_are_consecutive
    ]
    if len(rows) != 1:
        raise ContractError(
            f"the fixture declares {len(rows)} reconstruction answers for scenario "
            f"{context.scenario!r}, page {page_ordinal}, pages_are_consecutive = "
            f"{str(pages_are_consecutive).lower()}; a page reconstructed offline needs exactly one"
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


def _reply_as_given(context, call_record: Mapping[str, Any], what: str) -> None:
    """The reply the record holds must be the fixture's declared one or the retained one."""
    payload = call_record["payload"]
    if payload["reply_text"] is None:
        return
    if payload["serving_mode"] == SERVING_FIXTURE:
        declared = fixture_reply(
            context, payload["page_ordinal"], _sealed_policy(context).pages_are_consecutive
        )
        if (declared["content"], declared["stop_reason"]) != (
            payload["reply_text"],
            payload["stop_reason"],
        ):
            raise FatalAccounting(f"{what} holds a reply the fixture never declared")
        return
    if payload["serving_mode"] != SERVING_LIVE:
        raise FatalAccounting(f"{what} names serving mode {payload['serving_mode']!r}")
    retained = page_path.retained_reply(context.tree.read_bytes, payload["engine_call"])
    if (retained["content"], retained["finish_reason"], retained["stop_reason"]) != (
        payload["reply_text"],
        payload["finish_reason"],
        payload["stop_reason"],
    ):
        raise FatalAccounting(f"{what} holds a reply other than the one its engine returned")
    call = json.loads(
        context.tree.read_bytes(payload["engine_call"]["call_record_ref"]["relative_path"])
    )
    verify_retained_call_sampling(context, call, RECONSTRUCTOR_CHAIR)


def _sealed_policy(context) -> ReconstructionPolicy:
    policy = load_reconstruction_policy(context.args.reconstruction_config)
    context.require_sealed_config("reconstruction", policy.sha256)
    return policy


def _require_not_asked_evidence(payload: Mapping[str, Any], what: str) -> None:
    if payload["reply_text"] is not None:
        return
    problems = payload["problems"]
    if not isinstance(problems, list) or len(problems) != 1 or not isinstance(problems[0], dict):
        raise FatalAccounting(f"{what} has no reply and not exactly one reason it was not asked")
    code = problems[0].get("code")
    if code not in NOT_ASKED_CODES:
        raise FatalAccounting(f"{what} was not asked for a reason no call records ({code!r})")
    if code == CHAIR_ABSENT and payload["maker"]["chair_state"] != "absent":
        raise FatalAccounting(f"{what} says its chair is absent, but its maker names a chair")
    if code == REQUEST_OVER_CAPACITY and (
        not isinstance(payload["capacity"], Mapping) or payload["capacity"].get("fits") is not False
    ):
        raise FatalAccounting(f"{what} was refused for capacity with no record that it did not fit")
    if code == CALL_FAILED and not isinstance(payload["failure"], Mapping):
        raise FatalAccounting(f"{what} failed with no failure recorded")


def verified_reconstructions(
    context, reading_unit: str, rows: Sequence[Mapping[str, Any]] | None
) -> dict[str, Any]:
    """Every Coniector record, each recomputed and proven, for the Armarium to show.

    `reading_unit` is the run's sealed reading unit and `rows` its
    `reading_acts` rows, or `None` for an act-read run.
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
    if len(records[PLAN_KIND]) != 1:
        raise FatalAccounting("the Coniector did not publish exactly one reconstruction plan")
    (plan_record,) = records[PLAN_KIND]
    plan = _closed(plan_record["payload"], PLAN_FIELDS, PLAN_SCHEMA, "the reconstruction plan")
    plan_entries, shown = (None, {}) if rows is None else diplomatic_entries(context, rows)
    expected_plan = plan_payload(policy, reading_unit, plan_entries)
    if dict(plan) != expected_plan:
        raise FatalAccounting(
            "the reconstruction plan is not the one the sealed switches and the Perlector's "
            "readings give"
        )
    calls_by_page = {record["subject_id"]: record for record in records[CALL_KIND]}
    if len(calls_by_page) != len(records[CALL_KIND]):
        raise FatalAccounting("two reconstruction calls name one page")
    expected_pages = [call_page_id(call, shown) for call in plan["calls"]]
    if sorted(calls_by_page) != sorted(expected_pages):
        raise FatalAccounting("the reconstruction calls are not the planned calls")
    expected: dict[str, dict[str, Any]] = {}
    calls: dict[int, dict[str, Any]] = {}
    for call, page_id in zip(plan["calls"], expected_pages, strict=True):
        record = calls_by_page[page_id]
        what = f"the reconstruction call of page {call['page_ordinal']}"
        payload = _closed(record["payload"], CALL_FIELDS, CALL_SCHEMA, what)
        text = call_prompt(call, shown)
        if (
            payload["call"] != call
            or payload["page_id"] != page_id
            or payload["page_ordinal"] != call["page_ordinal"]
            or payload["prompt_version"] != PROMPT_VERSION
            or payload["prompt_sha256"] != text_sha256(text)
            or payload["shown"] != shown_keys(call, shown)
            or set(payload["maker"]) != MAKER_FIELDS
            or payload["maker"]["kind"] != MAKER_MODEL
            or payload["maker"]["chair"] != RECONSTRUCTOR_CHAIR
        ):
            raise FatalAccounting(f"{what} is not the call its plan and readings give")
        _reply_as_given(context, record, what)
        _require_not_asked_evidence(payload, what)
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
        if derived is None or dict(record["payload"]) != derived:
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
