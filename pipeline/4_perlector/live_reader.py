"""``VLLMReader``: the live implementation of the ``Reader`` protocol
(``reader.py``), behind one ``ChairClient`` (``operations/serving/client.py``,
U2) already entered for this chair's pass.

``FixtureReader`` stands in for an engine this chamber has no pod for.
``VLLMReader`` is the seam a real one occupies: everything downstream --
``run.py``'s orchestration, the Perlectio it publishes, the Recensor that
reads its truncation classification -- is unchanged by which reader answered.

**It inherits the protocol's `pass_kind` restriction exactly.** The module
docstring on ``reader.py`` explains why: ``lectio-nuda`` and ``lectio-prior``
are built from identical dossier arguments and carry the same
``dossier_digest`` and ``rendered_sha256``, so a reader that let its
generation vary with the label rather than the evidence would make
GOVERNANCE 10's contrast measure the pipeline's own routing instead of the
model. ``read`` below reads ``pass_kind`` in exactly two places: the closed
membership check, and the delivery hand-off to ``validate_audit_delivery``.
Nothing else in this module ever inspects it.

**A request that cannot fit the sealed row is refused before it is sent.**
``read`` computes ``common.request_capacity``'s record for the images it is
about to carry, the prompt it just rendered, and this chair's measured
single-act answer budget -- one act's reading is what this request asks for --
and raises
``common.request_capacity.RequestCapacityRefusal`` -- carrying that record --
when the row's ``max_model_len`` cannot hold them. That is a refusal rather
than a hold because a Perlector reading has no ``failed`` shape (see
``EngineSignalRefusal`` below): there is nothing to publish for an act nothing
read. On the admitted path the record travels on the request and the client
copies it onto the retained call record, so the arithmetic sits beside the
reading it allowed. Nothing is ever downscaled to make a request fit.

**The stop-reason mapping is where an unrecognized engine answer becomes a
loud stop, not a silent guess.** ``truncation.py:77-93`` documents the rule
this implements: an engine's own word is authoritative for ``length``, but a
string this seam does not recognize is not folded into either bucket -- it is
refused by name, with the raw response bytes already retained (they are
retained before this reader is ever asked to interpret them --
``ChairClient.read``), so nothing is lost even though the act publishes
nothing this pass.
"""

from __future__ import annotations

import base64
from typing import Any, Mapping

import prompts
from reader import PASS_KINDS, DeliveredPixels, LectioResult, validate_audit_delivery

from common.chairs.models import ChairIdentity
from common.contracts.errors import ContractError
from common.contracts.serving import ENGINE_STOP_COMPLETE, ENGINE_STOP_CUT_OFF
from common.request_capacity import (
    act_answer_budget,
    image_sizes,
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
    (``parse_problem``). A Perlector reading has no ``failed`` shape today --
    unlike a Testimonium, ``outcome="failed"`` is produced nowhere for a
    Perlectio in ``run.py`` -- so a body that is not a reading stops the pass
    loudly rather than minting a shape this section does not own. Nothing is
    lost: the raw bytes are already retained (``ChairClient.read`` retains
    before it parses), named here by ``raw_response_ref`` so the stopped act
    can be traced back to exactly the evidence that stopped it.
    """

    def __init__(self, message: str, *, raw_response_ref: Mapping[str, str]) -> None:
        self.raw_response_ref = dict(raw_response_ref)
        super().__init__(message)


def _data_uri(image_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")


def _image_content_blocks(images: list[bytes]) -> list[dict[str, Any]]:
    return [{"type": "image_url", "image_url": {"url": _data_uri(image)}} for image in images]


def _mapped_stop_reason(
    finish_reason: str | None, *, act_key: object, raw_response_ref: Mapping[str, str]
) -> str | None:
    """The engine's own word, translated into the reader-protocol's closed
    vocabulary (``truncation.py``'s own ``"stop"``/``"length"``/``None``), or
    a named refusal for anything else."""
    if finish_reason is None:
        return None
    if finish_reason in ENGINE_STOP_COMPLETE:
        return "stop"
    if finish_reason in ENGINE_STOP_CUT_OFF:
        return "length"
    raise EngineSignalRefusal(
        f"act {act_key!r} received an engine stop reason {finish_reason!r} this seam does "
        "not recognize (neither a completion nor a length cutoff); the raw response bytes "
        f"are retained at {dict(raw_response_ref)!r}",
        raw_response_ref=raw_response_ref,
    )


class VLLMReader:
    """One Perlector chair's live reading, behind an already-entered
    :class:`~operations.serving.client.ChairClient`.

    ``client`` is entered once by the caller for the whole pass (``run.py``'s
    construction site), never here -- this class issues exactly one reading
    request per :meth:`read` call and never starts, stops, or retries a
    service. ``chair`` is the resolved identity whose ``serving_recipe``
    selects the declared prompt builder (``prompts.build_prompt``);
    ``protocol_config`` is the sealed R5a policy that same builder renders
    through, or ``None`` to fall back to its own default. ``max_tokens`` is
    an explicit run decision, not a sealed one (spec 08 section 3.3: vLLM
    bounds generation by ``max_model_len`` when none is given, so an engine
    ``"length"`` then honestly means the context itself was exhausted).
    """

    def __init__(
        self,
        *,
        client: ChairClient,
        chair: ChairIdentity,
        protocol_config: Mapping[str, str | int] | None,
        max_tokens: int | None,
    ) -> None:
        self._client = client
        self._chair = chair
        self._protocol_config = protocol_config
        self._max_tokens = max_tokens

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
            text = "\n".join([text, *(reproof["prompt"] for reproof in instrument["reproofs"])])

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
        image_sha256s = tuple(
            [ref["sha256"] for view in autopsia["views"] for ref in view["region_refs"]]
            + [ref["sha256"] for view in autopsia["views"] for ref in view["page_render_refs"]]
        )
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

        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        content.extend(_image_content_blocks(region_images + page_render_images))

        # Before the request is built: does it fit the sealed row at all?
        # This is the seam with the most images in one request -- every region
        # crop and every page render, across every capture view
        # (`config/perlector_protocol.toml` allows up to 32, a ceiling with no
        # relation to any row's context) -- so it is also the seam most likely
        # to overrun.  `max_tokens` is deliberately unset here so that a
        # `"length"` stop honestly means the context was exhausted; the cost of
        # that honesty is that a *prompt*-side overrun surfaces as an HTTP 400
        # the engine answers before generating, which `EngineSignalRefusal`
        # never sees. Refusing here is what turns that into a laptop refusal
        # naming the arithmetic rather than a stack trace on a billing card.
        prompt_tokens, basis = perlector_prompt_tokens(text)
        capacity = refuse_unless_it_fits(
            self._client.handle.profile,
            image_sizes(region_images + page_render_images),
            prompt_tokens,
            # One act's reading, which is what this request asks for. A
            # page-fallback act -- an act whose bounds are the whole page --
            # carries a page-sized crop and is caught by its image cost, which
            # dwarfs the difference between the two measured answer budgets.
            act_answer_budget(self._chair.role),
            # The pass label is deliberately absent from this message: this
            # module may read it in exactly two places (the closed membership
            # check and the audit hand-off) so that nothing about a request can
            # vary with which pass it is. A refusal message is no exception.
            what=f"the Perlector request for act {dossier.get('act_key')!r}",
            prompt_tokens_basis=basis,
        )

        request = ChairRequest(
            kind="chat-completions",
            messages=({"role": "user", "content": content},),
            image_sha256s=image_sha256s,
            generation_declared={},
            generation_sent={"max_tokens": self._max_tokens}
            if self._max_tokens is not None
            else {},
            capacity=capacity,
        )
        response = self._client.read(request)

        if response.parse_problem is not None:
            raise EngineSignalRefusal(
                f"the reading response for act {dossier.get('act_key')!r} is not a reading "
                f"({response.parse_problem}); the raw response bytes are retained at "
                f"{dict(response.raw_response_ref)!r}",
                raw_response_ref=response.raw_response_ref,
            )

        stop_reason = _mapped_stop_reason(
            response.finish_reason,
            act_key=dossier.get("act_key"),
            raw_response_ref=response.raw_response_ref,
        )
        result: LectioResult = {"text": response.content, "stop_reason": stop_reason}
        result["engine_call"] = {
            "call_record_ref": dict(response.call_record_ref),
            "raw_response_ref": dict(response.raw_response_ref),
            "response_sha256": response.response_sha256,
            "finish_reason": response.finish_reason,
            "served_model_id": response.served_model_id,
        }
        return result
