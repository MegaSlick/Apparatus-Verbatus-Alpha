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
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import operations.serving.errors as serving_errors  # noqa: E402
from common.background_start import BackgroundStart  # noqa: E402
from common.chairs.models import ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.canonical import text_sha256  # noqa: E402
from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.stages import CONIECTOR  # noqa: E402
from common.decoding import load_decoding_policy, reconstructor_max_tokens  # noqa: E402
from common.in_order_window import in_order_window  # noqa: E402
from common.reconstruction import load_reconstruction_policy  # noqa: E402
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
    SUPERSEDES_FIELD,
    call_outcome,
    call_page_id,
    call_prompt,
    derive_reconstructions,
    diplomatic_entries,
    expected_maker,
    fixture_reply,
    generation_attempt,
    plan_chain,
    plan_of,
    plan_payload,
    reconstruction_outcome,
    reconstruction_subject,
    reply_state,
    sealed_generations,
)
from common.replay import not_replayed_problem, open_source, replay_of  # noqa: E402
from common.request_capacity import (  # noqa: E402
    RequestCapacityRefusal,
    reconstruction_request_capacity,
)
from common.retained_replies import unrecorded_replies  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    fixture_serving_details,
    open_stage_context,
    reading_acts,
    refuse_unlive_real_reading,
    run_stage,
    stage_parser,
)
from operations.serving.assembly import (  # noqa: E402
    SERVING_READER,
    bound_serving_recipes,
    launch_row,
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
        self.started = False
        # A start running on a background thread (`begin`), joined by `ready`.
        self.starting = None
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
        self.started = True
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

    def begin(self) -> None:
        """Start the chair on a background thread while the pass draws its calls.

        Refused first, on this thread, when an interrupted pass left a reply no
        record names: asking again would ask its page twice.
        """
        _refuse_unrecorded_replies(self.context)
        self.started = True
        self.starting = BackgroundStart(self.start, self.close, stage="coniector")

    def ready(self) -> None:
        """Have the chair up before the first call is sent: join a background start,
        or start it here when none was begun."""
        starting, self.starting = self.starting, None
        if starting is not None:
            starting.join()
        elif self.client is None:
            _refuse_unrecorded_replies(self.context)
            self.start()

    def settle(self) -> None:
        """Wait for a background start no call waited for, so `close` stops its chair
        before the seal instead of leaving it to the start's thread."""
        starting, self.starting = self.starting, None
        if starting is not None:
            starting.join()

    def close(self) -> None:
        """Stop the chair; one still starting is left to its start's thread, which
        stops it when the start returns, so a stopped pass does not wait for a load."""
        starting, self.starting = self.starting, None
        if starting is not None and starting.abandon():
            return
        try:
            if starting is not None:
                # A start no call waited for still reports its failure.
                starting.join()
        finally:
            client, self.client = self.client, None
            if client is not None:
                client.__exit__()

    def reclaim(self) -> None:
        """Stop the Perlector's chair if it was left serving for this stage and never taken
        over, because this pass sent nothing; a chair this pass started is already closed."""
        if not (self.present and self.live) or self.started:
            return
        client = self.client_factory(
            self.context,
            self.identity,
            self.context.args.placement_tier,
            decoding_policy=self.decoding,
            decoding_config_sha256=self.decoding_sha256,
        )
        reclaimed = client.reclaim_hand_off()
        if reclaimed is not None:
            print(f"coniector: stopped a chair left serving for it: {reclaimed}", file=sys.stderr)

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


def _reclaiming_chair(context, decoding, decoding_sha256, client_factory):
    """The chair of a pass that asks nothing, for `reclaim` only.

    A row that cannot be resolved live (no placement tier, a fixture catalogue) is
    one the Perlector could not have shared a service with, so nothing is reclaimed.
    """
    try:
        return _Chair(context, decoding, decoding_sha256, client_factory)
    except (ContractError, serving_errors.ServingError):
        return _NoChair()


class _NoChair:
    def reclaim(self) -> None:
        return None


def _publish_plan(context, plan: dict) -> bool:
    """Publish the plan, or adopt the last one sealed; whether it superseded an earlier plan.

    A plan that differs from the last sealed one, because an operator re-read
    changed the readings, is published as the next generation naming the plan
    it supersedes; the earlier plans stay as sealed.
    """
    chain = plan_chain(context)
    if chain and plan_of(chain[-1]) == plan:
        return len(chain) > 1
    payload = plan
    if chain:
        payload = {
            **plan,
            SUPERSEDES_FIELD: context.artifact_ref(CONIECTOR, PLAN_KIND, chain[-1]["artifact_id"]),
        }
    context.publish(
        kind=PLAN_KIND,
        subject_id=PLAN_SUBJECT,
        outcome="planned",
        attempt=generation_attempt(PLAN_KIND, PLAN_SUBJECT, len(chain) + 1),
        payload=payload,
    )
    return bool(chain)


def _publish_reconstruction(context, derived: dict, call_ref: dict, replanned: bool) -> None:
    """Publish one reconstruction, or adopt it; after a replan, as its subject's next generation."""
    subject = reconstruction_subject(derived)
    sealed = sealed_generations(context.tree, RECONSTRUCTION_KIND, subject)
    if any(record["payload"] == derived for record in sealed):
        return
    context.publish(
        kind=RECONSTRUCTION_KIND,
        subject_id=subject,
        outcome=reconstruction_outcome(derived),
        attempt=generation_attempt(
            RECONSTRUCTION_KIND, subject, len(sealed) + 1 if replanned else 1
        ),
        inputs=[call_ref],
        payload=derived,
    )


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


def _fixture_asked(chair: _Chair, call: dict, policy) -> dict:
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


def _admit(chair: _Chair, call: dict, text: str, policy, max_tokens: int) -> dict:
    """The live call's admitted capacity, or, as `asked`, why it was not sent."""
    try:
        return reconstruction_request_capacity(
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


def _refuse_unrecorded_replies(context) -> None:
    """Refuse to ask again while a reply an earlier pass retained is bound by no record.

    A pass stopped after a reply arrived and before its call record was published
    leaves that reply in the store; asking its page again would read it twice and
    leave the first answer unrecorded.
    """
    calls, unattributed = unrecorded_replies(context, CONIECTOR)
    if calls or unattributed:
        raise ContractError(
            "an interrupted Coniector pass retained a reply that no reconstruction call "
            "record names; asking again would ask its page twice. Reconstruct in a new run; "
            "the retained reply remains that pass's evidence"
        )


def _send(chair: _Chair, text: str, admitted: dict, what: str):
    """The live call, safe on a worker thread: it publishes nothing, and a failure of
    the call itself is returned, not raised."""
    try:
        return send_chat_request(
            chair.client,
            content=text,
            image_sha256s=[],
            capacity=admitted["capacity"],
            max_tokens=admitted["max_tokens"],
            what=what,
        )
    except _CALL_FAILURES as error:
        return error


def _answered(admitted: dict, result) -> dict:
    """What a sent call left: its reply, or the failure the serving layer observed."""
    if isinstance(result, Exception):
        refs = {
            name: dict(getattr(result, name))
            for name in ("raw_response_ref", "call_record_ref")
            if getattr(result, name, None) is not None
        }
        failure = {
            "code": getattr(result, "code", type(result).__name__),
            "detail": str(result),
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


class _Pass:
    """One Coniector pass's calls: drawn and finished on the main thread, sent in a window."""

    def __init__(self, context, chair, shown, policy, max_tokens: int, replanned: bool) -> None:
        self.context = context
        self.chair = chair
        self.shown = shown
        self.policy = policy
        self.max_tokens = max_tokens
        self.replanned = replanned
        # The source run a replay answers from (`common.replay`), once a live call needs it.
        self._replay = None

    def _replay_source(self):
        """The source run of a replay, or `None` on any other run."""
        if self._replay is None and replay_of(self.context.run) is not None:
            self._replay = open_source(self.context.run)
        return self._replay

    def _recorded(self, page_id: str, call: dict, text: str) -> bool:
        """Whether a replay's source asked exactly this call, in this prompt, and was answered."""
        return any(
            payload.get("call") == call and payload.get("prompt_sha256") == text_sha256(text)
            for payload in self._replay_source().answered_calls(page_id)
        )

    def width(self) -> int:
        """How many calls may be in flight: only a live engine batches, up to the
        width its chair is launched with (the row's, or the capacity plan's)."""
        if self.chair is None or not self.chair.live:
            return 1
        return launch_row(
            self.context, self.chair.identity, self.context.args.placement_tier
        ).max_num_seqs

    def sends_any(self, calls: list) -> bool:
        """Whether some call has no sealed record, so a live chair will be needed.

        Read from the records alone, before any call is drawn: a call refused for
        capacity still counts, so at worst the chair starts for a pass that sends
        nothing, and is stopped with it.
        """
        if self.chair is None or not (self.chair.present and self.chair.live):
            return False
        return any(self._sealed(call) is None for call in calls)

    def _sealed(self, call: dict):
        generations = sealed_generations(
            self.context.tree, CALL_KIND, call_page_id(call, self.shown)
        )
        return next(
            (record for record in generations if record["payload"].get("call") == call),
            None if self.replanned or not generations else generations[0],
        )

    def jobs(self, calls: list):
        for call in calls:
            yield self.draw(call)

    def draw(self, call: dict):
        """One call's job: adopt its sealed record, or prepare its send.

        After a replan (`_publish_plan`), a page whose call the new readings change
        is asked again, as its next generation; the earlier call stays as sealed.
        """
        context, chair = self.context, self.chair
        page_id = call_page_id(call, self.shown)
        text = call_prompt(call, self.shown, self.policy)
        keys = shown_keys(call, self.shown)
        generations = sealed_generations(context.tree, CALL_KIND, page_id)
        sealed = self._sealed(call)
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
                    f"page {page_id}'s retained reconstruction call was asked from another "
                    "plan, other readings or another chair than this run has now; it is not "
                    "adopted. Reconstruct in a new run"
                )
            return None, partial(self.derive, call, sealed)
        finish = partial(self.finish, call, page_id, text, keys, len(generations))
        if not chair.present:
            asked = _not_asked(CHAIR_ABSENT, "no reconstructor chair is configured for this run")
            return None, lambda _result: finish(asked)
        if not chair.live:
            return None, lambda _result: finish(_fixture_asked(chair, call, self.policy))
        admitted = _admit(chair, call, text, self.policy, self.max_tokens)
        if "reply_text" in admitted:
            return None, lambda _result: finish(admitted)
        if self._replay_source() is not None and not self._recorded(page_id, call, text):
            problem = not_replayed_problem(context.run)
            asked = _not_asked(problem["code"], problem["detail"])
            return None, lambda _result: finish(asked)
        chair.ready()
        what = f"the reconstruction of page {call['page_ordinal']}"
        return (
            partial(_send, chair, text, admitted, what),
            lambda result: finish(_answered(admitted, result)),
        )

    def finish(self, call, page_id, text, keys, earlier: int, asked: dict) -> None:
        """Publish one call's record, read it back, and publish its reconstructions."""
        context, chair = self.context, self.chair
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
            attempt=generation_attempt(CALL_KIND, page_id, earlier + 1),
            inputs=_inputs(context, self.shown, keys, asked),
            payload=payload,
        )
        self.derive(call, sealed_generations(context.tree, CALL_KIND, page_id)[-1], None)

    def derive(self, call: dict, record: dict, _result) -> None:
        """Publish, or adopt, every reconstruction of one sealed call record."""
        context = self.context
        payload = record["payload"]
        _state, answer, _problems = reply_state(payload["reply_text"], payload["stop_reason"], call)
        call_ref = context.artifact_ref(CONIECTOR, CALL_KIND, record["artifact_id"])
        for derived in derive_reconstructions(
            call,
            self.shown,
            parse_state=payload["parse_state"],
            answer=answer,
            problems=payload["problems"],
            policy=self.policy,
            call_ref=call_ref,
            maker=payload["maker"],
        ):
            _publish_reconstruction(context, derived, call_ref, self.replanned)


def main(registry_factory=ChairRegistry.from_toml, serving_factory=None) -> int:
    """Plan, ask each call once, publish each reconstruction, then seal.

    Both parameters are test seams. A real submission whose reconstructor row is
    not live is refused before the plan is published, since only a live chair can
    answer it. A live chair this pass started is stopped in `finally`, before the
    seal.
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
    factory = serving_factory or stage_chair_client
    chair = None
    if plan["calls"]:
        chair = _Chair(context, decoding, decoding_sha256, factory)
        refuse_unlive_real_reading(context, chair.identity, chair.serving_mode, stage="Coniector")
    replanned = _publish_plan(context, plan)
    try:
        calls = _Pass(context, chair, shown, policy, reconstructor_max_tokens(decoding), replanned)
        if calls.sends_any(plan["calls"]):
            # The chair loads (or takes over the Perlector's) while the calls are drawn.
            chair.begin()
        in_order_window(calls.width(), calls.jobs(plan["calls"]))
        if chair is not None:
            # Every call may have been refused for capacity, leaving a start nothing
            # waited for: its chair is stopped below, before the seal, not after it.
            chair.settle()
    except ChairResponseRefusal as refusal:
        raise ContractError(f"{type(refusal).__name__}: {refusal}") from refusal
    finally:
        if chair is not None:
            chair.close()
    # Before the seal, like the close above: a chair the Perlector left serving for
    # this stage is stopped here when nothing took it over.
    (chair or _reclaiming_chair(context, decoding, decoding_sha256, factory)).reclaim()
    print(
        f"coniector: mode {policy.mode}, {len(plan['calls'])} call(s)",
        file=sys.stderr,
    )
    context.seal_boundary()
    context.finish()
    return EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
