"""Coniector: a labelled, unconfirmed reconstruction beneath each diplomatic reading.

*Conicere*, to conjecture. After the Perlector has read, the Coniector's chair
(`reconstructor`, the Perlector's model asked text only) is shown each page's
diplomatic readings and, on a run sealed `pages_are_consecutive`, the
neighbouring pages' edge acts and the pieces of an act that crosses a page
break. It proposes departures from the diplomatic text and reports findings; it
never sees the page image and never changes the diplomatic reading, which stays
the established text. Only the Armarium reads what this stage writes.

Per run, in order (`common/reconstruction_records.py` holds the shapes):

    reconstruction-plan   the sealed switches and every call the run asks
    reconstruction-call   one per call: the reply as given, parsed, or why none
    reconstruction        one per subject act and one per join: made, or not made

`mode` (config/reconstruction.toml) is on by default; with `mode = "off"` the plan
asks nothing and no model is loaded. A reconstruction that could not be made
leaves the diplomatic reading delivered and says why.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import operations.serving.errors as serving_errors  # noqa: E402
from common.chairs.models import ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.stages import CONIECTOR  # noqa: E402
from common.decoding import load_decoding_policy, reconstructor_max_tokens  # noqa: E402
from common.reconstruction import MODE_ON, load_reconstruction_policy  # noqa: E402
from common.reconstruction_prompt import PROMPT_VERSION, shown_keys  # noqa: E402
from common.reconstruction_records import (  # noqa: E402
    CALL_FAILED,
    CALL_KIND,
    CALL_SCHEMA,
    CHAIR_ABSENT,
    PLAN_KIND,
    PLAN_SUBJECT,
    RECONSTRUCTION_KIND,
    RECONSTRUCTOR_CHAIR,
    REQUEST_OVER_CAPACITY,
    SERVING_FIXTURE,
    SERVING_LIVE,
    call_outcome,
    call_page_id,
    call_prompt,
    derive_reconstructions,
    diplomatic_entries,
    expected_maker,
    fixture_reply,
    plan_payload,
    reconstruction_outcome,
    reconstruction_subject,
    reply_state,
    text_sha256,
)
from common.request_capacity import (  # noqa: E402
    RequestCapacityRefusal,
    reconstruction_request_capacity,
)
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    fixture_serving_details,
    open_stage_context,
    reading_acts,
    run_stage,
    stage_parser,
)
from operations.serving.assembly import (  # noqa: E402
    SERVING_READER,
    bound_serving_recipes,
    stage_chair_client,
)
from operations.serving.chat_request import EngineSignalRefusal, send_chat_request  # noqa: E402
from operations.serving.client import serving_mode_for  # noqa: E402
from operations.serving.errors import ChairResponseRefusal  # noqa: E402
from operations.serving.http import EndpointUnavailable  # noqa: E402

DESCRIPTION = "Coniector: a labelled, unconfirmed reconstruction beneath each diplomatic reading."

# A failure of one call that leaves its page's reconstructions not made; any
# other failure stops the stage.
_CALL_FAILURES = (
    EngineSignalRefusal,
    ChairResponseRefusal,
    EndpointUnavailable,
    serving_errors.ChairTransportFailure,
)


class _Chair:
    """The reconstructor chair: who it is, how it is served, and its live client once started."""

    def __init__(self, context, decoding, decoding_sha256, client_factory) -> None:
        self.context = context
        self.identity = context.registry.resolve(RECONSTRUCTOR_CHAIR)
        self.present = isinstance(self.identity, ChairIdentity)
        self.serving_mode = (
            serving_mode_for(
                bound_serving_recipes(context, context.args.serving_recipes_config),
                self.identity,
                context.args.placement_tier,
            )
            if self.present
            else SERVING_FIXTURE
        )
        self.decoding = decoding
        self.decoding_sha256 = decoding_sha256
        self.client_factory = client_factory
        self.client = None
        self.receipt_ref = None
        self.fixture_receipt_ref = None

    @property
    def live(self) -> bool:
        return self.serving_mode == SERVING_LIVE

    def row(self):
        return bound_serving_recipes(
            self.context, self.context.args.serving_recipes_config
        ).for_identity(self.identity, self.context.args.placement_tier)

    def start(self) -> None:
        # Assigned before entering so `close` covers a failed start.
        self.client = self.client_factory(
            self.context,
            self.identity,
            self.context.args.placement_tier,
            decoding_policy=self.decoding,
            decoding_config_sha256=self.decoding_sha256,
        )
        self.client.__enter__()
        self.receipt_ref = dict(self.client.handle.receipt_reference)

    def close(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            client.__exit__()

    def maker(self, *, asked: bool) -> dict:
        """Who made a call's reconstructions: the chair, and the receipt of what served it."""
        receipt = None
        if asked and self.live:
            receipt = self.receipt_ref
        elif asked:
            if self.fixture_receipt_ref is None:
                self.fixture_receipt_ref = self.context.write_serving_receipt(
                    self.identity, fixture_serving_details(self.identity)
                )
            receipt = self.fixture_receipt_ref
        return expected_maker(self.identity, receipt)


def _sealed_call(context, page_id: str):
    from common.contracts.identities import artifact_id

    identifier = artifact_id(CONIECTOR, CALL_KIND, page_id, None)
    if not context.tree.has_artifact(CONIECTOR, CALL_KIND, identifier):
        return None
    return context.tree.read_artifact(CONIECTOR, CALL_KIND, identifier)


def _not_asked(code: str, detail: str) -> dict:
    return {
        "reply_text": None,
        "finish_reason": None,
        "stop_reason": None,
        "engine_call": None,
        "failure": None,
        "capacity": None,
        "problems": [{"code": code, "detail": detail}],
    }


def _ask(chair: _Chair, call: dict, text: str, policy, max_tokens: int, what: str) -> dict:
    """One call's reply, or why it was not asked; nothing here publishes."""
    if not chair.present:
        return _not_asked(CHAIR_ABSENT, "no reconstructor chair is configured for this run")
    if not chair.live:
        reply = fixture_reply(chair.context, call, policy.pages_are_consecutive)
        return {
            "reply_text": reply["content"],
            "finish_reason": reply["stop_reason"],
            "stop_reason": reply["stop_reason"],
            "engine_call": None,
            "failure": None,
            "capacity": None,
            "problems": [],
        }
    try:
        admitted = reconstruction_request_capacity(
            chair.row(),
            prompt_text=text,
            acts=len(call["subjects"]),
            joins=len(call["chains"]),
            policy=policy,
            answer_max_tokens=max_tokens,
        )
    except RequestCapacityRefusal as refusal:
        if refusal.capacity is None:
            raise
        return {
            **_not_asked(REQUEST_OVER_CAPACITY, str(refusal)),
            "capacity": {"capacity": refusal.capacity, "answer_reserve": None, "max_tokens": None},
        }
    if chair.client is None:
        chair.start()
    try:
        result = send_chat_request(
            chair.client,
            content=text,
            image_sha256s=[],
            capacity=admitted["capacity"],
            max_tokens=admitted["max_tokens"],
            what=what,
        )
    except _CALL_FAILURES as error:
        refs = {
            name: dict(getattr(error, name))
            for name in ("raw_response_ref", "call_record_ref")
            if getattr(error, name, None) is not None
        }
        failure = {
            "code": getattr(error, "code", type(error).__name__),
            "detail": str(error),
            **refs,
        }
        return {
            **_not_asked(CALL_FAILED, f"{failure['code']}: {failure['detail']}"),
            "capacity": admitted,
            "failure": failure,
        }
    return {
        "reply_text": result["content"],
        "finish_reason": result["finish_reason"],
        "stop_reason": result["stop_reason"],
        "engine_call": result["engine_call"],
        "failure": None,
        "capacity": admitted,
        "problems": [],
    }


def _inputs(context, shown: dict, keys: list, asked: dict) -> list:
    """The Perlectio of every entry the call showed, and what the engine retained."""
    refs = [shown[key]["perlectio_ref"] for key in keys]
    engine_call = asked.get("engine_call")
    if engine_call is not None:
        refs += [
            context.input_ref(engine_call[name]["relative_path"])
            for name in ("raw_response_ref", "call_record_ref")
        ]
    failure = asked.get("failure") or {}
    refs += [
        context.input_ref(failure[name]["relative_path"])
        for name in ("raw_response_ref", "call_record_ref")
        if name in failure
    ]
    unique = []
    for ref in refs:
        if ref not in unique:
            unique.append(ref)
    return unique


def _publish_call(context, chair: _Chair, call: dict, shown: dict, policy, max_tokens: int):
    """Publish one call's record, or adopt the one already sealed for its page."""
    page_id = call_page_id(call, shown)
    text = call_prompt(call, shown, policy)
    keys = shown_keys(call, shown)
    sealed = _sealed_call(context, page_id)
    if sealed is not None:
        payload = sealed["payload"]
        maker = payload.get("maker")
        if (
            payload.get("call") != call
            or payload.get("prompt_sha256") != text_sha256(text)
            or payload.get("serving_mode") != chair.serving_mode
            or not isinstance(maker, dict)
            or maker != expected_maker(chair.identity, maker.get("receipt_ref"))
        ):
            raise ContractError(
                f"page {page_id}'s retained reconstruction call was asked from another plan, "
                "other readings or another chair than this run has now; it is not adopted. "
                "Reconstruct in a new run"
            )
        return sealed
    what = f"the reconstruction of page {call['page_ordinal']}"
    asked = _ask(chair, call, text, policy, max_tokens, what)
    state, _answer, problems = reply_state(asked["reply_text"], asked["stop_reason"], call)
    if asked["reply_text"] is None:
        problems = asked["problems"]
    payload = {
        "schema": CALL_SCHEMA,
        "page_id": page_id,
        "page_ordinal": call["page_ordinal"],
        "call": call,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": text_sha256(text),
        "shown": keys,
        "serving_mode": chair.serving_mode,
        "capacity": asked["capacity"],
        "engine_call": asked["engine_call"],
        "failure": asked["failure"],
        "reply_text": asked["reply_text"],
        "finish_reason": asked["finish_reason"],
        "stop_reason": asked["stop_reason"],
        "parse_state": state,
        "problems": problems,
        "maker": chair.maker(asked=asked["reply_text"] is not None),
    }
    context.publish(
        kind=CALL_KIND,
        subject_id=page_id,
        outcome=call_outcome(state),
        inputs=_inputs(context, shown, keys, asked),
        payload=payload,
    )
    return _sealed_call(context, page_id)


def main(registry_factory=ChairRegistry.from_toml, serving_factory=None) -> int:
    """Plan, ask each call once, publish each reconstruction, then seal.

    Both parameters are test seams. A live chair this pass started is stopped
    in `finally`, before the seal.
    """
    args = stage_parser(DESCRIPTION).parse_args()
    context = open_stage_context(
        args, CONIECTOR, registry_factory=registry_factory, serving_reader=SERVING_READER
    )
    policy = load_reconstruction_policy(args.reconstruction_config)
    context.require_sealed_config("reconstruction", policy.sha256)
    decoding, decoding_sha256 = load_decoding_policy(args.decoding_config)
    context.require_sealed_config("decoding", decoding_sha256)
    plan_entries, shown = diplomatic_entries(context, reading_acts(context))
    plan = plan_payload(policy, plan_entries)
    context.publish(kind=PLAN_KIND, subject_id=PLAN_SUBJECT, outcome="planned", payload=plan)
    chair = None
    try:
        if plan["calls"] and policy.mode == MODE_ON:
            chair = _Chair(
                context, decoding, decoding_sha256, serving_factory or stage_chair_client
            )
        max_tokens = reconstructor_max_tokens(decoding)
        for call in plan["calls"]:
            record = _publish_call(context, chair, call, shown, policy, max_tokens)
            payload = record["payload"]
            _state, answer, _problems = reply_state(
                payload["reply_text"], payload["stop_reason"], call
            )
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
                context.publish(
                    kind=RECONSTRUCTION_KIND,
                    subject_id=reconstruction_subject(derived),
                    outcome=reconstruction_outcome(derived),
                    inputs=[call_ref],
                    payload=derived,
                )
    except ChairResponseRefusal as refusal:
        raise ContractError(f"{type(refusal).__name__}: {refusal}") from refusal
    finally:
        if chair is not None:
            chair.close()
    print(
        f"coniector: mode {policy.mode}, {len(plan['calls'])} call(s)",
        file=sys.stderr,
    )
    context.seal_boundary()
    context.finish()
    return EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
