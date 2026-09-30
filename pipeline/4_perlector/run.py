"""Perlector: reads each sealed page whole, with the testimonia as fallible clues.

The pass reads every sealed Exemplar page once (`page_run.py`) and seals the
stage. This module opens the pass -- its sealed configs, its chair, its serving
mode, its concurrency -- and holds what the page path reads its evidence
through: provenance, the `reader-sent` records of live calls, the retained
replies a resume must account for, and the facts of a failed call.

The sealed serving-recipe row picks the reader. A `kind = "vllm"` row for the
Perlector chair reads live; any other row reads the fixture's declared page
answers, which prove wiring only. A real submission has no fixture
declaration, so a non-live row there refuses (`fixture_reader_for`).

    python pipeline/4_perlector/run.py --run-root <dir> --run-id <id>
"""

import json
import sys
from collections import deque
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit  # noqa: E402
import page_run  # noqa: E402
import protocol  # noqa: E402
from live_reader import EngineSignalRefusal  # noqa: E402
from reader import FixtureReader  # noqa: E402

import operations.serving.errors as serving_errors  # noqa: E402
from common.chairs.models import AbsentChair, ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.errors import ContractError, SchemaRefusal  # noqa: E402
from common.contracts.identities import artifact_id  # noqa: E402
from common.contracts.identities import attempt_id as derived_attempt_id  # noqa: E402
from common.contracts.serving import (  # noqa: E402
    CHAIR_CALL_RECORD_SCHEMA,
    CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA,
)
from common.contracts.stages import PERLECTOR  # noqa: E402
from common.decoding import (  # noqa: E402
    load_decoding_policy,
    perlector_page_max_tokens,
    refuse_retired_call_record,
)
from common.image_sniff import PNG_SIGNATURE  # noqa: E402
from common.page_testimonia import sealed_proposal_regions  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    PERLECTOR_CHAIR,
    fixture_serving_details,
    is_real_ingress,
    open_stage_context,
    recovery_region_count,
    run_stage,
    stage_parser,
    verify_retained_call_sampling,
)
from operations.serving.assembly import (  # noqa: E402
    bound_serving_recipes,
    stage_chair_client,
)
from operations.serving.client import ChairClient, serving_mode_for  # noqa: E402
from operations.serving.errors import ChairResponseRefusal  # noqa: E402
from operations.serving.http import EndpointUnavailable  # noqa: E402

DESCRIPTION = "Perlector: reads each sealed page whole, with the testimonia as fallible clues."

# Every send of a live call is recorded as a `reader-sent` record before the call
# leaves, so a resumed pass can tell a received reply from a call that never answered.
SENT_KIND: Final = "reader-sent"
SENT_SCHEMA: Final = "perlector-reader-sent.v1"
_SENT_FIELDS: Final = frozenset(
    {
        "schema",
        "act_key",
        "attempt_ordinal",
        "pass",
        "send",
        "receipt_ref",
        "concurrency",
        "image_sha256s",
    }
)


def real_ingress(context) -> bool:
    """Whether this run authority names the real route, by the shared reader."""
    return is_real_ingress(context.run)


def declared_reading_failure(context, act_key: str) -> str | None:
    """The non-completed outcome a fixture scenario declares for a key, if any.

    A real submission declares nothing and never reads the fixture.
    """
    if real_ingress(context):
        return None
    for row in context.fixture.get("reading_failure", []):
        if row["scenario"] == context.scenario and row["act_key"] == act_key:
            return row["outcome"]
    return None


def fixture_reader_for(context, chair: ChairIdentity | AbsentChair, serving_mode: str):
    """Refuse a non-live row on a real submission, before anything is published.

    A declared text cannot stand in for real ink. An absent chair reads nothing and
    live mode starts its chair on first use, so both return `None`; the fixture route
    returns the fixture's reader.
    """
    if serving_mode == "live":
        return None
    if not real_ingress(context):
        return FixtureReader(context.fixture, context.scenario)
    if isinstance(chair, AbsentChair):
        return None
    raise ContractError(
        f"the Perlector cannot read a real submission through the fixture reader: the sealed "
        f"serving-recipe row for chair {chair.role!r} is not a live row, and a declared text "
        "cannot stand in for a reading of real ink. Start a new run sealed under a catalogue "
        "whose Perlector row is live; a sealed run's catalogue cannot be changed"
    )


def perlector_chair(context) -> ChairIdentity | AbsentChair:
    """The Perlector chair, resolved by name. Never another chair, never a base."""
    resolved = context.registry.resolve(PERLECTOR_CHAIR)
    if not isinstance(resolved, (ChairIdentity, AbsentChair)):
        raise ContractError("Perlector resolution returned neither an identity nor an absence")
    return resolved


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


def perlector_serving_mode(context, args, chair: ChairIdentity | AbsentChair) -> str:
    """`"fixture"` or `"live"`, from the sealed serving-recipe row kind alone.

    The catalogue is already sealed into `config_digest`; `--placement-tier` is a
    measured fact of the card and deliberately unsealed. Resolved before anything is
    published or started, so a live row without a tier refuses on an untouched tree. An
    absent chair is `fixture`: it reads nothing and has no identity to look a row up by.
    """
    if isinstance(chair, AbsentChair):
        return "fixture"
    return serving_mode_for(
        bound_serving_recipes(context, args.serving_recipes_config), chair, args.placement_tier
    )


class ResidentChair:
    """The one live chair a Perlector pass holds, and the promise it is stopped.

    `main` closes it in a `finally`, and the pass closes it before sealing, so a failed
    shutdown is never reported over a sealed stage; `close` is idempotent for that
    reason. A `ServiceStopError` propagates: an unverified shutdown must be reported.
    """

    __slots__ = ("client",)

    def __init__(self) -> None:
        self.client: ChairClient | None = None

    def close(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            client.__exit__()


def engine_call_inputs(context, engine_call: dict[str, Any] | None) -> list[dict[str, str]]:
    """Bind the two blobs a live reading's record names as direct inputs.

    A fixture reading has no `engine_call` and adds nothing. Each reference is
    re-derived from the bytes on disk, so a record cannot name a response that is
    absent or has changed, and the call record is held to the Perlector's sealed
    sampling row and to the serving receipt's seed.
    """
    if engine_call is None:
        return []
    if not isinstance(engine_call, dict) or set(engine_call) != {
        "call_record_ref",
        "raw_response_ref",
        "response_sha256",
        "finish_reason",
        "served_model_id",
    }:
        raise SchemaRefusal(f"a live reading's engine_call has the wrong shape: {engine_call!r}")
    if engine_call["response_sha256"] != engine_call["raw_response_ref"]["sha256"]:
        raise SchemaRefusal(
            "a live reading's engine_call names two different digests for one response: "
            f"response_sha256={engine_call['response_sha256']!r}, "
            f"raw_response_ref sha256={engine_call['raw_response_ref']['sha256']!r}"
        )
    references = []
    for name in ("raw_response_ref", "call_record_ref"):
        claimed = engine_call[name]
        observed = context.input_ref(claimed["relative_path"])
        if observed != dict(claimed):
            raise SchemaRefusal(
                f"a live reading's {name} names {claimed!r}, but the retained bytes at that "
                f"path are {observed!r}"
            )
        references.append(observed)
    call = _json_object(context.tree.read_bytes(engine_call["call_record_ref"]["relative_path"]))
    if call is None:
        raise SchemaRefusal("a live reading's call record is not a JSON object")
    try:
        verify_retained_call_sampling(context, call, "perlector", variance_arm=None)
    except ContractError as error:
        raise SchemaRefusal(
            f"a live reading's call record is not its sealed request: {error}"
        ) from error
    return references


def _start_chair(run: "_Pass") -> None:
    """Start this run's one chair and keep its receipt reference on the pass.

    Every record the pass publishes names the receipt of the service that answered,
    which `ChairClient.__enter__` has checked names this chair and revision.
    """
    # Assigned before entering so `close` covers any failure from here on; closing an
    # unstarted client is a no-op.
    run.service.client = run.client_factory(run.context, run.chair, run.args.placement_tier)
    run.service.client.__enter__()
    run.receipt_ref = dict(run.service.client.handle.receipt_reference)


def _sent_id(subject_id: str, ordinal: int, pass_name: str, send: int) -> str:
    return artifact_id(
        PERLECTOR,
        SENT_KIND,
        subject_id,
        derived_attempt_id(subject_id, f"{pass_name}-sent:{ordinal}", send),
    )


def _validate_sent(payload: Any, *, key: str, ordinal: int, pass_name: str, send: int) -> None:
    if (
        not isinstance(payload, dict)
        or set(payload) != _SENT_FIELDS
        or payload["schema"] != SENT_SCHEMA
        or (payload["act_key"], payload["attempt_ordinal"], payload["pass"], payload["send"])
        != (key, ordinal, pass_name, send)
        or not isinstance(payload["receipt_ref"], dict)
        or type(payload["concurrency"]) is not int
        or payload["concurrency"] < 1
        or not isinstance(payload["image_sha256s"], list)
        or not all(isinstance(value, str) for value in payload["image_sha256s"])
    ):
        raise SchemaRefusal(f"a reader-sent record for {key!r} is not its closed schema")


def _sent_records(
    context, subject_id: str, key: str, ordinal: int, pass_name: str
) -> list[dict[str, Any]]:
    """Every send of one pass of this subject, numbered 1..N, in order."""
    records: list[dict[str, Any]] = []
    while True:
        identifier = _sent_id(subject_id, ordinal, pass_name, len(records) + 1)
        if not context.tree.has_artifact(PERLECTOR, SENT_KIND, identifier):
            return records
        record = context.tree.read_artifact(PERLECTOR, SENT_KIND, identifier)
        _validate_sent(
            record["payload"],
            key=key,
            ordinal=ordinal,
            pass_name=pass_name,
            send=len(records) + 1,
        )
        records.append(record)


def _sent_refs(context, subject_id: str, key: str, ordinal: int, pass_name: str):
    return [
        context.artifact_ref(PERLECTOR, SENT_KIND, record["artifact_id"])
        for record in _sent_records(context, subject_id, key, ordinal, pass_name)
    ]


def _publish_sent(
    run: "_Pass",
    subject_id: str,
    key: str,
    ordinal: int,
    pass_name: str,
    image_sha256s: list[str],
) -> None:
    """Record, before it leaves, that this call is being sent, and under what.

    A second send names the first, so a call re-sent after an interruption is visible.
    `concurrency` is the width of the window the call was sent in: how many reader calls
    this pass kept in flight at most, which bounds the batch the engine decoded it in.
    `image_sha256s` are the images the call carries, in the order it sends them. A
    page's send is keyed by its page id and `page_run.page_key`, recorded in the
    schema's `act_key` field.
    """
    earlier = _sent_refs(run.context, subject_id, key, ordinal, pass_name)
    send = len(earlier) + 1
    run.context.publish(
        kind=SENT_KIND,
        subject_id=subject_id,
        outcome="read",
        attempt=derived_attempt_id(subject_id, f"{pass_name}-sent:{ordinal}", send),
        inputs=earlier + [dict(run.receipt_ref)],
        payload={
            "schema": SENT_SCHEMA,
            "act_key": key,
            "attempt_ordinal": ordinal,
            "pass": pass_name,
            "send": send,
            "receipt_ref": dict(run.receipt_ref),
            "concurrency": run.concurrency,
            "image_sha256s": list(image_sha256s),
        },
    )


# Blobs the serving manager keeps in a stage's own store beside the chair's calls.
_SERVING_BLOB_SCHEMAS: Final = frozenset({"serving-launch-audit.v1", "serving-evidence.v1"})


def _json_object(data: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _unrecorded_replies(context) -> tuple[list[dict[str, Any]], bool]:
    """Every retained reply no record of this stage binds, as far as it can be attributed.

    Returns the unbound call records that carry a reply, and whether any retained blob is
    a reply that cannot be attributed at all. The client retains a reply's raw bytes
    before the call record that names them, so a pass stopped between the two leaves
    bytes no call record names: every blob that is not a call record, a reply one names,
    serving evidence, a page render, or an input of some record is counted as such a
    reply.
    """
    manifest = context.tree.build_manifest(PERLECTOR)
    bound = {
        reference["relative_path"]
        for entry in manifest["artifacts"]
        for reference in context.tree.read_artifact(PERLECTOR, entry["kind"], entry["artifact_id"])[
            "inputs"
        ]
    }
    calls, named, others = [], set(), []
    for name in manifest["blobs"]:
        path = context.tree.blob_path(PERLECTOR, name)
        data = context.tree.read_bytes(path)
        if data.startswith(PNG_SIGNATURE):
            # A page render this stage cut for a reader call; a chat endpoint's reply is
            # never an image.
            continue
        record = _json_object(data)
        schema = record.get("schema") if record is not None else None
        # A call record from before the decoding bump is refused by its name, not
        # counted as a reply no record binds.
        refuse_retired_call_record(schema, subject=f"retained blob {path}")
        if schema in {CHAIR_CALL_RECORD_SCHEMA, CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA}:
            reply = record.get("raw_response_ref")
            if reply is not None:
                named.add(reply["relative_path"])
                if path not in bound:
                    calls.append(record)
        elif schema not in _SERVING_BLOB_SCHEMAS:
            others.append(path)
    unattributed = any(path not in bound and path not in named for path in others)
    return calls, unattributed


def _answers_a_send(calls: list[dict[str, Any]], markers: list[dict[str, Any]]) -> bool:
    """Whether an unbound reply came from one of these sends' sessions, about these images."""
    sent = [
        (record["payload"]["receipt_ref"], record["payload"]["image_sha256s"]) for record in markers
    ]
    return any((call.get("receipt_ref"), call.get("image_sha256s")) in sent for call in calls)


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{value!r} names no time zone")
    return parsed


def refuse_past_deadline(
    deadline: datetime | None, seconds_needed: int, what: str, *, rate: str, remedy: str
) -> None:
    """Refuse work the reading deadline cannot hold, naming the planning rate and the way out."""
    if deadline is None:
        return
    remaining = int((deadline - datetime.now(timezone.utc)).total_seconds())
    if remaining < seconds_needed:
        raise ContractError(
            f"{what} needs {seconds_needed}s at {rate}, but the reading deadline "
            f"{deadline.isoformat()} leaves {remaining}s; nothing more was started. {remedy}"
        )


_NO_FAILURE_EVIDENCE: Final = {
    "raw_response_ref": None,
    "call_record_ref": None,
    "request_sha256": None,
    "receipt_ref": None,
    "served_model_id": None,
    "response_completion": None,
}


def _failure_facts(phase: str, kind: str, code: str, detail: str, **evidence) -> dict[str, Any]:
    return {
        "phase": phase,
        "kind": kind,
        "code": code,
        "detail": detail,
        **_NO_FAILURE_EVIDENCE,
        **evidence,
    }


def _copied_ref(reference: Mapping[str, str] | None) -> dict[str, str] | None:
    return dict(reference) if reference is not None else None


def _reported_call_evidence(error: Exception) -> dict[str, Any]:
    return {
        "call_record_ref": _copied_ref(getattr(error, "call_record_ref", None)),
        "request_sha256": getattr(error, "request_sha256", None),
        "receipt_ref": _copied_ref(getattr(error, "receipt_ref", None)),
        "served_model_id": getattr(error, "served_model_id", None),
    }


def _failure_record(error: Exception, *, phase: str) -> dict[str, Any] | None:
    """Translate only observed engine and transport failures into retained facts.

    Contract and schema errors return `None`: recorded as an engine incident, one would
    let the stage seal over an integrity defect.
    """
    if isinstance(error, EngineSignalRefusal):
        return _failure_facts(
            phase,
            "engine-signal",
            error.code,
            error.detail,
            raw_response_ref=dict(error.raw_response_ref),
            call_record_ref=dict(error.call_record_ref),
            request_sha256=error.request_sha256,
            receipt_ref=dict(error.receipt_ref),
            served_model_id=error.served_model_id,
            response_completion="complete",
        )
    if isinstance(error, ChairResponseRefusal):
        return _failure_facts(
            phase,
            "chair-response",
            error.code,
            error.detail,
            raw_response_ref=_copied_ref(getattr(error, "raw_response_ref", None)),
            response_completion="complete",
            **_reported_call_evidence(error),
        )
    if isinstance(error, serving_errors.ChairTransportFailure):
        return _failure_facts(
            phase,
            "transport",
            error.code,
            error.detail,
            response_completion=getattr(error, "response_completion", None),
            **_reported_call_evidence(error),
        )
    if isinstance(error, EndpointUnavailable):
        return _failure_facts(
            phase,
            "transport",
            "endpoint-unavailable",
            str(error),
            response_completion="unknown",
        )
    return None


def main(registry_factory=ChairRegistry.from_toml, serving_factory=None) -> int:
    """Run the pass, and guarantee any chair it started is stopped.

    Both parameters are test seams; neither decides which engine answers, which is the
    sealed serving-recipe row's business. `_read_the_acts` stops the chair before
    sealing; the `finally` covers a pass that raised first.

    `ChairResponseRefusal` is a `RuntimeError`, which `run_stage` does not catch, so it
    is re-raised as a `ContractError` to exit as a named refusal. `ChairRequestRefusal`
    is deliberately not caught: it means this code built a bad request, and a traceback
    at the construction site is worth more.
    """
    service = ResidentChair()
    try:
        return _read_the_acts(registry_factory, serving_factory, service)
    except ChairResponseRefusal as refusal:
        raise ContractError(f"{type(refusal).__name__}: {refusal}") from refusal
    finally:
        service.close()


def _read_the_acts(registry_factory, serving_factory, service: ResidentChair) -> int:
    """One Perlector pass: every sealed page read whole (`page_run.py`), then the seal."""
    run = _open_pass(registry_factory, serving_factory, service)
    page_run.read_the_pages(
        run,
        page_run.StageHooks(
            provenance_for=provenance_for,
            engine_call_inputs=engine_call_inputs,
            start_chair=_start_chair,
            publish_sent=_publish_sent,
            sent_records=_sent_records,
            unrecorded_replies=_unrecorded_replies,
            answers_a_send=_answers_a_send,
            in_order_window=_in_order_window,
            refuse_past_deadline=refuse_past_deadline,
            failure_record=_failure_record,
            real_ingress=real_ingress,
            sent_kind=SENT_KIND,
        ),
    )
    # Before the seal, so a failed shutdown is never reported over a sealed stage;
    # `close` is idempotent with `main`'s `finally`.
    service.close()
    run.context.seal_boundary()
    run.context.finish()
    return EXIT_COMPLETE


@dataclass
class _Pass:
    """The sealed inputs one Perlector pass reads every page under, and its live chair."""

    context: Any
    args: Any
    service: ResidentChair
    client_factory: Any
    chair: ChairIdentity | AbsentChair
    serving_mode: str
    protocol_config: dict[str, Any]
    # The sealed decoding policy, and from it the output cap of one whole-page reading.
    decoding_policy: dict[str, Any]
    page_max_tokens: int
    audit_policy: dict[str, Any]
    audit_sha256: str
    # The run-wide routing denominator the page testimonia are read against.
    all_proposal_regions: list[dict]
    receipt_ref: dict[str, str] | None = None
    # Reader calls in flight at once; see `_reading_concurrency`.
    concurrency: int = 1


def _open_pass(registry_factory, serving_factory, service: ResidentChair) -> _Pass:
    """Resolve every sealed input the pass needs, refusing before anything is published."""
    parser = stage_parser(DESCRIPTION)
    parser.add_argument(
        "--reading-deadline",
        type=_utc,
        default=None,
        help="UTC time by which a live pass must finish reading; it refuses to start, or "
        "to read another page, when the planned calls would run past it",
    )
    parser.add_argument(
        "--perlector-concurrency",
        type=_positive_int,
        default=None,
        help="reader calls a live pass keeps in flight at once, so the engine can batch "
        "them; capped by the served row's max_num_seqs, which is also the default. "
        "A fixture pass reads one page at a time",
    )
    args = parser.parse_args()
    context = open_stage_context(args, PERLECTOR, registry_factory=registry_factory)
    decoding_policy, decoding_sha256 = load_decoding_policy(args.decoding_config)
    context.require_sealed_config("decoding", decoding_sha256)
    chair = perlector_chair(context)
    serving_mode = perlector_serving_mode(context, args, chair)
    fixture_reader_for(context, chair, serving_mode)
    protocol_config, protocol_sha256 = protocol.load(context.perlector_protocol_config_path)
    context.require_sealed_config("perlector-protocol", protocol_sha256)
    audit_policy, audit_sha256 = audit.load(context.perlector_audit_config_path)
    context.require_sealed_config("perlector-audit", audit_sha256)
    if args.act:
        raise ContractError(
            f"asked to read act {args.act}, but the Perlector reads every sealed page whole "
            "and names its own acts, so there is no Designator act to read alone; run the "
            "pass without --act"
        )
    return _Pass(
        context=context,
        args=args,
        service=service,
        client_factory=serving_factory
        or partial(
            stage_chair_client,
            decoding_policy=decoding_policy,
            decoding_config_sha256=decoding_sha256,
        ),
        chair=chair,
        serving_mode=serving_mode,
        protocol_config=protocol_config,
        decoding_policy=decoding_policy,
        page_max_tokens=perlector_page_max_tokens(decoding_policy),
        audit_policy=audit_policy,
        audit_sha256=audit_sha256,
        all_proposal_regions=sealed_proposal_regions(context),
        concurrency=_reading_concurrency(context, args, chair, serving_mode),
    )


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise ValueError(f"{value!r} is not a positive count")
    return number


def _reading_concurrency(context, args, chair, serving_mode: str) -> int:
    """How many reader calls may be in flight at once.

    Only a live engine batches, and never beyond its served row's `max_num_seqs`.
    """
    if serving_mode != "live":
        return 1
    bound = (
        bound_serving_recipes(context, args.serving_recipes_config)
        .for_identity(chair, args.placement_tier)
        .max_num_seqs
    )
    return min(args.perlector_concurrency or bound, bound)


def _in_order_window(width: int, jobs) -> list[Any]:
    """Run jobs' calls with at most `width` jobs unfinished; finish every job in order here.

    A job is `(call, finish)`, and `call` is `None` for a job that only publishes. Jobs
    are drawn lazily, so the next is prepared, and its deadline checked, only once fewer
    than `width` are unfinished. `finish` runs on this thread, strictly in job order,
    with what `call` returned (`None` without a call), so records are published in the
    order a serial pass publishes them. Width 1 calls inline, with no thread.

    Every job drawn is finished: if drawing a job, a call or a finish raises, the jobs
    already drawn are still finished in order, so no reply is left without its record,
    and then the first error re-raises with any later ones attached as notes. An
    interrupt stops waiting at once, so the chair can be stopped: it first finishes
    every job whose reply has already arrived, and leaves the calls still out
    unfinished. A reply that arrived is recorded even when an earlier call is still
    out; a record's bytes do not depend on the order it was written in.
    """
    finished: list[Any] = []
    if width == 1:
        for call, finish in jobs:
            finished.append(finish(call() if call is not None else None))
        return finished
    pool = ThreadPoolExecutor(max_workers=width)
    window: deque = deque()

    def finish_first() -> None:
        future, finish = window.popleft()
        finished.append(finish(future.result() if future is not None else None))

    error: Exception | None = None
    try:
        try:
            for call, finish in jobs:
                window.append((pool.submit(call) if call is not None else None, finish))
                while window and (len(window) == width or window[0][0] is None):
                    finish_first()
        except Exception as raised:
            error = raised
        while window:
            try:
                finish_first()
            except Exception as raised:
                if error is None:
                    error = raised
                else:
                    error.add_note(f"a later job also failed: {type(raised).__name__}: {raised}")
    except BaseException as interrupt:
        for future, finish in window:
            if future is not None and (
                not future.done() or future.cancelled() or future.exception() is not None
            ):
                continue
            try:
                finish(future.result() if future is not None else None)
            except Exception as raised:
                interrupt.add_note(f"an arrived reply was not recorded: {raised}")
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if error is not None:
        raise error
    return finished


def _next_attempt(context, act_id: str, regions: list[dict]) -> int:
    """Which reading attempt this is, derived from the act rather than from history.

    One reading of the proposal plus one per recovery region cut since, so a rerun that
    changed nothing recomputes the same ordinal and reuses the same bytes. Witness
    testimony is not counted: a Testimonium primes a reading and never makes a new
    attempt.

    Counted by the shared `recovery_region_count`, which refuses an unknown origin.
    """
    return recovery_region_count(act_id, regions) + 1


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
