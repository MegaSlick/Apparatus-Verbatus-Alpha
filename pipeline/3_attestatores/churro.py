"""Churro's page-witness adapter, running the vendor's own system.

Tonight's ruling (Tyrel, 2026-09-06) is that each witness runs as its
developers intended: the vendor's preprocessing, prompt bytes, message shape,
generation values and output grammar are adopted verbatim and pinned by digest,
and the vendor's harness is not. This module is where that ruling reaches the
Churro chair. The grammar itself -- the carried system strings and the reader
for the `HistoricalDocument` XML they ask for -- lives in
`common/churro_document.py`, because Churro captures alone among the three
adapters are re-derived from their retained blob by `verify_native_capture_bytes`
at three stages, and neither the Perlector nor the Recensor may import an
Attestatores module. One implementation in `common/` is what makes those
re-derivations reach the branch this adapter wrote the record under.

**Why the JSON coordinate channel is gone, and what it cost.** Until now this
chair was asked, in a *modified* carry of a prompt the model was never trained
on, for a closed JSON object of this repository's own invention
(`feeding.churro_layout_prompt`, parsed by `common/churro_response.py`), so that
a geometry-blind page witness would have block rectangles to attach acts by.
Two facts retire it. The ruling says each witness runs its vendor's own system,
and the vendor's own registry answer for `stanford-oval/churro-3B` is a single
system instruction with no user message at all
(`providers/specs.py::resolve_ocr_profile` -> `churro_3b_profile()` ->
`CHURRO_3B_XML_TEMPLATE`, `user_prompt=None`). And Churro-DS, the fine-tuning
target, **carries no geometry**: the paper's own description is one continuous
text string per page in reading order, so a `box_1000` per block was a channel
the weights were never taught to fill. Asking for it did not add geometry; it
replaced the vendor's framing with one nobody measured, on the hope that the
base model's layout ability survived the fine-tune. The prompt, its wire
contract and its module are gone, and the attachment problem they were invented
to solve is solved where it belongs -- the Perlector admits the existing
`anchor-line` basis for an aligned page witness (U12).

**The four vendor pieces this adapter is responsible for.**

* *Preprocess.* `present` reproduces
  `src/churro_ocr/_internal/image.py::prepare_ocr_image` --
  `ensure_rgb(resize_image_to_fit(img, 2500, 2500))` -- through
  `common/imaging_ports.py::resize_to_fit_churro`, which is the port,
  `common/imaging.py::resize_png_lanczos`, which moves the pixels, and
  `common/imaging.py::convert_png_to_rgb`, which is the vendor's `ensure_rgb`.
  It is recorded as an `adapter-crop` under the vendor's own operation name
  `churro-prepare-ocr-image.v1`, with the colour step named in `colour_mode`,
  so the exact image the chair saw re-derives from the sealed Exemplar
  (ARCHITECTURE invariant 3).
* *Prompt.* `prompt` sends one of the two vendor-attested system strings
  (`common/churro_document.py::CHURRO_PROMPT_VARIANTS`) as a system turn, with
  an image-only user turn -- the shape `templates/hf.py::HFChatTemplate.
  build_conversation` builds for a profile whose `user_prompt` is `None`.
* *Parse.* `parse` reads the `HistoricalDocument` grammar through
  `common/churro_document.py`, the re-expression of the vendor's own
  `evaluation/xml_utils.py` flattener over the standard library.
* *Observe.* Churro reports no geometry, **by vendor design**, so `observe`
  returns the honest `bounds_source="presented"` echo of the image it was
  shown. Routing and coverage expressly exclude that source, so nothing is
  fabricated from the presentation itself, and this adapter declares no
  `quantization` rule at all: there are no normalized coordinates to convert,
  and a rule acquired by omission is a rule nobody declared for this chair.

**Two framings, both the vendor's.** `FRAMINGS` is the same selector Unit 12
built, with its two entries replaced by the two strings a vendor artifact
actually sent: the registry's answer at tag `v0.3.0`, and the paper-era
benchmark harness's own `SYSTEM_MESSAGE` with its two spelling errors intact.
Nothing here selects among readings (hard rule 8): it selects the wording of a
question before the page is read, and the name is written onto the Testimonium
the reading produces so an A/B compares on a recorded fact rather than on a
digest of two prompts.

**Provenance.** `github.com/stanford-oval/Churro` (Apache-2.0) at tag `v0.3.0`
= `4abb17386d9656199c2776195926545fc527a691` for the registry string, the image
rule and the grammar guide, and at the paper-era release
`ed09bc7fd6475c333a25427f3d0b9227af46ce27` for the harness string and the XSD;
`stanford-oval/churro-3B` at `ca2150ea465d5a3d67818c50e234b9422619c75d` for the
weights. No vendor package is installed: every carried byte is cited where it
is carried and re-checked against the vendor by `common/test_vendor_parity.py`.
"""

from __future__ import annotations

from functools import partial
from types import MappingProxyType
from typing import Any, Callable, Final, Mapping

import feeding

from common import churro_document
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ATTESTATORES
from common.imaging import convert_png_to_rgb, crop_png, dimensions, resize_png_lanczos
from common.imaging_ports import resize_to_fit_churro
from common.native_witness import parse_churro_response, validate_presented

#: The vendor preprocessing this adapter records over its own presented pixels.
#: The name is `common/native_witness.py`'s, where the operation is admitted,
#: its rounding rule declared (`floor`) and its replay from sealed page bytes
#: implemented; spelled once here so the writer and the vocabulary cannot drift.
PRESENT_OPERATION: Final = "churro-prepare-ocr-image.v1"
#: `prepare_ocr_image` is `ensure_rgb(resize_image_to_fit(...))` with no branch
#: in it, so this adapter always converts and always says so. `native_witness`
#: holds it to exactly this value for exactly this operation: a record saying
#: `keep` would say the RGB half of the operation it names did not run.
PRESENT_COLOUR_MODE: Final = "rgb"

#: What Churro's grammar can carry, declared from the grammar rather than
#: assumed from a blanket default (vendor systems design, Contract boundary).
#:
#: `can_express_layout` is **false and stays false**: `HistoricalDocument` has
#: `Page`, `Header`, `Body`, `Footer` and `Line`, and not one coordinate
#: anywhere in the XSD. Reading order is structure, not geometry.
#:
#: `can_express_uncertainty` is **true, and the order is the whole point**. The
#: grammar carries a doubt -- `Illegible`, `Gap`, `Deletion` and `Addition` are
#: in the XSD and `churro_document` records each one's span -- but a chair that
#: declares uncertainty before the Perlector can compare one is permanently
#: `compared: unknown` under `dissent.is_comparable`. U6 landed the comparison
#: views, U12 wired them, and the flip is this integration's, after both, so a
#: declared capability is never a capability nothing can act on (the judges'
#: second fatal flaw).
#:
#: The view this chair is compared through is the page witness's, not the
#: bracket one: `dissent_testimonia` gives a page witness the act's anchored,
#: markup-stripped slice of its own page reading. That is available here because
#: this chair attaches at all -- on the `anchor-line` basis, since the grammar
#: reports no geometry -- so declaring the capability costs it no dissent row at
#: any act it reaches. An act it does not reach was already `compared:
#: "unknown"` with its reason recorded, and stays so.
FORMAT_CAPABILITIES: Final[Mapping[str, bool]] = MappingProxyType(
    {"can_express_uncertainty": True, "can_express_layout": False}
)


#: The framings this chair can be asked in, by name -- both of them a vendor
#: artifact's own bytes, and both system-only. Closed and exact: a name outside
#: it is refused rather than resolved to something near it. The set is derived
#: from `churro_document.CHURRO_PROMPT_VARIANTS` rather than restated, so a
#: variant added there cannot be a framing this adapter silently cannot offer.
FRAMINGS: Final[Mapping[str, Callable[[], dict[str, str]]]] = MappingProxyType(
    {
        name: partial(churro_document.churro_prompt_view, name)
        for name in sorted(churro_document.CHURRO_PROMPT_VARIANTS)
    }
)

#: What a run gets when it names no framing: the string the vendor's own
#: registry resolves for this model id today
#: (`providers/specs.py::resolve_ocr_profile("stanford-oval/churro-3B")` at tag
#: `v0.3.0`). The paper-era harness string is the second arm rather than the
#: default because the registry is what the vendor ships now, and which of the
#: two the fine-tuning itself saw is stated nowhere -- so the default is the
#: attested current answer, and the comparison is Stage 2's A4b arm.
DEFAULT_FRAMING: Final = "registry-v0.3.0"

if DEFAULT_FRAMING not in FRAMINGS:  # pragma: no cover - import-time guard
    raise SchemaRefusal(
        f"churro.v1's default framing {DEFAULT_FRAMING!r} is not one the vendor grammar "
        f"declares ({sorted(FRAMINGS)}); a chair is never asked a question nobody carried"
    )


def resolve_framing(framing: Any = None) -> str:
    """One declared framing name, exactly, or a refusal listing the declared set.

    **Not a picker** (hard rule 8). It selects the wording of a question before
    the page is read; nothing here chooses among readings, ranks them, or looks
    at a response. The name it returns is written onto the Testimonium the
    reading produces, so which question was asked is a recorded fact rather
    than something a later reader infers from the prompt bytes.
    """

    if framing is None:
        return DEFAULT_FRAMING
    if not isinstance(framing, str) or framing not in FRAMINGS:
        raise SchemaRefusal(
            f"churro.v1 has no framing named {framing!r}; the declared framings are "
            f"{sorted(FRAMINGS)} and a reading is never taken under a near match"
        )
    return framing


def prompt(framing: Any = None) -> dict[str, str]:
    """The vendor's own system string, as the one system turn it is sent in.

    `{"system": ...}` and nothing else, which is what makes
    `live_witness._page_messages` build a system turn plus an **image-only**
    user turn: both attested profiles set `user_prompt`/`user_message_text` to
    `None`, so there is no vendor user text to send, and recording
    `{"system": ..., "user": ""}` would be the difference between saying no
    user text was sent and saying empty user text was.
    """

    return FRAMINGS[resolve_framing(framing)]()


def vendor_identity(system_prompt: Any) -> dict[str, Any] | None:
    """Which vendor pin the prompt bytes beside a reading were taken from.

    GOVERNANCE 6 already puts the model's resolved identity on every stored
    reading. This is the other half once the chair runs the vendor's own
    system: the prompt is the vendor's, taken at one commit, and a later
    re-parse under a different pin produces a different reading of the same
    retained response. Returned as fresh, plain containers because it travels
    into a retained record (`common/native_witness.py::validate_vendor_identity`
    closes its shape).

    **Keyed on the retained bytes, not on the framing name.** The two attested
    variants come from two different files at two different commits, and a
    record built from a name could name a pin for bytes it did not retain --
    the fixture posture, for one, writes a prompt view without ever naming a
    framing. Matching the exact string the view carries makes the identity a
    fact about those bytes. The carried string is then named by the vendor's own
    symbol rather than by this repository's word for the arm.

    ``None`` where the system string is not one of the carried strings. There is
    then no vendor pin to record, and the optional field is honestly absent
    rather than filled in with a provenance for text no vendor supplied.
    """

    for name, entry in churro_document.CHURRO_PROMPT_VARIANTS.items():
        if entry["system"] == system_prompt:
            provenance = churro_document.churro_prompt_provenance(name)
            return {
                "repository": provenance["repository"],
                "sha": provenance["commit"],
                "carried_strings": {provenance["symbol"]: provenance["system_sha256"]},
            }
    return None


def parse(raw_response: bytes, *, system_prompt: str | None = None) -> Any:
    """Return the page text of a vendor grammar answer, or a named shape outcome.

    The two return kinds `chandra.parse` has, for the same reason: a caller
    that already handles one page witness handles this one. Three legal shapes
    reach `parsed` -- the `HistoricalDocument` grammar, the plain reading-order
    text the paper-era harness itself expected, and the retired `<output>`
    envelope kept so retained history still reads -- and each of the last two
    carries a finding on the capture beside these bytes rather than passing
    silently (GOVERNANCE 2).

    `system_prompt` is the exact string this request sent, and only that string
    is ever trimmed from the head of the response
    (`churro_document.trim_leading_prompt`, the vendor's own
    `run_churro_ocr.py` rule). Trimming against a framing that was not sent
    could remove real transcription that happens to begin the same way, which
    is why the caller passes what it asked rather than this module trying every
    variant it knows.

    `common/churro_document.py` is the implementation, called rather than
    restated: `derive_churro_capture` and the two readers'
    `verify_native_capture_bytes` re-derive through that same function, and a
    second copy here could come to disagree with the record it wrote.
    """

    result = parse_churro_response(raw_response, system_prompt=system_prompt)
    if result["state"] == "parsed":
        return result["text"]
    if result["state"] == "unrecognized-shape":
        return {"parse_outcome": result["reason"]}
    raise SchemaRefusal(result["reason"])


def retain(
    tree: Any,
    *,
    view: dict[str, Any],
    raw_response: bytes,
    transport_stop_reason: str,
    parser: str | None = None,
    served: bool = False,
) -> dict[str, Any]:
    """Retain one Churro view under its own registry identity only.

    Accepts no ``adapter`` argument to pin: forwarding one would let code that
    had resolved ``churro.v1`` file this response under another chair's model
    boundary, and the retained record would then name the wrong one
    (GOVERNANCE 6). Chandra's and DAI's wrappers pin their names the same way.
    """
    return feeding.retain_model_view(
        tree,
        adapter="churro.v1",
        view=view,
        raw_response=raw_response,
        transport_stop_reason=transport_stop_reason,
        parser=parser,
        served=served,
    )


def present(context: Any, presentation: dict[str, Any]) -> dict[str, Any]:
    """Size and colour a whole page the way Churro's own pipeline does, and record it.

    `src/churro_ocr/_internal/image.py::prepare_ocr_image` runs on every image
    the vendor's inference path sends, and under tonight's ruling it runs here
    too: the crop comes off the sealed Exemplar,
    `common/imaging_ports.resize_to_fit_churro` decides the target,
    `common/imaging.resize_png_lanczos` moves the pixels,
    `common/imaging.convert_png_to_rgb` is the vendor's `ensure_rgb`, and the
    result is published as an `adapter-crop` whose transform names the vendor
    operation and the colour step. That is what makes the exact image the chair
    saw re-derivable from the Exemplar plus the record (ARCHITECTURE invariant
    3): `common/native_witness.py::validate_presented_page_binding` replays
    exactly these steps, in this order, against the sealed page and refuses a
    digest that does not come back.

    **The colour step runs after the resize**, because that is where the vendor
    performs it -- `ensure_rgb(resize_image_to_fit(...))` -- and converting
    first would resample three expanded channels instead of the one the vendor
    resampled.

    **Only a whole-page presentation is transformed, and that is the honest
    line.** Churro is a page witness: the image a chair is ever shown is a page
    (`live_witness.page_chair_request`), and the vendor preprocessing is a fact
    about that image. An act view of a page witness is a compatibility record
    that restates one page reading against one act's Designator crop; no chair
    was ever shown those pixels, and minting a vendor recipe over them would
    record a preprocessing step that never ran on an image nobody sent. So a
    `region` presentation is returned exactly as it arrived, and
    `witness_adapters.validate_adapter_presentation` re-derives both cases
    apart.

    **A page already inside the 2,500-pixel square still records the
    operation**, and still runs both calls. `resize_image_to_fit` returns the
    image it was given when both sides already fit, and `ensure_rgb` runs
    regardless; `churro-prepare-ocr-image.v1` names the vendor function, not a
    resampler, and that function runs on every call. `resize_png_lanczos` is
    still called and still frames the output, so no step is claimed that did
    not happen.
    """

    validate_presented(presentation)
    if presentation["kind"] != "page":
        return presentation
    transform = presentation["transform"]
    page_id = transform["source_page_id"]
    page_bytes = feeding.sealed_page_bytes(context, page_id, what="Churro")
    # Bounds failures must stay SchemaRefusals so callers can hold the attempt;
    # `crop_png` alone would expose a bare ValueError at this boundary.
    validate_presented(presentation, page_size=dimensions(page_bytes))
    bounds = dict(transform["bounds"])
    source_width, source_height = bounds["w"], bounds["h"]
    target_width, target_height = resize_to_fit_churro(source_width, source_height)
    resized = resize_png_lanczos(crop_png(page_bytes, bounds), target_width, target_height)
    try:
        model_image = convert_png_to_rgb(resized)
    except ValueError as error:
        # `ensure_rgb`'s own failure, named at this boundary rather than raised
        # through it. `convert_png_to_rgb` refuses an image mode a sealed crop
        # cannot arrive in, and a bare `ValueError` out of an adapter is the
        # thing the bounds check above is already written to prevent: a caller
        # that could have held this attempt with a reason gets an unnamed
        # interpreter error instead (GOVERNANCE 2).
        raise SchemaRefusal(
            f"Churro's presented page cannot be converted to RGB, which is half of the vendor's "
            f"own prepare_ocr_image and cannot be recorded as having run: {error}"
        ) from error
    digest, published = context.tree.put_blob(ATTESTATORES, model_image)
    return {
        "kind": "adapter-crop",
        "source_page_id": page_id,
        "source_page_ordinal": transform["source_page_ordinal"],
        "image_path": published.relative_path,
        "image_sha256": digest,
        "transform": presented_transform(
            page_id,
            transform["source_page_ordinal"],
            bounds,
            (target_width, target_height),
        ),
    }


def presented_transform(
    page_id: str,
    page_ordinal: int,
    bounds: dict[str, int],
    target: tuple[int, int],
) -> dict[str, Any]:
    """The one transform this adapter writes, so the re-deriver reads it here.

    `witness_adapters.validate_adapter_presentation` has to state what this
    adapter could have produced without running it. Two hand-written copies of
    one recipe agree only for as long as both are edited together, so the
    writer and the re-deriver call this.
    """

    return {
        "operation": PRESENT_OPERATION,
        "source_page_id": page_id,
        "source_page_ordinal": page_ordinal,
        "bounds": dict(bounds),
        "colour_mode": PRESENT_COLOUR_MODE,
        "resize": {
            "resampler": "pillow-lanczos",
            "dimension_rounding": "floor",
            "source_width_px": bounds["w"],
            "source_height_px": bounds["h"],
            "target_width_px": target[0],
            "target_height_px": target[1],
        },
    }


def observe(presentation: dict[str, Any], native_payload: Any) -> list[dict[str, Any]]:
    """The honest no-layout echo: Churro reports no geometry, by vendor design.

    `HistoricalDocument` has no coordinate vocabulary anywhere in the vendor's
    guide or its XSD, and Churro-DS carries none either, so there is nothing to
    convert and nothing that could be converted. What this returns is a single
    ``bounds_source="presented"`` entry restating the presentation -- a source
    routing and coverage expressly exclude -- so the record says which image
    this reading speaks for without fabricating geometry from it.

    It takes no ``page_size``: that keyword exists for an adapter whose
    response reports boxes normalized against the whole page, and this one
    reports no boxes at all. `witness_adapters.RunnableAdapter.takes_page_size`
    is `False` for this adapter, and every call site reads that flag rather
    than comparing adapter names.

    `native_payload` is accepted because it is part of the common adapter
    interface and is deliberately not read: no property of a response could
    make a presentation echo into reported ink.
    """

    del native_payload
    validate_presented(presentation)
    return [
        {
            "ordinal": 0,
            "bounds": dict(presentation["transform"]["bounds"]),
            "bounds_source": "presented",
            "span": None,
        }
    ]
