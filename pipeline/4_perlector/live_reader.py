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
        protocol_config: dict[str, str] | None,
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
        image_sha256s = tuple(
            [region["image_sha256"] for region in dossier.get("regions", [])]
            + [render["image_sha256"] for render in dossier.get("page_renders", [])]
        )

        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        content.extend(_image_content_blocks(region_images + page_render_images))

        request = ChairRequest(
            kind="chat-completions",
            messages=({"role": "user", "content": content},),
            image_sha256s=image_sha256s,
            generation_declared={},
            generation_sent={"max_tokens": self._max_tokens}
            if self._max_tokens is not None
            else {},
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
