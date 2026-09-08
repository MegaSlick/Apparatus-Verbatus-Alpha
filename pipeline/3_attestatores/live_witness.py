"""Request builders and response derivation for the live Attestatores boundary.

U5 of the live reading seam (`SPEC_A.md` section 2.2). This module owns exactly
the seam between an already-issued :class:`~operations.serving.client.ChairResponse`
and the ``Attempt``-shaped facts `pipeline/3_attestatores/run.py::resolve_attempt`
already derives from a retained recordable response. It never wires a pass, never
schedules a chair, and never touches ``run.py`` -- that is U6's job.

**Where this deliberately reads narrower than `SPEC_A.md` section 2.2, and why:**

1. ``act_chair_request``/``page_chair_request`` take a ready-made ``presentation``
   (the exact dict shape ``run.py``'s ``presentation_for_region``/
   ``presentation_for_page`` already build) rather than an ``act``/``region``/
   ``page_ordinal`` and computing it themselves. ``run.py`` is U6's file and this
   module must never import it -- U6 imports *this* module, so the reverse import
   would be circular. Duplicating those two pure builders here instead would give
   two copies of "how a presentation is built" that can silently drift; a
   single ready-made argument keeps one source of truth in run.py and one
   consumer here.
2. ``live_attempt_from_response``/``captured_page_attempt`` retain
   ``response.content.encode("utf-8")``, not ``response.raw_response``, as the
   adapter's native bytes. The roadmap's prose named ``response.raw_response``,
   but the landed ``operations.serving.client.ChairResponse`` (U2) defines
   ``raw_response`` as the *entire* HTTP response body -- the OpenAI JSON
   envelope -- while every adapter's native parser (``feeding.validate_dai_text``,
   ``churro.parse``, ``chandra.parse``) expects the
   model's own output bytes, exactly as the fixture declares them
   (``row["raw_xml"]``, a fixture Chandra placeholder body). ``response.content`` is
   the field ``operations.serving.http.parse_openai_reading`` already extracted
   for exactly this purpose. Retaining the envelope here would hand every
   native parser JSON it cannot read and turn a working reading into a bogus
   parse failure. The full envelope is not lost: ``ChairClient.read`` already
   retained it before parsing. It survives two distinct ways depending on the
   branch, and the two must not be read as one kind of evidence: on the
   malformed branch (``_malformed_response_attempt``), ``LiveAttempt.raw_response_ref``
   *is* the envelope, taken straight from ``response.raw_response_ref``; on
   every parsed branch, ``LiveAttempt.raw_response_ref``/``native_capture["raw_response_ref"]``
   is the adapter's own native output bytes (``response.content``, retained by
   ``adapter.retain``), and the envelope instead survives only through
   ``LiveAttempt.call_record_ref`` -> the call record's own ``raw_response_ref``
   field (``operations/serving/client.py``). Landed code wins over the sketch;
   this is the seam where they disagreed. Every branch therefore also sets
   ``LiveAttempt.raw_response_kind``, so the published record *says* which of
   the two it holds rather than leaving a reader to infer it from whether some
   other optional field happens to be present
   (``common.contracts.serving.RAW_RESPONSE_KINDS``).
3. A wire body ``ChairClient`` could not parse at all (``response.parse_problem``
   is not ``None``) produces ``LiveAttempt.native_capture = None``: no adapter
   ever ran, so there is no adapter-shaped capture to retain, only the envelope
   `_malformed_response_attempt`` already carries forward on ``raw_response_ref``.
   `SPEC_A.md` section 2.3's "``native_capture`` ... required on every live
   attempted record" reads, at this boundary, as "every live attempted record
   whose bytes reached an adapter parser" -- a malformed body never did. U6 must
   carry this reading forward rather than treat a bare ``None`` here as a bug to
   paper over with a fabricated capture. It is also the only branch whose
   ``raw_response_kind`` is ``transport-response-body``, which is the record's
   own way of saying the same thing.

**The generation split** (DAI, act-scoped): ``feeding.dai_generation()`` is
DAI's *carried* HuggingFace ``generation_config.json`` -- ``do_sample``,
``temperature``, three token-id fields, and a library version string sit
beside the three values vLLM's OpenAI-compatible endpoint actually accepts as
extra decoding parameters (``repetition_penalty``, ``top_k``, ``top_p``).
``generation_declared`` retains the whole carried view as evidence;
``generation_sent`` is built by *allow-listing* those three fields rather than
a "minus" subtraction, so a future carried key nobody has named yet defaults
to *not* being sent (GOVERNANCE 7: never send a vendor value silently) instead
of leaking onto the wire by omission from a denylist.

**Every chair's generation bound is now decided by one rule, against the
sealed row.** ``common/request_capacity.py::sendable_max_tokens`` holds it:
``min(the chair's declared upstream bound, max_model_len - image tokens -
prompt tokens)``, both terms read off *this request's own* capacity record.
``generation_bound_sent`` below is this module's name for it, used at all three
witness builders; ``pipeline/2_designator/structure_pass.py`` calls the shared
function directly for the fourth chair.

This generalises what used to be Churro's rule alone -- DAI and Chandra sent no
bound at all, which is not the same as being unbounded: with no ``max_tokens``
the engine sets the answer budget to ``max_model_len - prompt`` itself, so a
DAI act crop was free to generate some 7,700 tokens where its own publisher
runs it at 1,024.

**The row term is expressed by sending no field, and the vendor's term by
sending one.** The prompt cost this seam can measure is a floor -- vLLM's own
assembly has never been observed to agree with it -- so putting the row term on
the wire would turn a one-token undercount into an HTTP 400 before generation,
on a card that bills by the hour. Sending nothing cannot fail that way and is
exactly what the engine already did. A value therefore goes out only where the
*declared* bound is strictly smaller than what the row leaves, which is the
only case where the vendor's number changes anything -- and the gap between the
two terms is then also the margin against an undercount, thousands of tokens
wide at every shipped row. A ``"length"`` stop means the vendor's bound
wherever one was sent and the context wherever none was, and the retained
chair-call record's ``generation_sent`` says which.

**Whether a request fits is a different question from what may be sent, and it
is asked of every chair here.** The paragraph above bounds Churro's generation;
it cannot say whether the request the engine receives is admissible at all. A
whole 300-dpi page costs Chandra 1,715 prompt tokens and Churro 2,280 at the
smallest tier's ``max_pixels``, before a word of prompt is counted. **A
page-fallback act does not hand DAI a page-sized crop**: `config/
designator_grouping.toml`'s ``fallback_bands`` divides the page into four
horizontal bands, and a fallback act is assigned one band
(``regions_by_act[act_id][0]``), roughly 2,550x927 at 300 dpi before DAI's own
``crop-resize-preserve-aspect`` recipe brings it down toward its 1,500-px
width cap (roughly 1,500x545) -- cheaper than a whole page, not equal to one.
Every fallback-act crop is still checked against the sealed row exactly like
any other DAI request; nothing here special-cases it because it no longer
costs what a whole page would.
``request_capacity_or_refuse`` computes that from the sealed row's own
``min_pixels``/``max_pixels``/``patch_size``/``merge_size``
(``common/request_capacity.py``), the chair's measured prompt cost, and its
measured answer budget at the scope it was asked at, and refuses by name before
the request is built. That is not the "guessed reservation" the paragraph above
declines: ``smart_resize`` is deterministic integer arithmetic over numbers the
row states, and the prompt and answer costs are measured values bound to the
text and the response shape they were measured over -- nothing here estimates.
The record travels on the admitted request and the client copies it onto the
retained call record.

**DAI's closed model view**: ``dai.v1`` is act-scoped and, unlike Churro or
Chandra, ``feeding.retain_model_view`` closes its ``view`` to DAI's own schema
(``feeding.validate_dai_model_view``) rather than accepting an open dict.
``live_attempt_from_response`` builds that closed view with
``feeding.dai_model_view`` -- the same builder the sealed DAI record already
uses -- from the act's presentation, the adapter's published crop, its exact
prompt text, and its declared generation config, retaining the prompt and
generation-config bytes as blobs so every reference in the closed view names
real, digest-checked bytes. The no-resize case is what that builder's
identity-transform rule governs, and it is satisfied here by *content*: DAI's
``adapter-crop`` on that path is ``crop_png`` of the same sealed page at the
same bounds as the Designator's own proposal crop, so the two references carry
one digest under two stage-owned paths. ``feeding.dai_model_view`` compares
the digest for exactly that reason (see its docstring); held to the whole
reference dict, as it once was, it refused every genuine no-resize DAI act
after the response had already come back.

**Confirming a blank response**: ``genuinely-empty`` is the one completed
outcome whose whole content is an absence (`run.py`'s own history names why
that makes it the one outcome most easily minted from something other than
evidence). This module confirms it only when the transport word is a
recognized *natural* completion (``completed is True``); an empty response
whose stop word is a recognized cut-off, unreported entirely, or unrecognized
(``completed`` is ``False`` or ``None``) is held as ``failed`` instead --
GOVERNANCE 10 forbids defaulting any of those into "the provider finished
naturally." This applies on both the page-scoped and act-scoped paths alike;
a naive read of `run.py::resolve_attempt`'s own fixture-path parity, which has
no transport stop reason to reason about at all, would have kept this module's
act path exempt, but there was no parity there to keep.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final, Mapping

import feeding
import witness_adapters

from common.chair_wire import chandra_wire_fields
from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal
from common.contracts.serving import (
    ENGINE_STOP_COMPLETE,
    ENGINE_STOP_CUT_OFF,
    RAW_RESPONSE_MODEL_OUTPUT,
    RAW_RESPONSE_TRANSPORT_BODY,
    STOP_REASON_UNREPORTED,
)
from common.contracts.stages import ATTESTATORES
from common.imaging import dimensions
from common.native_witness import native_parse_refusal
from common.request_capacity import (
    act_answer_budget,
    dense_page_answer_budget,
    refuse_unless_it_fits,
    sealed_prompt_tokens,
    sendable_max_tokens,
)
from operations.serving.client import ChairRequest, ChairResponse

# The old blanket value every live attempt used to record, before an adapter
# could declare its own. Re-exported from `witness_adapters` (the adapter
# layer, not `common/contracts/outcomes.py`'s algebra: this is a fact about
# one adapter's declared grammar, not a term `check_algebra_is_total` closes
# over) rather than duplicated as a separate literal that could drift from it.
DEFAULT_FORMAT_CAPABILITIES: Mapping[str, bool] = witness_adapters.FALLBACK_FORMAT_CAPABILITIES


def _format_capabilities_for(adapter: Any) -> dict[str, bool]:
    """What this adapter's own grammar can carry, or the old blanket default.

    A live witness never self-reports a format capability from the response
    itself -- `ChairResponse` carries none -- so this is a fact about the
    *adapter's grammar*, not about any one reply. Thin wrapper over
    `witness_adapters.declared_format_capabilities`, the one place this
    validation lives: it used to be duplicated here and in
    `run.py::_declared_format_capabilities` (same body, same docstring,
    naming the other as a "mirror"), which is a fact this seam should not have
    to keep in sync by hand with a sibling stage's copy of it.
    """
    return witness_adapters.declared_format_capabilities(adapter)


# The allow-listed subset of `feeding.dai_generation()` vLLM's OpenAI-compatible
# endpoint accepts as extra decoding parameters. Everything else in that carried
# view -- `do_sample`, `temperature`, the three token-id fields, and
# `transformers_version` -- is retained evidence (`generation_declared`) but
# never sent (GOVERNANCE 7's "the pipeline does not gate model behaviour" cuts
# the other way here too: it also never silently *substitutes* its own
# understanding of a vendor field for the vendor's declared value).
_DAI_GENERATION_SENT_KEYS = ("repetition_penalty", "top_k", "top_p")


@dataclass(frozen=True, slots=True)
class LiveAttempt:
    """One live chair's resolved outcome for one request -- `Attempt`'s live twin.

    Field-for-field compatible with `run.py::Attempt` on every name that
    exists there (``outcome``, ``native_payload``, ``witness_reported``,
    ``format_capabilities``, ``health``, ``reason``, ``raw_response_ref``,
    ``observation_payload``) so U6's conversion to the real `Attempt` is a
    rename, not a remap. ``observation_payload`` is populated only on
    Chandra's parsed-and-accepted branch (`run.py` reads it, preferring it
    over ``native_payload``, to feed ``adapter.observe`` for page geometry);
    every other branch leaves it ``None``, matching `Attempt`'s own default.
    The three trailing fields are live-only: `SPEC_A.md` section 2.3 admits
    ``native_capture`` on every live attempted record whose bytes reached an
    adapter parser (deviation 3 above) and a new ``serving_call_ref`` field
    sourced from ``call_record_ref``; ``receipt_ref`` is what
    ``provenance_for(receipt_ref=...)`` needs to stop writing
    ``fixture_serving_details``.
    """

    outcome: str
    native_payload: Any
    witness_reported: Any
    format_capabilities: Mapping[str, bool] | None
    health: dict[str, Any]
    reason: str | None
    raw_response_ref: Mapping[str, str] | None
    native_capture: Mapping[str, Any] | None
    call_record_ref: Mapping[str, str] | None
    receipt_ref: Mapping[str, str] | None
    # Which sort of bytes ``raw_response_ref`` names on this branch --
    # ``model-output`` wherever an adapter parsed, ``transport-response-body``
    # on the one branch where none could. ``None`` only when nothing was
    # retained at all.
    raw_response_kind: str | None = None
    observation_payload: Any = None


@dataclass(frozen=True, slots=True)
class ActChairRequest:
    """One DAI request, plus the exact presented crop and prompt it came from.

    ``live_attempt_from_response`` needs ``presented``'s own image reference
    and the exact prompt text to build DAI's closed model view once the
    response comes back. Both are already computed here by
    ``adapter.present``/``adapter.prompt``; carrying them forward keeps
    ``adapter.present`` -- a real crop and resize -- from running a second
    time for the same act.
    """

    request: ChairRequest
    presented: Mapping[str, Any]
    prompt: Mapping[str, Any]
    capacity: Mapping[str, Any]


def _data_uri(image_bytes: bytes) -> str:
    import base64

    return "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")


def _user_content(text: str, image_bytes: bytes) -> list[dict[str, Any]]:
    """One user turn's content parts: **the image first, then the text.**

    Every chat template these three chairs ship emits a message's content parts
    in the order the list gives them, so this order *is* the token sequence the
    model sees. All three occupants were fine-tuned with the vision block
    before the instruction, and this repository sent the reverse until now:

    * DAI -- the model card's own inference snippet builds
      ``[image, text]``, and its ~37k fine-tuning examples are one fixed
      framing seen by a rank-8 LoRA. The old pipeline
      (``remote/pilot_crops_dai.py``) sent image-first; the rebuild inverted
      it, so this is a regression against upstream *and* against our own prior
      working system.
    * Chandra -- ``chandra/model/vllm.py`` appends the image block before the
      text prompt, and ``model/hf.py`` does the same.
    * Churro -- its provider builds the image part first.

    (``workbench/raw/research-2026-09-06/``, all three reports; verified by the
    host against the tree and the vendor sources.)

    **No token count moves with this.** The measured prompt constants
    (``common/request_capacity.py``) are taken over the message *texts* and
    sealed against a digest of those texts in order; an image part carries no
    text and is not part of that digest, and ``smart_resize``'s image cost is a
    function of pixels alone. What moves is only which of the two blocks the
    template emits first.
    """

    return [
        {"type": "image_url", "image_url": {"url": _data_uri(image_bytes)}},
        {"type": "text", "text": text},
    ]


def _system_content(text: str) -> list[dict[str, str]]:
    """One system turn's content: a single-element list of ``{type: text}`` parts.

    DAI's README and Churro's own ``HFChatTemplate.build_conversation`` both
    render a system message this way -- a one-element list, not the bare
    string this seam sent until now. `common/test_vendor_parity.py`'s request
    shape test (Wave 1, U8) is what pins this against the vendors' own shape;
    here it is one small helper so both builders below carry it identically
    rather than as two literals that can drift.  No token count moves with
    this: the measured prompt constants are taken over the message *texts*,
    and a plain string versus a one-element list of one text part carries
    the same text either way -- what moves is only the JSON shape the engine's
    chat template sees.
    """

    return [{"type": "text", "text": text}]


def _presented_image_bytes(context: Any, presented: Mapping[str, Any]) -> bytes:
    """Read back exactly the bytes an adapter's own presentation names.

    ARCHITECTURE invariant 3: the exact image shown must be reproducible from
    what was recorded. `adapter.present` may publish an image of its own --
    DAI's act crop-and-resize, Chandra's whole-page vendor `scale_to_fit` --
    and only Churro binds the bytes it was handed. Reading the bytes back by
    the path the presentation names and checking the digest it names is what
    makes that claim checked here rather than assumed.
    """

    image_bytes = context.tree.read_bytes(presented["image_path"])
    actual = digest_bytes(image_bytes)
    if actual != presented["image_sha256"]:
        raise SchemaRefusal(
            "an adapter's presented image bytes do not match its own declared digest: "
            f"expected {presented['image_sha256']}, read {actual}"
        )
    return image_bytes


# Which numbered chair each adapter name occupies.  The measured prompt and
# answer costs are per *chair*, because that is what the serving row and the
# receipt are keyed on; the adapter name is what this seam is handed.
_ADAPTER_CHAIRS: Mapping[str, str] = {
    "chandra.v1": "attestator_1",
    "dai.v1": "attestator_2",
    "churro.v1": "attestator_3",
}


def _prompt_texts(prompt: Mapping[str, Any]) -> tuple[str, ...]:
    """One adapter's prompt as the ordered text parts its request will carry."""

    if set(prompt) == {"system", "user"}:
        return (prompt["system"], prompt["user"])
    if set(prompt) == {"user"}:
        # Chandra's shape: the vendor's own `OCR_LAYOUT_PROMPT` bytes as a
        # single user turn with no system message, which is what
        # `chandra/model/vllm.py` builds. One text part to measure.
        return (prompt["user"],)
    if set(prompt) == {"system"}:
        # Churro's registry-fixed shape (design's "Churro {system}"): the
        # whole instruction is the system turn and the user turn carries no
        # text of its own, so there is exactly one text part to measure.
        return (prompt["system"],)
    raise SchemaRefusal(
        f"a witness adapter returned an unrecognized prompt shape {sorted(prompt)}; this seam "
        "knows the churro.v1/dai.v1 system/user framing, churro.v1's system-only framing, "
        "and chandra.v1's single user turn"
    )


def request_capacity_or_refuse(
    profile: Any,
    adapter_name: str,
    prompt: Mapping[str, Any],
    image_bytes_list: list[bytes],
    *,
    scope: str,
    what: str,
) -> dict[str, Any]:
    """Whether this witness request fits the sealed row, refused by name if not.

    Asked before the request is built, so a request the engine would answer
    with HTTP 400 never reaches a billing card.  The image cost is read off the
    bytes this request will actually embed -- the adapter has already cropped
    and resized by this point, and DAI's own ceilings bind before the row's --
    so the count is of the pixels the chair is really charged for.

    The prompt cost is the measured constant for this chair, bound to a digest
    of the exact prompt text (`common/request_capacity.py`: no tokenizer is
    available offline here).  The answer budget is that chair's own measured
    response *at the scope it was asked at*: a page chair reserves a dense
    800-word page's answer, because a row that cannot hold one cannot witness a
    real register page; an act chair reserves one act's answer, because
    reserving a page's would refuse ordinary act crops that measurably work.

    Never a silent downscale: the alternative to refusing is showing the model
    fewer pixels than `config/pdf_render.toml` argues are needed to read the
    ink, which is a reading-quality decision this seam does not own.
    """

    chair = _ADAPTER_CHAIRS.get(adapter_name)
    if chair is None:
        raise SchemaRefusal(
            f"witness adapter {adapter_name!r} occupies no chair this seam can name, so its "
            f"request cannot be checked against a serving row; the named adapters are "
            f"{sorted(_ADAPTER_CHAIRS)}"
        )
    budget = dense_page_answer_budget if scope == "page" else act_answer_budget
    return refuse_unless_it_fits(
        profile,
        [dimensions(image) for image in image_bytes_list],
        sealed_prompt_tokens(chair, *_prompt_texts(prompt)),
        budget(chair),
        what=what,
    )


def act_chair_request(
    context: Any, adapter: Any, presentation: Mapping[str, Any], *, profile: Any
) -> ActChairRequest:
    """Build one act-scoped (DAI) reading request from an act's proposal presentation.

    ``presentation`` is exactly what `run.py::presentation_for_region` returns
    for the act's one proposal region; ``adapter.present`` is DAI's own
    crop-and-resize step (`witness_adapters._dai_present`), which publishes and
    returns the adapter-owned image this request actually embeds.

    ``profile`` is the sealed serving row this chair runs under
    (``ChairClient.handle.profile``).  A page-fallback act -- an act whose only
    proposal region comes from the Designator's page-level ``fallback-tiles``
    disposition rather than ordinary grouping -- is part of why this check
    exists: its crop is one fallback band (``config/designator_grouping.toml``'s
    ``fallback_bands``, roughly 2,550x927 at 300 dpi before DAI's own resize),
    not a whole page, but it is still real pixels this seam must weigh against
    the row rather than assume away.
    """

    presented = adapter.present(context, dict(presentation))
    image_bytes = _presented_image_bytes(context, presented)
    prompt = adapter.prompt()
    capacity = request_capacity_or_refuse(
        profile,
        "dai.v1",
        prompt,
        [image_bytes],
        scope="act",
        what=f"the dai.v1 request for region {presentation.get('region_ref')!r}",
    )
    messages = (
        {"role": "system", "content": _system_content(prompt["system"])},
        {"role": "user", "content": _user_content(prompt["user"], image_bytes)},
    )
    generation_declared = feeding.dai_generation()
    generation_sent = {
        key: generation_declared[key]
        for key in _DAI_GENERATION_SENT_KEYS
        if key in generation_declared
    }
    generation_sent.update(generation_bound_sent(_ADAPTER_CHAIRS["dai.v1"], capacity))
    # The second EOS id the engine never reads off the model's own file
    # (`feeding.dai_wire_stop_token_ids`).
    generation_sent.update(feeding.dai_wire_stop_token_ids())
    request = ChairRequest(
        kind="chat-completions",
        messages=messages,
        image_sha256s=(presented["image_sha256"],),
        generation_declared=generation_declared,
        generation_sent=generation_sent,
        capacity=capacity,
    )
    return ActChairRequest(request=request, presented=presented, prompt=prompt, capacity=capacity)


def generation_bound_sent(chair: str, capacity: Mapping[str, Any]) -> dict[str, Any]:
    """The ``max_tokens`` this chair's request carries, from the sealed row.

    ``common.request_capacity.sendable_max_tokens`` holds the rule and the
    declared bounds; this is the Attestatores-local name for it, kept so the
    three builders below read the same as the Designator's own call site and so
    an adapter name is never what decides a bound. Returns ``{}`` where the
    sealed row is what binds -- see that function for why the row term is
    expressed by sending no field rather than by sending our own count of it.
    """

    return sendable_max_tokens(chair, capacity)


#: The adapters this seam knows how to build a page request for and capture a
#: page response from. It is named in `captured_page_attempt`'s refusal, which
#: is the one place the set is *asked* a question: every branch after that
#: refusal already knows it holds a page adapter, so a membership test there
#: would be a second, quieter copy of this list rather than a check.
_PAGE_SCOPED_ADAPTERS: Final = frozenset({"churro.v1", "chandra.v1"})


def _framed_prompt(adapter: Any, framing: str | None) -> Mapping[str, Any]:
    """One adapter's prompt, under the framing this run resolved for its chair.

    ``None`` means the adapter has one framing and there is nothing to name
    (``witness_adapters.framing_for``); the adapter is then asked exactly as it
    always was, so an adapter with a single prompt keeps a zero-argument
    ``prompt()`` and no call site has to know which kind it holds.
    """

    return adapter.prompt() if framing is None else adapter.prompt(framing)


def _page_messages(
    adapter_name: str, prompt: Mapping[str, Any], image_bytes: bytes
) -> tuple[Mapping[str, object], ...]:
    """The message tuple for one page-scoped request, dispatched on prompt shape.

    Two shapes, closed and exact -- a third is refused rather than guessed at.
    Extracted from `page_chair_request` so this dispatch (and, in particular, a
    new shape) is provable without a sealed row or a measured prompt-token
    constant standing in the way: it is pure, and both of its branches are
    decided by ``set(prompt)`` alone.

    * ``{"user"}`` -- Chandra's framing (`chandra.prompt`): a single user turn
      carrying the image and the vendor's own ``OCR_LAYOUT_PROMPT`` bytes, with
      no system message at all -- the shape `chandra/model/vllm.py:64-76`
      builds.
    * ``{"system"}`` -- Churro's framing (`churro.prompt`): the whole
      instruction sits in the system turn and the user turn carries the image
      alone -- both attested profiles set ``user_prompt``/``user_message_text``
      to ``None``, so there is no vendor user text to send, and
      `templates/hf.py::HFChatTemplate.build_conversation` builds exactly this.

    The ``{"system", "user"}`` shape this seam also knew is gone from the page
    path with the modified carry that produced it: neither page chair is asked
    in two messages any more. `_prompt_texts` still knows it, because DAI is,
    and DAI is act-scoped and never reaches here.

    Every system turn is sent as a one-element list of ``{type: text}`` parts
    (`_system_content`), the vendors' own shape for DAI and Churro alike.
    """

    if set(prompt) == {"user"}:
        return ({"role": "user", "content": _user_content(prompt["user"], image_bytes)},)
    if set(prompt) == {"system"}:
        return (
            {"role": "system", "content": _system_content(prompt["system"])},
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": _data_uri(image_bytes)}}],
            },
        )
    raise SchemaRefusal(
        f"page-scoped adapter {adapter_name!r} returned an unrecognized prompt shape "
        f"{sorted(prompt)}; this seam knows churro.v1's system-only framing and chandra.v1's "
        "single user turn"
    )


def page_chair_request(
    context: Any,
    adapter: Any,
    adapter_name: str,
    presentation: Mapping[str, Any],
    *,
    profile: Any,
    framing: str | None = None,
) -> ChairRequest:
    """Build one page-scoped (Churro or Chandra) reading request from a whole page.

    ``presentation`` is exactly what `run.py::presentation_for_page` returns,
    and the two page-scoped adapters do **not** treat it alike. `churro.present`
    binds the image it was given unchanged; `chandra.present` runs the vendor's
    own `scale_to_fit` over a whole-page presentation and publishes the resized
    blob (`chandra-scale-to-fit.v1`), so a 2480x3508 sealed page reaches the
    wire as 2100x2968 under a different digest. Everything below therefore
    reads the bytes, the digest and the size off what ``adapter.present``
    returned, never off ``presentation``.

    ``profile`` is the sealed serving row this chair runs under
    (``ChairClient.handle.profile``). Two separate questions are asked of it,
    and they are not the same question. **What may be sent** is asked for both
    page chairs alike, through `generation_bound_sent` against this request's
    own capacity record: a ``max_tokens`` goes out only where the chair's
    declared bound is strictly below what the row leaves, so the adapter's name
    decides nothing here and the row decides everything. At every row in the
    shipped real catalogue that leaves both declarations -- Chandra's 12,384
    and Churro's 20,000 -- above the room a page and its prompt leave, so
    neither page chair sends a bound today; that is a fact about the catalogue,
    not about either adapter, and a longer-context row would change it without
    a line moving here. **Whether the request fits** is asked before either is
    built: a whole 300-dpi page costs 1,715 image tokens on Chandra and 2,280
    on Churro at the smallest tier's `max_pixels` -- Chandra's vendor resize
    does not move that number, because 2100x2968 is still over that
    `max_pixels` and the engine's own `smart_resize` caps both sizes to the
    same grid -- and neither fitted the 2,048-token rows this catalogue shipped
    before the contexts were raised, with a prompt and an answer beside them.
    Sending no bound does not make a request short enough; it only means
    nothing here could have shortened it.
    """

    presented = adapter.present(context, dict(presentation))
    image_bytes = _presented_image_bytes(context, presented)
    prompt = _framed_prompt(adapter, framing)
    image_sha256s = (presented["image_sha256"],)
    messages = _page_messages(adapter_name, prompt, image_bytes)
    # After the prompt shape is recognized, so an unknown adapter framing keeps
    # its own refusal, and before any generation bound is decided: whether the
    # request fits is a different question from what may be sent, and the
    # answer to the first does not depend on the second.
    capacity = request_capacity_or_refuse(
        profile,
        adapter_name,
        prompt,
        [image_bytes],
        scope="page",
        what=f"the {adapter_name} request for page {presentation.get('source_page_ordinal')!r}",
    )
    # Both page chairs carry a vendor generation view to retain as evidence:
    # Churro's `max_new_tokens` (`feeding.churro_generation`) and Chandra's own
    # `chandra/settings.py::MAX_OUTPUT_TOKENS` (`feeding.chandra_generation`).
    # Neither is by itself what goes on the wire -- `generation_bound_sent`
    # below sends a value only where the declared bound is what binds, strictly
    # below what the sealed row leaves. Beside them go the fields each occupant
    # needs that `generation_config = "vllm"` would otherwise decide for it:
    # Churro's own shipped repetition penalty, and Chandra's thinking-mode flag.
    #
    # Dispatched by name and **total**, so a third page adapter is refused here
    # rather than quietly handed the other one's vendor fields. The prompt-shape
    # branch above cannot stand in for this: it recognizes a *shape*, and a new
    # adapter sharing Churro's two-message shape would have fallen through to
    # Chandra's flag.
    generation_declared: dict[str, Any]
    if adapter_name == "churro.v1":
        generation_declared = dict(feeding.churro_generation())
        wire_fields: dict[str, Any] = dict(feeding.churro_wire_decoding())
    elif adapter_name == "chandra.v1":
        generation_declared = dict(feeding.chandra_generation())
        wire_fields = chandra_wire_fields()
    else:
        raise SchemaRefusal(
            f"page-scoped adapter {adapter_name!r} has no declared generation view at this "
            f"seam; the page-scoped adapters are {sorted(_PAGE_SCOPED_ADAPTERS)}"
        )
    # The capacity record above already counted this exact request's image and
    # prompt against this exact row, so the sendable bound is decided against
    # the prompt that will sit beside it rather than against the row alone.
    generation_sent = generation_bound_sent(_ADAPTER_CHAIRS[adapter_name], capacity)
    generation_sent.update(wire_fields)
    return ChairRequest(
        kind="chat-completions",
        messages=messages,
        image_sha256s=image_sha256s,
        generation_declared=generation_declared,
        generation_sent=generation_sent,
        capacity=capacity,
    )


def _finish_reason_facts(response: ChairResponse) -> tuple[str, bool | None, bool | None]:
    """``(transport_stop_reason, completed, cut_off)`` from one retained response.

    `SPEC_A.md` section 1.6: the transport word travels verbatim, defaulted
    only to the literal absence marker, never to a meaning. ``completed`` and
    ``cut_off`` are each ``True``/``False`` only when the transport word
    positively says so (a recognized natural stop, or a recognized cut-off
    word); an absent ``finish_reason`` and an engine word this system does not
    recognize both leave *both* as ``None`` rather than guessing which bucket
    they belong in -- GOVERNANCE 10: an unread engine signal is never defaulted
    to a meaning, including the meaning "not cut off."
    """

    finish_reason = response.finish_reason
    if finish_reason is None:
        return STOP_REASON_UNREPORTED, None, None
    if finish_reason in ENGINE_STOP_COMPLETE:
        return finish_reason, True, False
    if finish_reason in ENGINE_STOP_CUT_OFF:
        return finish_reason, False, True
    return finish_reason, None, None


def _content_health(text: str, *, completed: bool | None) -> dict[str, Any]:
    """The recordable-text branch of `run.py::content_health`, reproduced.

    Every native payload this module ever hands to `content_health` is a
    decoded ``str`` (or absent entirely on the unrecordable branches, handled
    separately) -- `run.py`'s object/list/other branches never apply here, so
    only the ``str`` branch needs reproducing, and reproducing it (rather than
    importing `run.py`, which would import this module) keeps the boundary
    acyclic.
    """

    return {
        "native_type": "string",
        "encoding": "utf-8-json-native",
        "recordable": True,
        "empty": text == "",
        "blank": text.strip() == "",
        "truncated": None if completed is None else not completed,
        "characters": len(text),
        "truncation_basis": (
            "trusted-response-boundary" if completed is not None else "not-recorded"
        ),
    }


def _unrecordable_health(reason: str) -> dict[str, Any]:
    """The shape `validate_content_health` requires for `recordable=False`."""

    return {
        "native_type": "unrecordable",
        "encoding": "invalid-or-unrecordable",
        "recordable": False,
        "empty": None,
        "blank": None,
        "truncated": None,
        "characters": None,
        "truncation_basis": reason,
    }


def _unconfirmed_blank_reason(kind: str, transport_stop_reason: str, cut_off: bool | None) -> str:
    """Why an empty response is held rather than confirmed as ``genuinely-empty``.

    ``kind`` names what was read (``"page"`` or ``"act"``); ``cut_off`` is
    ``True`` for a recognized cut-off word and ``None`` for an unreported or
    unrecognized one -- both land here rather than becoming a confirmed blank.
    """

    if cut_off:
        return (
            "the provider response parsed empty after the provider stopped it at its bound "
            f"(transport_stop_reason {transport_stop_reason!r}); a cut-off response is not a "
            f"confirmed blank {kind}"
        )
    return (
        "the provider response parsed empty and the provider's stop boundary was never "
        f"confirmed complete (transport_stop_reason {transport_stop_reason!r}); an unconfirmed "
        f"boundary is not a confirmed blank {kind}"
    )


def _failed_parse_composition(
    parse_reason: str, transport_stop_reason: str, cut_off: bool | None
) -> tuple[str, str]:
    """Compose a parse failure's ``reason`` suffix and content-health basis.

    Shared by the act-scoped and page-scoped parse-failure branches so a
    provider-truncated response cannot go on being folded into one and
    dropped from the other. When ``cut_off`` is ``True`` — the provider's stop
    word was a recognized cut-off — both strings name the truncation ahead of
    the underlying parse reason; otherwise both are the parse reason verbatim.
    """

    if cut_off:
        cut_note = (
            f"the provider stopped the response at its bound "
            f"(transport_stop_reason {transport_stop_reason!r}) and "
        )
        basis = f"response cut off by the provider ({transport_stop_reason!r}); {parse_reason}"
        return f"{cut_note}{parse_reason}", basis
    return parse_reason, parse_reason


def _blob_ref(context: Any, data: bytes) -> dict[str, str]:
    """Retain ``data`` and return the closed ``{relative_path, sha256}`` reference shape."""

    digest, published = context.tree.put_blob(ATTESTATORES, data)
    return {"relative_path": published.relative_path, "sha256": digest}


def _dai_model_view(
    context: Any,
    presentation: Mapping[str, Any],
    presented: Mapping[str, Any],
    prompt: Mapping[str, Any],
    generation_declared: Mapping[str, Any],
) -> dict[str, Any]:
    """Build DAI's closed model view (`feeding.dai_model_view`) for this act.

    ``source_image_ref`` is the Designator's own region crop -- ``presentation``
    is exactly what `run.py::presentation_for_region` reads off the sealed
    Designator record, matching `feeding.py`'s own tests' naming
    (``"designator/crops/..."``). ``model_image_ref`` is DAI's own further
    crop-and-resize output (``presented``, from `witness_adapters._dai_present`).
    The no-resize case is satisfied by *content*, not by path identity:
    ``_dai_present`` always republishes its own crop as a fresh blob under
    ``3_attestatores/``, but on that path it is ``crop_png`` of the same sealed
    page at the same bounds as the Designator's ``2_designator/`` proposal
    crop, so the two references share one digest under two stage-owned paths,
    and ``feeding.dai_model_view`` compares exactly that digest.

    Prompt and generation-config bytes are retained here (not carried bytes
    read back from elsewhere) because DAI's closed view requires digest-backed
    references to both; ``generation_declared`` is serialized with plain JSON
    rather than `common.contracts.canonical.canonical_bytes` because DAI's
    carried config includes a float (``temperature``), which canonical
    encoding refuses outright.
    """

    bounds = presentation["transform"]["bounds"]
    return feeding.dai_model_view(
        source_image_ref={
            "relative_path": presentation["image_path"],
            "sha256": presentation["image_sha256"],
        },
        model_image_ref={
            "relative_path": presented["image_path"],
            "sha256": presented["image_sha256"],
        },
        width_px=bounds["w"],
        height_px=bounds["h"],
        system_prompt_ref=_blob_ref(context, prompt["system"].encode("utf-8")),
        query_prompt_ref=_blob_ref(context, prompt["user"].encode("utf-8")),
        generation_config_ref=_blob_ref(
            context, json.dumps(dict(generation_declared), sort_keys=True).encode("utf-8")
        ),
    )


def _malformed_response_attempt(response: ChairResponse, *, adapter: Any) -> LiveAttempt:
    """A wire response `ChairClient` could not parse into a reading at all.

    Retained (the raw bytes are already on disk via ``raw_response_ref``),
    never repaired, never re-requested -- the same "malformed" branch
    `resolve_attempt` takes for a fixture-declared malformed response.
    ``format_capabilities`` still names the adapter's own grammar
    (`_format_capabilities_for`): what a chair's grammar can carry is a fact
    about the chair, not about whether this one body happened to parse.
    """

    reason = f"the provider response was refused without repair: {response.parse_problem}"
    return LiveAttempt(
        outcome="failed",
        native_payload=None,
        witness_reported=None,
        format_capabilities=_format_capabilities_for(adapter),
        health=_unrecordable_health(reason),
        reason=reason,
        raw_response_ref=dict(response.raw_response_ref),
        native_capture=None,
        call_record_ref=dict(response.call_record_ref),
        receipt_ref=dict(response.receipt_ref),
        raw_response_kind=RAW_RESPONSE_TRANSPORT_BODY,
    )


def live_attempt_from_response(
    context: Any,
    adapter: Any,
    adapter_name: str,
    response: ChairResponse,
    *,
    presentation: Mapping[str, Any],
    presented: Mapping[str, Any],
    prompt: Mapping[str, Any],
    generation_declared: Mapping[str, Any],
    parser: str,
) -> LiveAttempt:
    """Derive one act-scoped chair's `LiveAttempt` from its retained response.

    ``dai.v1`` is the only act-scoped adapter today; this function refuses any
    other name rather than guessing at a view shape it does not know.
    ``presentation``/``presented``/``prompt`` are exactly what
    `act_chair_request` computed for this same act (``presented``/``prompt``
    from its returned `ActChairRequest`; ``presentation`` is the same argument
    that call was given) -- reused here rather than recomputed, so DAI's real
    crop and resize runs once per act, not twice.

    Mirrors `resolve_attempt`: ``adapter.retain`` -> its ``parse`` state ->
    ``read`` | ``genuinely-empty`` (confirmed only when the transport word was
    a recognized natural stop) | ``failed`` (an empty response whose boundary
    was cut off, unreported, or unrecognized; a parse failure; or a wire-level
    ``parse_problem`` before any native parse was possible at all).
    """

    if adapter_name != "dai.v1":
        raise SchemaRefusal(
            f"live_attempt_from_response has no capture recipe for adapter {adapter_name!r}; "
            "only dai.v1 is act-scoped today"
        )
    if response.parse_problem is not None:
        return _malformed_response_attempt(response, adapter=adapter)

    transport_stop_reason, completed, cut_off = _finish_reason_facts(response)
    view = _dai_model_view(context, presentation, presented, prompt, generation_declared)
    capture = adapter.retain(
        context.tree,
        view=view,
        raw_response=response.content.encode("utf-8"),
        transport_stop_reason=transport_stop_reason,
        parser=parser,
        # These bytes came off a chair that answered. The flag decides exactly
        # one thing at the retention seam: Chandra's fixture-placeholder parser
        # may not run for a served chair, because a served chair answering in a
        # shape `chandra.prompt()` never asked for is a named surprise rather
        # than a reading (CodeRabbit round 1, T7).
        served=True,
    )
    parsed = capture["parse"]
    if parsed["state"] == "parsed" and (completed is True or parsed["text"] != ""):
        text = parsed["text"]
        outcome = "genuinely-empty" if text == "" else "read"
        return LiveAttempt(
            outcome=outcome,
            native_payload=text,
            witness_reported=None,
            format_capabilities=_format_capabilities_for(adapter),
            health=_content_health(text, completed=completed),
            reason=None,
            raw_response_ref=dict(capture["raw_response_ref"]),
            native_capture=capture,
            call_record_ref=dict(response.call_record_ref),
            receipt_ref=dict(response.receipt_ref),
            raw_response_kind=RAW_RESPONSE_MODEL_OUTPUT,
        )
    if parsed["state"] == "parsed":
        # An interrupted or unconfirmed empty response is not evidence of a
        # blank act.
        reason = _unconfirmed_blank_reason("act", transport_stop_reason, cut_off)
        return LiveAttempt(
            outcome="failed",
            native_payload="",
            witness_reported=None,
            format_capabilities=_format_capabilities_for(adapter),
            health=_content_health("", completed=completed),
            reason=reason,
            raw_response_ref=dict(capture["raw_response_ref"]),
            native_capture=capture,
            call_record_ref=dict(response.call_record_ref),
            receipt_ref=dict(response.receipt_ref),
            raw_response_kind=RAW_RESPONSE_MODEL_OUTPUT,
        )
    # "failed" (dai.v1's only other parse state) lands here: it produced no
    # text this attempt can call a reading.
    reason_suffix, basis = _failed_parse_composition(
        parsed["reason"], transport_stop_reason, cut_off
    )
    return LiveAttempt(
        outcome="failed",
        native_payload=None,
        witness_reported=None,
        format_capabilities=_format_capabilities_for(adapter),
        health=_unrecordable_health(basis),
        reason=f"the provider response was retained but not usable: {reason_suffix}",
        raw_response_ref=dict(capture["raw_response_ref"]),
        native_capture=capture,
        call_record_ref=dict(response.call_record_ref),
        receipt_ref=dict(response.receipt_ref),
        raw_response_kind=RAW_RESPONSE_MODEL_OUTPUT,
    )


def captured_page_attempt(
    context: Any,
    page_ordinal: int,
    chair: str,
    adapter_name: str,
    adapter: Any,
    response: ChairResponse,
    framing: str | None = None,
) -> LiveAttempt:
    """The live twin of `run.py::captured_churro_page_attempt`, generalized.

    Takes an already-retained `ChairResponse` instead of a fixture row, and
    keeps every branch that function has: parsed-and-complete becomes
    ``read``/``genuinely-empty``; an empty response whose transport word was
    not a recognized natural stop -- cut off, unreported, or unrecognized --
    is ``failed`` with "not a confirmed blank page" rather than the emptier
    and wrong ``genuinely-empty``; an unparseable response is ``failed`` with
    its bytes retained. Runs for both page-scoped adapters today (``churro.v1``,
    ``chandra.v1``). Chandra reads the vendor's own layout grammar
    (`common/chandra_layout.py`, the answer `chandra.prompt` asks for); a body
    that yields no top-level block lands on the ``unrecognized-shape`` branch
    naming which of the two ways that happened (``no-layout-blocks`` for prose,
    ``blocks-not-at-top-level`` for a wrapped answer, and the rest of that
    module's closed set). A parsed Chandra response also carries its bytes
    forward as ``observation_payload``, because `run.py` derives the page's
    block geometry from those same bytes rather than from the text.

    Churro reads the vendor's own `HistoricalDocument` grammar
    (`common/churro_document.py`, the answer `churro.prompt` asks for), the
    plain reading-order text the paper-era harness itself expected, or the
    retired `<output>` envelope kept so retained history still reads; a
    well-formed XML body rooted at anything else lands on the
    `unrecognized-shape` branch naming which root it was, and a body that offers
    the grammar and will not parse on the unparseable branch. Both page
    witnesses carry their bytes forward as ``observation_payload`` for the same
    reason -- though Churro derives no geometry from them, because
    `HistoricalDocument` publishes none.

    ``page_ordinal`` and ``chair`` are not read by this function's own logic;
    they are accepted to keep this call site self-describing at the one place
    U6 will call it, exactly as `captured_churro_page_attempt` names them for
    the same reason.
    """

    del page_ordinal, chair
    if response.parse_problem is not None:
        return _malformed_response_attempt(response, adapter=adapter)

    transport_stop_reason, completed, cut_off = _finish_reason_facts(response)
    if adapter_name == "churro.v1":
        generation_declared: dict[str, Any] = dict(feeding.churro_generation())
        # `"xml"`, the vendor's own `HistoricalDocument` grammar
        # (`common/churro_document.py`). One name, because this chair has one
        # grammar: Unit 12's second parser existed only for the JSON coordinate
        # contract this repository invented, and that is retired with the prompt
        # that asked for it, so the fixture posture and a served answer read the
        # same three legal shapes through the same branch.
        parser = "xml"
    elif adapter_name == "chandra.v1":
        generation_declared = dict(feeding.chandra_generation())
        # `"html"`, the vendor's own layout grammar. The name selects the parser
        # (`feeding._RUNNABLE_PARSERS`): the committed fixture's placeholder
        # keeps `"json"` and is refused for a served chair outright, so a live
        # reading can only ever be taken in the grammar the chair was asked in.
        parser = "html"
    else:
        raise SchemaRefusal(
            f"captured_page_attempt has no capture recipe for adapter {adapter_name!r}; "
            f"the page-scoped adapters are {sorted(_PAGE_SCOPED_ADAPTERS)}"
        )
    view: dict[str, Any] = {"prompt": _framed_prompt(adapter, framing)}
    if generation_declared:
        view["generation"] = generation_declared
    if framing is not None:
        # Which question this reading was asked, by name, on the record it
        # produced. The prompt bytes are already retained beside it; the name is
        # what lets one run's readings be compared with another's without
        # digesting two prompts to discover they differ (GOVERNANCE 6, and the
        # A/B this makes possible at all).
        view["framing"] = framing
    capture = adapter.retain(
        context.tree,
        view=view,
        raw_response=response.content.encode("utf-8"),
        transport_stop_reason=transport_stop_reason,
        parser=parser,
        # These bytes came off a chair that answered. The flag decides exactly
        # one thing at the retention seam: Chandra's fixture-placeholder parser
        # may not run for a served chair, because a served chair answering in a
        # shape `chandra.prompt()` never asked for is a named surprise rather
        # than a reading (CodeRabbit round 1, T7).
        served=True,
    )
    parsed = capture["parse"]
    if parsed["state"] == "parsed" and (completed is True or parsed["text"] != ""):
        text = parsed["text"]
        outcome = "genuinely-empty" if text == "" else "read"
        return LiveAttempt(
            outcome=outcome,
            native_payload=text,
            witness_reported=None,
            format_capabilities=_format_capabilities_for(adapter),
            health=_content_health(text, completed=completed),
            reason=None,
            raw_response_ref=dict(capture["raw_response_ref"]),
            native_capture=capture,
            call_record_ref=dict(response.call_record_ref),
            receipt_ref=dict(response.receipt_ref),
            raw_response_kind=RAW_RESPONSE_MODEL_OUTPUT,
            # Both page-scoped adapters derive their block geometry in `run.py`
            # from these same bytes rather than from the parsed text, so both
            # carry them forward. Withholding them from Churro would hand its
            # `observe` the joined page text and derive no geometry at all --
            # the unattached page witness this unit exists to close.
            # Unconditional, and that is the point: the dispatch above has
            # already refused every adapter but the two page-scoped ones, so a
            # membership test here could only ever be true. Written as a test it
            # would be a second, quieter list of the page adapters -- and a third
            # page chair added to the dispatch and forgotten here would then land
            # with `observation_payload=None`, deriving no geometry, silently
            # (GOVERNANCE 2). One list, at the refusal, where a miss is loud.
            observation_payload=response.content.encode("utf-8"),
        )
    if parsed["state"] == "parsed":
        # An interrupted or unconfirmed empty response is not evidence of a
        # blank page.
        reason = _unconfirmed_blank_reason("page", transport_stop_reason, cut_off)
        return LiveAttempt(
            outcome="failed",
            native_payload="",
            witness_reported=None,
            format_capabilities=_format_capabilities_for(adapter),
            health=_content_health("", completed=completed),
            reason=reason,
            raw_response_ref=dict(capture["raw_response_ref"]),
            native_capture=capture,
            call_record_ref=dict(response.call_record_ref),
            receipt_ref=dict(response.receipt_ref),
            raw_response_kind=RAW_RESPONSE_MODEL_OUTPUT,
        )
    # Shared with `common/native_witness.py`'s page validator, which re-derives
    # this attempt's `reason` and `truncation_basis` and compares them: two
    # inline copies of one sentence is a record that refuses itself the moment
    # they drift.
    parse_reason = native_parse_refusal(parsed)
    reason_suffix, basis = _failed_parse_composition(parse_reason, transport_stop_reason, cut_off)
    return LiveAttempt(
        outcome="failed",
        native_payload=None,
        witness_reported=None,
        format_capabilities=_format_capabilities_for(adapter),
        health=_unrecordable_health(basis),
        reason=f"the provider response was retained but not usable: {reason_suffix}",
        raw_response_ref=dict(capture["raw_response_ref"]),
        native_capture=capture,
        call_record_ref=dict(response.call_record_ref),
        receipt_ref=dict(response.receipt_ref),
        raw_response_kind=RAW_RESPONSE_MODEL_OUTPUT,
    )
