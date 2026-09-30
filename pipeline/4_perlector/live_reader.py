"""``VLLMReader``: the live implementation of the ``Reader`` protocol
(``reader.py``), behind one ``ChairClient`` (``operations/serving/client.py``)
already entered for this chair's pass.

``FixtureReader`` stands in for an engine this chamber has no pod for.
``VLLMReader`` is the seam a real one occupies: everything downstream --
``run.py``'s orchestration, the Perlectio it publishes, the Recensor that
reads its truncation classification -- is unchanged by which reader answered.

**It inherits the protocol's `pass_kind` restriction exactly.** The module
docstring on ``reader.py`` explains why: ``lectio-nuda`` and ``lectio-prior``
are built from identical dossier arguments and carry the same
``dossier_digest`` and ``rendered_sha256``, so a reader that let its
request vary with the label rather than the evidence would make
the witness-dependence contrast measure the pipeline's own routing instead of the
model. ``read`` below reads ``pass_kind`` in exactly three places: the closed
membership check, the delivery hand-off to ``validate_audit_delivery``, and
naming the sampling-variance arm, whose only effect is the arm's own sealed
seed (``common.decoding.variance_arm_seed``), recorded on its call record. The
two arms are the same request drawn twice; under one seed they would be one
draw. Nothing else in this module ever inspects it.
The output bound follows the same rule: a reading's bound is one value for every
reading kind, and only a delivered re-proof instrument selects the re-proof's own.

**A request that cannot fit the sealed row is refused before it is sent.**
``read`` computes ``common.request_capacity``'s record for the images it is
about to carry, the prompt it just rendered, and the answer budget
``_reserved_answer_budget`` decides -- one act's reading ordinarily, a dense
page's wherever the act's own crop is as expensive as a whole-page render --
and raises
``common.request_capacity.RequestCapacityRefusal`` -- carrying that record --
when the row's ``max_model_len`` cannot hold them. That is a refusal rather
than a hold because nothing was sent and nothing read: the stage publishes the
act as failed, naming the arithmetic. On the admitted path the record travels on the request and the client
copies it onto the retained call record, so the arithmetic sits beside the
reading it allowed. Nothing is ever downscaled to make a request fit.

**Admission is on an upper bound; the floor travels beside it.** This chair's
prompt is dossier-built, so it has no fixed text to seal a constant against.
A check admitting on a lower bound would admit exactly the requests it should
refuse: a dossier carrying five witnesses' full act texts, or a pass-B prompt
with reproof instruments appended, would pass it and then be answered with the
HTTP 400 the check exists to prevent. ``perlector_prompt_bound`` is the
measured upper bound this reader admits on -- the maximum tokens-per-character
ratio over 168 rendered prompts, with a stated margin, over the measured
chat-template overhead -- and it is sealed against ``prompts.py``'s own module
digest, so editing the
prompt builder expires the measurement rather than leaving a stale rate in
force. The floor is still computed, and is recorded on the capacity record
beside the bound with both bases named, because it is what says a refused
request was refused by a measurement and not by a margin.

**The stop-reason mapping is where an unrecognized engine answer becomes a
loud stop, not a silent guess.** ``common/truncation.py::classify``'s docstring
documents the rule this implements: an engine's own word is authoritative for
``length``, but a
string this seam does not recognize is not folded into either bucket -- it is
refused by name, with the raw response bytes already retained (they are
retained before this reader is ever asked to interpret them --
``ChairClient.read``), so nothing is lost even though the act publishes
nothing this pass.
"""

from __future__ import annotations

import base64
from typing import Any, Mapping

from common import reading_annotations as annotations
import prompts
from reader import PASS_KINDS, DeliveredPixels, LectioResult, validate_audit_delivery

from common.chairs.models import ChairIdentity
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError
from common.contracts.serving import ENGINE_STOP_COMPLETE, ENGINE_STOP_CUT_OFF
from common.cross_capture_autopsia import presented_image_sha256s
from common.decoding import VARIANCE_ARMS
from common.perlector_audit import render_reproof_instruction
from common.request_capacity import (
    act_answer_budget,
    dense_page_answer_budget,
    image_sizes,
    image_token_costs,
    perlector_prompt_bound,
    perlector_prompt_tokens,
    refuse_unless_it_fits,
)
from operations.serving.client import ChairClient, ChairRequest


class EngineSignalRefusal(ContractError):
    """The engine's response cannot be turned into an honest ``LectioResult``.

    Two distinct causes share this refusal, because both leave a Perlector
    reading with no honest text to publish: a ``finish_reason`` this seam does
    not recognize (neither in ``ENGINE_STOP_COMPLETE`` nor
    ``ENGINE_STOP_CUT_OFF``, nor absent), or a response
    :class:`~operations.serving.client.ChairClient` could not parse at all
    (``parse_problem``). The stage publishes such an act as a failed Perlectio
    (``run.py``) rather than a reading, so a body that is not a reading never
    becomes text. Nothing is
    lost: the raw bytes are already retained (``ChairClient.read`` retains
    before it parses), named here by ``raw_response_ref`` so the stopped act
    can be traced back to exactly the evidence that stopped it.
    """

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        raw_response_ref: Mapping[str, str],
        call_record_ref: Mapping[str, str],
        request_sha256: str,
        receipt_ref: Mapping[str, str],
        served_model_id: str,
    ) -> None:
        self.code = code
        self.detail = detail
        self.raw_response_ref = dict(raw_response_ref)
        self.call_record_ref = dict(call_record_ref)
        self.request_sha256 = request_sha256
        self.receipt_ref = dict(receipt_ref)
        self.served_model_id = served_model_id
        super().__init__(f"{code}: {detail}")


def _data_uri(image_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")


def _image_content_blocks(images: list[bytes]) -> list[dict[str, Any]]:
    return [{"type": "image_url", "image_url": {"url": _data_uri(image)}} for image in images]


def _mapped_stop_reason(finish_reason: str | None, *, act_key: object, response: Any) -> str | None:
    """The engine's own word, translated into the reader-protocol's closed
    vocabulary (``common/truncation.py``'s own ``"stop"``/``"length"``/``None``), or
    a named refusal for anything else."""
    if finish_reason is None:
        return None
    if finish_reason in ENGINE_STOP_COMPLETE:
        return "stop"
    if finish_reason in ENGINE_STOP_CUT_OFF:
        return "length"
    raise EngineSignalRefusal(
        "ENGINE_FINISH_REASON_UNRECOGNIZED",
        f"act {act_key!r} received an engine stop reason {finish_reason!r} this seam does "
        "not recognize (neither a completion nor a length cutoff); the raw response bytes "
        f"are retained at {dict(response.raw_response_ref)!r}",
        raw_response_ref=response.raw_response_ref,
        call_record_ref=response.call_record_ref,
        request_sha256=response.request_sha256,
        receipt_ref=response.receipt_ref,
        served_model_id=response.served_model_id,
    )


def _reserved_answer_budget(
    role: str,
    *,
    profile: Any,
    region_sizes: list[tuple[int, int]],
    page_render_sizes: list[tuple[int, int]],
) -> int:
    """How much room this request reserves for the reading it asks for.

    Two facts, and the larger of them wins.

    **One act's reading** is the floor, because that is what a Perlector
    request asks for and reserving a whole page's answer for an ordinary act
    would refuse crops that measurably work.

    **A dense page's reading** replaces it whenever the act's own crop is
    page-sized -- a *page-fallback act*, an act whose bounds are the whole
    page, whose reading is a page of text and costs 1,318 tokens rather than
    216 (`TOKEN_COST_REPORT.md` section 8).  "Page-sized" is decided from the
    same arithmetic the capacity record is built from, and against this
    request's own evidence: a region crop is page-sized when it costs at least
    as much as the cheapest whole-page render delivered beside it.  The two
    other definitions available were both worse.  Comparing against a modelled
    page at the row's ``max_pixels`` would compare the act with a page nobody
    sent; and reading "page-fallback" off a label upstream would let the
    reserve depend on a word rather than on the pixels the chair is charged
    for.  A request that delivers no page render at all has no threshold to
    compare against, so every region counts as page-sized there: reserving
    more can only refuse a request the row could barely have held, and
    reserving less would send one the engine answers with HTTP 400.
    """

    budget = act_answer_budget(role)
    page_render_costs = image_token_costs(profile, page_render_sizes)
    region_costs = image_token_costs(profile, region_sizes)
    page_sized_cost = min(page_render_costs, default=0)
    if any(cost >= page_sized_cost for cost in region_costs):
        budget = max(budget, dense_page_answer_budget(role))
    return budget


class VLLMReader:
    """One Perlector chair's live reading, behind an already-entered
    :class:`~operations.serving.client.ChairClient`.

    ``client`` is entered once by the caller for the whole pass (``run.py``'s
    construction site), never here -- this class issues exactly one reading
    request per :meth:`read` call and never starts, stops, or retries a
    service. ``chair`` is the resolved identity whose ``serving_recipe``
    selects the declared prompt builder (``prompts.build_prompt``);
    ``protocol_config`` is the sealed R5a policy that same builder renders
    through, or ``None`` to fall back to its own default. ``max_tokens`` and
    ``reproof_max_tokens`` are the output caps sealed in the decoding policy
    (their digest is what makes the policy value visible on a run), for a
    reading and for an audit re-proof. What goes on the wire is the smaller of
    the cap and the context the prompt leaves, because the engine refuses a
    request whose prompt plus ``max_tokens`` exceeds ``max_model_len``. A reply
    that reaches it comes back as an engine ``"length"``, which the truncation
    classifier holds as a visible failure of the act, never a re-run. The sent
    value is on the retained call record's ``generation_sent``.
    """

    def __init__(
        self,
        *,
        client: ChairClient,
        chair: ChairIdentity,
        protocol_config: Mapping[str, str | int] | None,
        max_tokens: int,
        reproof_max_tokens: int,
    ) -> None:
        self._client = client
        self._chair = chair
        self._protocol_config = protocol_config
        self._max_tokens = max_tokens
        self._reproof_max_tokens = reproof_max_tokens

    def read(
        self,
        dossier: dict[str, Any],
        *,
        pass_kind: str,
        delivered_pixels: DeliveredPixels | None = None,
        audit_request: dict[str, Any] | None = None,
    ) -> LectioResult:
        if pass_kind not in PASS_KINDS:
            raise ContractError(
                "an unknown Perlector pass kind reached the live reader; a pass this reader "
                "cannot name would be served as the establishing read, not refused"
            )
        instrument = validate_audit_delivery(
            dossier, pass_kind=pass_kind, audit_request=audit_request
        )

        text = prompts.build_prompt(
            self._chair.serving_recipe, self._chair.role, dossier, self._protocol_config
        )
        if instrument is not None:
            # Delivered instrument, not a label (`reader.py`'s own docstring):
            # every reproof prompt the request actually carries, verbatim and
            # in order, and nothing else appended beside them.
            text = "\n".join([text, render_reproof_instruction(instrument)])

        if delivered_pixels is None:
            raise ContractError(
                "the live Perlector reader received a dossier with no delivered pixels; a "
                "live reading cannot show the model images it was never given"
            )
        region_images = list(delivered_pixels.get("region_images", []))
        page_render_images = list(delivered_pixels.get("page_render_images", []))
        # `delivered_pixels` was built by `atomic_delivered_pixels` walking the
        # dossier's own `cross_capture_autopsia` -- region refs across every
        # view, then page-render refs across every view (both already sorted
        # onto that record). `dossier['regions']`/`['page_renders']` sort on
        # `region_id`/`source_page_id` instead, an independent key from a
        # content-addressed `image_path`, so claiming those two lists' order
        # here would name digests in an order the pixels were never sent in --
        # refused half the time by `ChairClient`'s own "exactly and in order"
        # check for any act with more than one region or page render. Walking
        # the same autopsia the same way is the only way the claimed order can
        # ever agree with the sent order.
        autopsia = dossier.get("cross_capture_autopsia")
        if not isinstance(autopsia, dict) or not isinstance(autopsia.get("views"), list):
            raise ContractError(
                "the live Perlector reader received a dossier with no cross-capture autopsia; "
                "the order delivered pixels were sent in cannot be recovered from region_id or "
                "source_page_id order alone"
            )
        if any(
            not isinstance(view, dict)
            or not isinstance(view.get("region_refs"), list)
            or not isinstance(view.get("page_render_refs"), list)
            for view in autopsia["views"]
        ):
            raise ContractError(
                f"act {dossier.get('act_key')!r}: a cross-capture autopsia view does not name "
                "both region_refs and page_render_refs, so the order the delivered pixels were "
                "sent in cannot be recovered"
            )
        image_sha256s = tuple(presented_image_sha256s(autopsia))
        declared_sha256s = [region["image_sha256"] for region in dossier.get("regions", [])] + [
            render["image_sha256"] for render in dossier.get("page_renders", [])
        ]
        if sorted(image_sha256s) != sorted(declared_sha256s):
            raise ContractError(
                f"act {dossier.get('act_key')!r}: the cross-capture autopsia names different "
                "evidence than the dossier's own regions and page_renders -- the dossier and "
                "the presentation it was delivered beside must name the same images, whatever "
                f"order each sorts them in (autopsia {sorted(image_sha256s)!r}, dossier "
                f"{sorted(declared_sha256s)!r})"
            )

        # Page render first, then the prompt text, then the act's own region
        # crops. The page render is the one image shared, byte-identical,
        # across every act on the same page; the region crop is the one image
        # unique to this act.
        # A chat template that renders a message's content parts in list order
        # sees the shared block first and the act-unique block last, which is
        # what gives vLLM's automatic prefix cache the longest run of
        # identical leading tokens across the acts on one page; putting it
        # after the act's own text would invalidate the cache on every act's
        # own rendered dossier even though the page pixels never moved.
        # **Whether the engine's chat template actually
        # renders content in list order, rather than falling back to a
        # string convention that would re-order it regardless, is a fact
        # about the launch argument `common/chair_wire.py` documents
        # (`--chat-template-content-format`, `operations/serving/manager.py`)
        # and cannot be pinned from a request builder** (verified against the
        # pinned `vllm==0.27.1` source: it is resolved once at server
        # construction, never read from the request body). This reorder is
        # therefore a necessary but not sufficient fix; the launch argument is
        # the other half, out of this module's reach.
        # `image_sha256s` below is built in the same order for the same
        # reason `ChairClient` checks it against: the claimed digests and the
        # wire bytes must agree exactly and in order, whatever the dossier's
        # own `regions`/`page_renders` lists sort on.
        content: list[dict[str, Any]] = _image_content_blocks(page_render_images)
        content.append({"type": "text", "text": text})
        content.extend(_image_content_blocks(region_images))

        # Before the request is built: does it fit the sealed row at all?
        # This is the seam with the most images in one request -- every region
        # crop and every page render, across every capture view
        # (`config/perlector_protocol.toml` allows up to 32, a ceiling with no
        # relation to any row's context) -- so it is also the seam most likely
        # to overrun.  A *prompt*-side overrun surfaces as an HTTP 400 the
        # engine answers before generating, which `EngineSignalRefusal` never
        # sees. Refusing here is what turns that into a laptop refusal naming
        # the arithmetic rather than a stack trace on a billing card.
        # Admitted on the measured upper bound, with the measured floor recorded
        # beside it: a request is never let through on a number that says only
        # what it costs *at least*.
        # An audit re-proof answers in JSON and has its own cap; a reading's
        # cap is one value for every reading pass. The two are told apart by
        # the delivered instrument, never by the pass label.
        policy_cap = self._max_tokens if instrument is None else self._reproof_max_tokens
        # A fed prior draft is a model reply that may have looped, so it is
        # charged its bytes, or the reply cap where there is one
        # (`perlector_prompt_bound`).
        prior = dossier["prior_draft"]["text"] if dossier.get("prior_draft_view") == "fed" else ""
        # The neighbour clues are charged one token per byte, which no byte-level
        # tokenizer exceeds.
        neighbours = prompts.neighbour_block(dossier, self._protocol_config)
        prompt_bound, bound_basis = perlector_prompt_bound(
            text,
            template_digest=prompts.BUILDER_SHA256,
            # The block first: a prior draft could repeat a neighbour's reading, and
            # taking it out of the block would leave the block unfound.
            capped_spans=[
                *([(neighbours, None)] if neighbours else ()),
                *([(prior, self._max_tokens)] if prior else ()),
            ],
        )
        prompt_floor, floor_basis = perlector_prompt_tokens(text)
        region_sizes = image_sizes(region_images)
        page_render_sizes = image_sizes(page_render_images)
        capacity = refuse_unless_it_fits(
            self._client.handle.profile,
            region_sizes + page_render_sizes,
            prompt_bound,
            # One act's reading, a whole page's where the act's own crop is
            # page-sized (`_reserved_answer_budget`). The policy cap is not
            # reserved: it is far above an honest reading and would refuse acts
            # that fit.
            _reserved_answer_budget(
                self._chair.role,
                profile=self._client.handle.profile,
                region_sizes=region_sizes,
                page_render_sizes=page_render_sizes,
            ),
            # The pass label is deliberately absent from this message: this
            # module reads it only in the three places the module docstring
            # names, so that nothing else about a request can vary with which
            # pass it is. A refusal message is no exception.
            what=f"the Perlector request for act {dossier.get('act_key')!r}",
            prompt_tokens_basis=bound_basis,
            prompt_tokens_floor=prompt_floor,
            prompt_tokens_floor_basis=floor_basis,
        )

        # The engine refuses prompt + max_tokens over `max_model_len`, so the
        # cap is cut to what the admitted prompt leaves; a reply that then hits
        # it is a visible length stop.
        max_tokens = min(
            policy_cap,
            capacity["max_model_len"] - capacity["image_prompt_tokens"] - capacity["prompt_tokens"],
        )

        request = ChairRequest(
            kind="chat-completions",
            messages=({"role": "user", "content": content},),
            image_sha256s=image_sha256s,
            # The model card declares thinking as its default, but the pinned
            # Perlector instruction asks for the transcription itself.  Keep
            # the vendor generation view empty and record the request-level
            # chat-template selection exactly where ChairClient records what
            # was sent.
            generation_declared={},
            generation_sent={
                "chat_template_kwargs": {"enable_thinking": False},
                "max_tokens": max_tokens,
            },
            capacity=capacity,
            variance_arm=pass_kind if pass_kind in VARIANCE_ARMS else None,
        )
        response = self._client.read(request)

        if response.parse_problem is not None:
            raise EngineSignalRefusal(
                response.parse_problem,
                f"the reading response for act {dossier.get('act_key')!r} is not a reading "
                f"({response.parse_problem}); the raw response bytes are retained at "
                f"{dict(response.raw_response_ref)!r}",
                raw_response_ref=response.raw_response_ref,
                call_record_ref=response.call_record_ref,
                request_sha256=response.request_sha256,
                receipt_ref=response.receipt_ref,
                served_model_id=response.served_model_id,
            )

        stop_reason = _mapped_stop_reason(
            response.finish_reason,
            act_key=dossier.get("act_key"),
            response=response,
        )
        if instrument is None:
            reading, assessment = annotations.read_doubt_marks(response.content)
        else:
            reading, assessment = (
                response.content,
                annotations.not_assessed(
                    "a re-proof answers in JSON; doubt marks are read only from a plain reading"
                ),
            )
        result: LectioResult = {
            "text": reading,
            "stop_reason": stop_reason,
            "assessment": assessment,
        }
        result["engine_call"] = {
            "call_record_ref": dict(response.call_record_ref),
            "raw_response_ref": dict(response.raw_response_ref),
            "response_sha256": response.response_sha256,
            "finish_reason": response.finish_reason,
            "served_model_id": response.served_model_id,
        }
        if instrument is not None:
            result["rendered_prompt"] = text
            result["request_sha256"] = response.request_sha256
        return result


def send_page_request(
    client: ChairClient,
    *,
    images: list[bytes],
    text: str,
    capacity: Mapping[str, Any],
    max_tokens: int,
    what: str,
) -> dict[str, Any]:
    """Send one whole-page reading request and return what the engine answered.

    `images` are the page render and then, when drawn, its overlay, in that
    order; `text` is `page_prompt.build_page_prompt`'s rendered text, sent after
    them; `capacity` is the request-capacity record the request was admitted on
    (`common.request_capacity.page_request_capacity`), copied onto the retained
    call record; `max_tokens` is the admitted output cap. The caller's
    generation is the one a reading sends, thinking off and the cap; the client
    adds the Perlector's sealed sampling row and the serving receipt's seed, as
    for an act reading (attempt 1, no variance arm).

    Returns `{content, stop_reason, finish_reason, request_sha256, engine_call}`:
    `stop_reason` is the engine's word mapped as a reading's (`"stop"`,
    `"length"` or `None`), and an unrecognized word or an unparsed body is
    refused as `EngineSignalRefusal` with the retained bytes named, as a
    reading's is.
    """
    content: list[dict[str, Any]] = _image_content_blocks(images)
    content.append({"type": "text", "text": text})
    request = ChairRequest(
        kind="chat-completions",
        messages=({"role": "user", "content": content},),
        image_sha256s=tuple(digest_bytes(image) for image in images),
        generation_declared={},
        generation_sent={
            "chat_template_kwargs": {"enable_thinking": False},
            "max_tokens": max_tokens,
        },
        capacity=capacity,
    )
    response = client.read(request)
    if response.parse_problem is not None:
        raise EngineSignalRefusal(
            response.parse_problem,
            f"the response to {what} is not a reading ({response.parse_problem}); the raw "
            f"response bytes are retained at {dict(response.raw_response_ref)!r}",
            raw_response_ref=response.raw_response_ref,
            call_record_ref=response.call_record_ref,
            request_sha256=response.request_sha256,
            receipt_ref=response.receipt_ref,
            served_model_id=response.served_model_id,
        )
    return {
        "content": response.content,
        "stop_reason": _mapped_stop_reason(response.finish_reason, act_key=what, response=response),
        "finish_reason": response.finish_reason,
        "request_sha256": response.request_sha256,
        "engine_call": {
            "call_record_ref": dict(response.call_record_ref),
            "raw_response_ref": dict(response.raw_response_ref),
            "response_sha256": response.response_sha256,
            "finish_reason": response.finish_reason,
            "served_model_id": response.served_model_id,
        },
    }
