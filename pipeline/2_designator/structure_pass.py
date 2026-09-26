"""The structure chair's side of the Designator's live pass (SPEC_D §1, §2).

Everything here faces the served chair: which pass a run selects, what is sent
per sealed page, what comes back and what it does to the page, and the
geometry the answer mints. Nothing here cuts a crop or writes a stage artifact
-- `run.py::live_initial_pass` does both, through the same helpers the fixture
pass uses, so a crop has exactly one author and a page exactly one status
record whichever pass marked it out.

**What is sent.** At most one `chat-completions` request per page per attempt, subject to capacity admission. The rendered
and resized RGB PNG `prepare_page_request_image` produces as a
`data:image/png;base64` block, bound back to the sealed page by a
transformation artifact -- `image_sha256s` records that rendered image's own
digest, not the sealed page's. Sent as a single `user` turn with the image
block before the instruction -- the shape Chandra's own inference code
expects. The
instruction is the vendor's own prompt (`structure_prompt.py`), not one this
project wrote. No tiling: the chair this pass serves is page-level. The
generation bound is `min(Chandra's 12,384-token MAX_OUTPUT_TOKENS,
max_model_len less this request's measured cost)`; a `"length"` stop means the
answer did not fit and the page is held rather than read short.

**What comes back** is Chandra's layout HTML, read by the one parser in this
tree for that grammar. Each top-level block whose geometry resolves becomes
one **structure proposal**: its rectangle, vendor label, and text as a digest
and a length. A block with a malformed or absent `data-bbox`, or labelled
`Blank-Page`, proposes nothing and is recorded in `blocks_without_proposal`
with the reason -- never minted, and never given a substituted rectangle (the
vendor's own parser would otherwise default to `[0, 0, 1, 1]`, a crop in the
page's corner under this chair's provenance).

**What each answer does to the page** is the closed table in SPEC_D §1.4,
implemented by `ask_page`: a parsed, complete answer with at least one
proposal marks the page `detected`; one that proposes nothing marks it
`fallback-tiles` and cuts the page into predetermined crops; a cut-off, an
unparseable answer, an unusable call, or rectangles that touch none of the
scan's own ink holds the page under `STRUCTURE_HELD_CODES`. A refusal before
the chair was reached (`ServingError`/`EndpointUnavailable`) is fatal: no
answer or page status is published, but the request-image artefact remains; a
capacity refusal or a dispatched call that came back unusable, with the
durable call facts recorded, instead holds the
page under `HELD_CALL_UNUSABLE` -- a terminal outcome, not a pending one. A
bad reading is never repaired or re-rolled; the bounded call retry
(`ABSOLUTE_STRUCTURE_ATTEMPT_CEILING`) only recovers a structural loop or an
invalid layout.

**A page whose blocks all failed to place is tiled, not held**: the
difference from a `Blank-Page` answer is only in the record
(`block_count`/`blocks_without_proposal`/findings), not the disposition, since
both claims are "no rectangle was proposed" and tiling keeps the page covered
rather than costing every act on it until reviewed.

**Decoding** runs under `config/decoding.toml`'s `[structure]` section, never
under `reading_of_record`, so this pass may vary its posture while the
Attestatores stay fixed; the value is digest-checked and recorded on every
page. Coverage recovery keeps temperature fixed and advances the seed by
attempt ordinal from the serving profile's base seed.

**No picker.** The chair proposes rectangles; the ink scan corroborates them
and never overrides them; nothing here ranks, selects among, or repairs what
the chair returned. A duplicate rectangle mints once, recorded as a finding,
never chosen between.
"""

from __future__ import annotations

import base64
import dataclasses
from collections import Counter
from pathlib import Path
from typing import Any, Final, Mapping, TypedDict, TypeVar, cast

import geometry
import structure_prompt

from common import chandra_layout, structure_answer
from common.chair_wire import chandra_wire_fields
from common.chairs.models import AbsentChair, ChairIdentity
from common.chandra_custody import retain_chandra_response
from common.chandra_presentation import (
    STRUCTURE_REQUEST_IMAGE_FIELDS,
    STRUCTURE_REQUEST_IMAGE_KIND,
    STRUCTURE_REQUEST_IMAGE_SCHEMA,
    presented_transform,
    render_page,
)
from common.contracts.canonical import digest_of
from common.contracts.envelope import verify_input_bytes
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.serving import ENGINE_STOP_COMPLETE, ENGINE_STOP_CUT_OFF
from common.contracts.stages import DESIGNATOR
from common.decoding import load_decoding_policy
from common.imaging import Bounds, dimensions
from common.native_witness import validate_presented, validate_presented_page_binding
from common.request_capacity import (
    dense_page_answer_budget,
    request_fits,
    sealed_prompt_tokens,
    sendable_max_tokens,
)
from common.sealed_config import read_sealed_toml
from common.stage import (
    DEFAULT_POD_PLACEMENT_CONFIG_PATH,
    DESIGNATOR_CHAIR,
    STRUCTURE_ANSWER_PARSED,
    STRUCTURE_ANSWER_RECORD_SCHEMA_V3,
    STRUCTURE_CALL_KIND,
    STRUCTURE_CALL_SCHEMA,
    STRUCTURE_DECODING_POLICY,
    validate_serving_provenance,
)
from operations.serving.client import ChairClient, ChairRequest, ChairResponse, serving_mode_for
from operations.serving.config import ServingConfigInputs, ServingRecipes, load_serving_recipes
from operations.serving.errors import ChairResponseRefusal, ChairTransportFailure, ServingError
from operations.serving.http import EndpointUnavailable, UrllibHttpTransport
from operations.serving.manager import (
    MECHANICS_QUALIFICATION_PURPOSE,
    ServingManager,
    StageContextReceiptPublisher,
)
from operations.serving.process import SubprocessLauncher
from operations.serving.residency import POD_RESIDENCY_LOCK_PATH, FileResidencyLease

# The per-page record's second parse state; the first is `common.stage`'s
# `STRUCTURE_ANSWER_PARSED`, named there because the consumer reads it back.
STRUCTURE_ANSWER_REFUSED: Final = "refused"

# Why a page was held by the live pass (SPEC_D §1.4). These live on
# structure-status.reason_code, not run.py::HOLD_REASON_CODES: the live path
# has no declared act to hold, only a page.
HELD_CUT_OFF: Final = "structure-answer-cut-off"
HELD_CALL_UNUSABLE: Final = "structure-call-unusable"
HELD_NO_INK_OVERLAP: Final = "structure-answer-no-ink-overlap"
# The response arrived and was retained, but custody could not bind it to the
# chair's serving receipt. Nothing mints from bytes nothing proves came from
# this call.
HELD_RESPONSE_NOT_RETAINED: Final = "structure-response-not-retained"
# Image tokens + prompt + a real answer exceed the row's max_model_len. Held
# before anything goes on the wire, since the engine's answer would be HTTP
# 400 anyway; another page may still fit, so this holds rather than aborts the
# run. The page is never silently downscaled to make it fit.
HELD_REQUEST_TOO_LARGE: Final = "structure-request-too-large"
# Cut off while spending its whole context on one repeated fragment: a
# degenerate generation, not a page too dense to describe, and named apart
# from HELD_CUT_OFF so a reader isn't sent to the token budget for this.
HELD_DEGENERATE: Final = "structure-answer-degenerate"
STRUCTURE_HELD_CODES: Final = frozenset(
    {
        HELD_CUT_OFF,
        HELD_DEGENERATE,
        HELD_CALL_UNUSABLE,
        HELD_NO_INK_OVERLAP,
        HELD_RESPONSE_NOT_RETAINED,
        HELD_REQUEST_TOO_LARGE,
    }
    # Chandra's layout grammar's own refusals, not the retired JSON contract's.
    | {f"structure-answer-{outcome}" for outcome in chandra_layout.PARSE_OUTCOMES}
)

# What a page's answer did to it: detected/fallback-tiles are the two
# structure_evidence values a scanned page's status carries; held is the third
# disposition, with a reason code instead.
DISPOSITION_DETECTED: Final = "detected"
DISPOSITION_FALLBACK_TILES: Final = "fallback-tiles"
DISPOSITION_HELD: Final = "held"

# The two act-group evidence values only this pass emits (SPEC_D §2.5): here
# the chair proposes and the scan corroborates, so a merged ink group under two
# proposals is recorded rather than refused (unlike the fixture path, where a
# declared act is ground truth).
EVIDENCE_SHARED_DETECTION: Final = "shared-detection"
EVIDENCE_SPLIT_DETECTION: Final = "split-detection"
EVIDENCE_MODEL_ONLY: Final = "model-only"

# Why one of the answer's top-level blocks proposed no rectangle. Closed
# vocabulary, disjoint and ordered: blank-page is decided first, since a
# Blank-Page block carries no page geometry regardless of its own data-bbox.
NO_PROPOSAL_BLANK_PAGE: Final = "blank-page"
NO_PROPOSAL_NO_BBOX: Final = "no-bbox"
NO_PROPOSAL_MALFORMED_BBOX: Final = "malformed-bbox"
NO_PROPOSAL_REASONS: Final = frozenset(
    {NO_PROPOSAL_BLANK_PAGE, NO_PROPOSAL_NO_BBOX, NO_PROPOSAL_MALFORMED_BBOX}
)

# The finding kinds a live page's record can carry: this pass's own
# duplicate-rectangle plus the grammar's six, written out rather than derived
# from chandra_layout.LAYOUT_FINDING_KINDS, so a new grammar kind fails
# _reconcile_finding_kinds at import instead of passing through unexamined.
DUPLICATE_RECTANGLE_FINDING: Final = "duplicate-rectangle"
STRUCTURE_FINDING_KINDS: Final = frozenset(
    {
        DUPLICATE_RECTANGLE_FINDING,
        "malformed-bbox",
        "blank-page-retained",
        "nested-bbox-retained",
        "unclosed-block",
        "block-count-mismatch",
        "content-outside-blocks",
    }
)


def _reconcile_finding_kinds() -> None:
    """Refuse to import if the grammar raises a kind this stage cannot publish.

    Checked both directions: a grammar kind not declared here would reach a
    page record with no field set closed for it; a declared kind the grammar
    no longer raises is a dead branch nothing can ever exercise. A
    `RuntimeError`, not an `assert`, since this must survive `python -O`.
    """
    ours = STRUCTURE_FINDING_KINDS - {DUPLICATE_RECTANGLE_FINDING}
    theirs = frozenset(chandra_layout.LAYOUT_FINDING_KINDS)
    if ours != theirs:
        raise RuntimeError(
            "the structure pass and common/chandra_layout.py disagree about the layout "
            f"grammar's finding kinds: the grammar raises {sorted(theirs - ours)} this stage "
            f"does not declare, and this stage declares {sorted(ours - theirs)} the grammar "
            "does not raise"
        )


_reconcile_finding_kinds()

_SHARED_DETECTION_RATIONALE: Final = (
    "the ink scan found one region covering at least half of this rectangle, but the "
    "same region also covers another act the structure chair proposed; the scan did not "
    "detect a boundary between them, so it corroborates neither independently"
)
_SPLIT_DETECTION_RATIONALE: Final = (
    "two or more regions the ink scan found each cover at least half of this rectangle; the "
    "chair drew one act where the scan found several, and no single region corroborates the "
    "rectangle rather than one of its parts"
)
_MODEL_ONLY_RATIONALE: Final = (
    "no region the ink scan found covers half of this rectangle; the rectangle rests on "
    "the structure chair's proposal alone and the scan neither corroborates nor "
    "contradicts it"
)


# --- selection ----------------------------------------------------------------


def bound_serving_recipes(context: Any, recipes_path: str | Path) -> ServingRecipes:
    """The serving catalogue this run sealed, re-read and proven by digest.

    Restated here because a stage may not import another stage's module: the
    rows this stage decides live-or-fixture from must be the rows the run's
    config_digest covers, checked at the moment they are used.
    """
    if context.serving_config_inputs is None:
        raise ContractError(
            "this run authority seals no serving configuration inputs, so the catalogue that "
            "decides whether the structure chair is live cannot be proven"
        )
    try:
        recipes = load_serving_recipes(recipes_path)
        _, placement_sha256 = read_sealed_toml(
            DEFAULT_POD_PLACEMENT_CONFIG_PATH, "pod placement configuration"
        )
        ServingConfigInputs.from_record(dict(context.serving_config_inputs)).require_loaded(
            recipes_sha256=recipes.source_sha256,
            placement_sha256=placement_sha256,
        )
    except ServingError as error:
        raise ContractError(f"the sealed serving configuration was refused: {error}") from error
    return recipes


def resolved_structure_chair(context: Any) -> ChairIdentity:
    """The configured structure chair, refused rather than substituted when absent."""
    resolved = context.registry.resolve(DESIGNATOR_CHAIR)
    if isinstance(resolved, AbsentChair):
        raise ContractError(
            f"the Designator chair is explicitly absent: {resolved.reason}; "
            "no other chair may mark out structure"
        )
    if not isinstance(resolved, ChairIdentity):
        raise ContractError("Designator resolution returned neither an identity nor an absence")
    return resolved


def structure_serving_mode(context: Any, args: Any) -> tuple[str, ChairIdentity]:
    """`"fixture"` or `"live"` for the structure chair, by the sealed row kind alone.

    The selector is the sealed serving-recipe catalogue, never a flag and never
    the ingress route: the offline end-to-end run drives this live pass over
    fixture pages, and a real submission under the fixture catalogue is refused
    by `run.py::main`, not silently marked out by an ink scan.
    """
    identity = resolved_structure_chair(context)
    try:
        mode = serving_mode_for(
            bound_serving_recipes(context, args.serving_recipes_config),
            identity,
            args.placement_tier,
        )
    except ServingError as error:
        raise ContractError(
            f"the serving posture of the structure chair could not be resolved: {error}"
        ) from error
    return mode, identity


# --- decoding -----------------------------------------------------------------


def executable_temperature(policy: Mapping[str, Any]) -> int | float:
    """Return the sealed structure posture the serving seam sends verbatim."""
    return policy["structure"]["temperature"]


def structure_engine_call(decoding_config_sha256: str) -> dict[str, str]:
    """The closed posture record `common/stage.py` verifies on every structural seal."""
    return {
        "schema": STRUCTURE_CALL_SCHEMA,
        "call_kind": STRUCTURE_CALL_KIND,
        "decoding_policy": STRUCTURE_DECODING_POLICY,
        "decoding_config_sha256": decoding_config_sha256,
    }


def live_chair_record(
    context: Any,
    identity: ChairIdentity,
    receipt_ref: Mapping[str, str],
    engine_call: Mapping[str, str],
) -> dict[str, Any]:
    """The provenance block every live-path artifact carries.

    Built from the client's real serving receipt, never from
    `run.py::_configured_chair_record` (whose receipt is a declared
    `fixture://` value over a chair nothing called). Checked through
    `validate_serving_provenance` at construction.
    """
    record = {
        "chair": identity.role,
        "chair_state": "configured",
        "resolved_identity": identity.to_record(),
        "resolved_revision": {
            "kind": identity.receipt_revision_kind,
            "value": identity.receipt_revision,
        },
        "receipt_ref": dict(receipt_ref),
        "adapter_revision": context.adapter_revision,
        "engine_call": dict(engine_call),
    }
    validate_serving_provenance(context, record, producer_stage=DESIGNATOR, require_receipt=True)
    return record


# --- the production client --------------------------------------------------


def retain_chair_bytes(context: Any, data: bytes) -> dict[str, str]:
    """Store one chair response or call record under its own digest.

    The client retains before it parses. Guarded after the seal, since this
    writes into the stage's own blob directory, whose inventory the completion
    seal already witnessed.
    """
    if context.sealed:
        raise ContractError(
            "the Designator has sealed its completion boundary; retaining a chair response "
            "afterwards would make its witnessed blob inventory false"
        )
    digest, result = context.tree.put_blob(DESIGNATOR, data)
    return {"relative_path": result.relative_path, "sha256": digest}


def default_serving_factory(context: Any, identity: ChairIdentity, tier: str) -> ChairClient:
    """Build the client the live pass reads the structure chair through.

    Nothing here starts anything -- `ChairClient.__enter__` does, later, once.
    A stage test supplies its own factory (`main(serving_factory=...)`), the
    same in-process seam the Attestatores and the Perlector expose.
    """
    policy, decoding_sha256 = load_decoding_policy(context.args.decoding_config)
    manager = ServingManager(
        registry=context.registry,
        recipes=bound_serving_recipes(context, context.args.serving_recipes_config),
        config_inputs=ServingConfigInputs.from_record(dict(context.serving_config_inputs)),
        launcher=SubprocessLauncher(),
        http=UrllibHttpTransport(),
        receipt_publisher=StageContextReceiptPublisher(context),
        # One card, one resident chair, one lease: the shared container-local
        # lock path lets a still-running structure chair refuse a witness
        # rather than co-reside, across run trees.
        log_root=context.tree.resolve(context.tree.serving_log_path(DESIGNATOR)),
        residency_lease=FileResidencyLease(POD_RESIDENCY_LOCK_PATH),
        producer="pipeline/2_designator/run.py",
        _launch_purpose=(
            MECHANICS_QUALIFICATION_PURPOSE
            if getattr(context.args, "mechanics_qualification", False)
            else None
        ),
    )
    return ChairClient(
        manager=manager,
        identity=identity,
        tier=tier,
        retain=lambda data: retain_chair_bytes(context, data),
        decoding_config_sha256=decoding_sha256,
        # The already-checked sealed value, never a serving default.
        record_temperature=executable_temperature(policy),
        read_receipt=lambda reference: context.tree.read_run_receipt(dict(reference)),
    )


# --- the request ----------------------------------------------------------------


def _data_uri(image_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")


def structure_prompt_tokens() -> int:
    """The measured prompt-token cost of this pass's own prompt, digest-checked.

    No tokenizer runs here; the constant was measured against the structure
    chair's own tokenizer at the pinned revision and bound to a digest of the
    exact text `structure_prompt.messages()` returns, so editing the prompt
    refuses rather than leaving a stale count in force.
    """

    return sealed_prompt_tokens(
        DESIGNATOR_CHAIR, *(message["content"] for message in structure_prompt.messages())
    )


def prepare_page_request_image(
    context: Any,
    page_record: Mapping[str, Any],
    page_bytes: bytes,
    page_w: int,
    page_h: int,
) -> tuple[bytes, dict[str, Any], dict[str, str]]:
    """Publish Chandra's native request image while preserving its sealed source."""
    page_id = page_record["subject_id"]
    payload = page_record["payload"]
    page_ordinal = payload["ordinal"]
    source_ref = {
        "relative_path": payload["image_path"],
        "sha256": payload["source_sha256"],
    }
    verify_input_bytes(source_ref, page_bytes)
    if dimensions(page_bytes) != (page_w, page_h):
        raise ContractError(
            f"the Designator analysis for page {page_id} does not match its sealed pixel dimensions"
        )
    bounds = {"x": 0, "y": 0, "w": page_w, "h": page_h}
    try:
        model_image, target = render_page(page_bytes, bounds)
    except ValueError as error:
        raise SchemaRefusal(
            f"the Designator cannot reproduce Chandra's RGB scale_to_fit request image: {error}"
        ) from error
    image_sha256, image_blob = context.tree.put_blob(DESIGNATOR, model_image)
    presented = {
        "kind": "adapter-crop",
        "source_page_id": page_id,
        "source_page_ordinal": page_ordinal,
        "image_path": image_blob.relative_path,
        "image_sha256": image_sha256,
        "transform": presented_transform(page_id, page_ordinal, bounds, target),
    }
    validate_presented(presented, page_size=(page_w, page_h))
    validate_presented_page_binding(
        presented,
        page_ordinal=page_ordinal,
        page_image_path=payload["image_path"],
        page_sha256=payload["source_sha256"],
        page_size=(page_w, page_h),
        page_bytes=page_bytes,
    )
    evidence = {
        "schema": STRUCTURE_REQUEST_IMAGE_SCHEMA,
        "page_id": page_id,
        "page_ordinal": page_ordinal,
        "source_image_ref": source_ref,
        "presented": presented,
    }
    if set(evidence) != STRUCTURE_REQUEST_IMAGE_FIELDS:
        raise AssertionError(  # pragma: no cover - closed by construction
            f"{STRUCTURE_REQUEST_IMAGE_SCHEMA} built the wrong field set"
        )
    published = context.publish(
        kind=STRUCTURE_REQUEST_IMAGE_KIND,
        subject_id=page_id,
        outcome="proposed",
        inputs=[source_ref],
        payload=evidence,
    )
    return model_image, presented, context.input_ref(published.relative_path)


def page_capacity(profile: Any, image_w: int, image_h: int) -> dict[str, Any]:
    """Whether one whole-page structure request fits the sealed serving row.

    Admission is computed over the exact pixels put on the wire. The answer
    budget is the measured cost of an answer over a dense (800-word,
    six-block) page -- a row that cannot hold that cannot mark out a real
    register page, whatever it does with a sparse one.
    """

    return request_fits(
        profile,
        [(image_w, image_h)],
        structure_prompt_tokens(),
        dense_page_answer_budget(DESIGNATOR_CHAIR),
    )


def page_request(
    image_bytes: bytes,
    image_sha256: str,
    *,
    temperature: int | float,
    structure_recovery_seed: int | None = None,
    capacity: Mapping[str, Any] | None = None,
) -> ChairRequest:
    """One whole-page structure request with Chandra's native presented PNG.

    The digest is the retained native presentation's own digest; a separate
    transformation artifact binds those bytes back to the unchanged sealed page.

    `capacity` is the record this request was admitted on, copied onto the
    retained call record and used to derive the generation bound: Chandra's
    12,384-token `MAX_OUTPUT_TOKENS` is sent only where the row leaves strictly
    more than that, otherwise nothing is sent and the engine bounds generation
    by `max_model_len` itself. No capacity record means no bound either, since
    a bound is never sent on a guess.
    """
    # One user turn, no system turn, carrying Chandra's own prompt bytes.
    (user,) = structure_prompt.messages()
    messages = (
        {
            "role": user["role"],
            # Image block first, then instruction: Chandra's own inference
            # code appends the image before the text on every request it was
            # fine-tuned with.
            "content": [
                {"type": "image_url", "image_url": {"url": _data_uri(image_bytes)}},
                {"type": "text", "text": user["content"]},
            ],
        },
    )
    return ChairRequest(
        kind=STRUCTURE_CALL_KIND,
        messages=messages,
        image_sha256s=(image_sha256,),
        # Empty deliberately: the record shape this vendor bound is retained
        # under is shared with the other Chandra chair (live_witness.py), and
        # filling it in from one side would give one model two vendor views.
        generation_declared={},
        # The bound, plus the thinking-mode flag both Chandra chairs send.
        generation_sent={
            **({} if capacity is None else sendable_max_tokens(DESIGNATOR_CHAIR, capacity)),
            **chandra_wire_fields(),
        },
        capacity=capacity,
        structure_recovery_seed=structure_recovery_seed,
    )


# --- the answer -----------------------------------------------------------------


class MintedRectangle(TypedDict):
    """What the minting geometry reads from a rectangle before it is cut.

    Exactly two things: the block ordinal (`proposal_act_key`) and the
    page-pixel rectangle (`validated_rectangle`). A freshly parsed answer
    supplies them on a `StructureProposal`; a resumed pass supplies them on
    the sealed act row instead, which carries both fields under the same names
    -- so a sealed row can be minted from directly rather than reconstructing
    a `text` the tree doesn't hold just to satisfy a type.
    """

    ordinal: int
    raw_bounds: Bounds


_Rectangle = TypeVar("_Rectangle", bound=MintedRectangle)


class StructureProposal(MintedRectangle):
    """One of the chair's layout blocks, resolved to a rectangle on the sealed page.

    `ordinal` is the block's ordinal in the answer's document order, kept even
    where earlier blocks proposed nothing: one ordinal namespace for the whole
    page, so every proposal, `blocks_without_proposal` entry and finding joins
    to the same block rather than a re-numbered act sequence.
    """

    label: str
    label_declared: bool
    box_1000: list[int]
    text: str
    # data-bbox attributes below the block's own top level: evidence, never geometry.
    nested_bbox_count: int


@dataclasses.dataclass(frozen=True, slots=True)
class PageAnswer:
    """What one page's answer did to it, and the text-free record of the answer.

    `record` is the `structure-answer` payload (SPEC_D §1.3). `mint` is the
    list of rectangles the pass cuts, one per distinct rectangle: fresh
    `StructureProposal`s on a live answer, or sealed act rows on a resumed
    pass -- both are `MintedRectangle`s, which is every field the cutting reads.
    """

    ordinal: int
    page_id: str
    disposition: str
    reason_code: str | None
    mint: tuple[MintedRectangle, ...]
    record: dict[str, Any]


def _malformed_bbox_findings(
    findings: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """The grammar's `malformed-bbox` findings, indexed by the block they name."""
    return {
        finding["ordinal"]: finding for finding in findings if finding["kind"] == "malformed-bbox"
    }


def blocks_to_proposals(
    parsed: chandra_layout.ParsedLayout, page_w: int, page_h: int
) -> tuple[list[StructureProposal], list[dict[str, Any]]]:
    """The answer's blocks, split into what proposes a rectangle and what does not.

    Geometry comes from `chandra_layout.block_page_bounds`, which returns
    `None` -- never a substituted rectangle -- for a `Blank-Page` block or one
    whose `data-bbox` was absent or malformed. That `None` is the grammar's
    selection rule, not this function's own.

    Both halves come back: the second list is the record's
    `blocks_without_proposal`, naming a reason from `NO_PROPOSAL_REASONS` for
    each, so a page that minted three of eight blocks says what became of the
    other five without a reader opening the retained blob.
    """
    malformed = _malformed_bbox_findings(parsed["findings"])
    proposals: list[StructureProposal] = []
    without: list[dict[str, Any]] = []
    for block in parsed["blocks"]:
        bounds = chandra_layout.block_page_bounds(block, page_size=(page_w, page_h))
        if bounds is None:
            without.append(_block_without_proposal(block, malformed))
            continue
        box = block["bbox_1000"]
        if box is None:  # pragma: no cover - closed by `block_page_bounds`
            raise ContractError(
                f"block {block['ordinal']} resolved to page rectangle {bounds} from no "
                "normalized box; the grammar's geometry and its conversion disagree"
            )
        proposals.append(
            {
                "ordinal": block["ordinal"],
                "label": block["label"],
                "label_declared": block["label_declared"],
                "box_1000": list(box),
                "raw_bounds": bounds,
                "text": block["text"],
                "nested_bbox_count": len(block["nested_bboxes"]),
            }
        )
    return proposals, without


def _no_proposal_reason(
    block: chandra_layout.LayoutBlock, malformed: Mapping[int, Mapping[str, Any]]
) -> str:
    """Which of the three named cases kept this block out of the mint.

    Blank first, since a `Blank-Page` block has no page geometry regardless of
    its own `data-bbox`. Otherwise the grammar's own finding decides: its
    `data_bbox` is `None` exactly where the answer declared no attribute at all,
    telling "drew no box" apart from "drew a box nobody could read".
    """
    if block["blank_page"]:
        return NO_PROPOSAL_BLANK_PAGE
    finding = malformed.get(block["ordinal"])
    if finding is None:  # pragma: no cover - closed by `block_page_bounds`
        raise ContractError(
            f"block {block['ordinal']} resolved to no page rectangle and is not a Blank-Page, "
            "but the layout grammar recorded no malformed-bbox finding for it; a block may "
            "not leave the mint for a reason nothing named"
        )
    return NO_PROPOSAL_NO_BBOX if finding["data_bbox"] is None else NO_PROPOSAL_MALFORMED_BBOX


def _block_without_proposal(
    block: chandra_layout.LayoutBlock, malformed: Mapping[int, Mapping[str, Any]]
) -> dict[str, Any]:
    """One retained block that proposed nothing, as the published record carries it.

    Everything a proposal record carries except the geometry, reduced the same
    way: the chair's free strings as a digest and a length, never as text.
    `blank_page` is its own boolean beside the closed `reason` so a consumer
    counting blank pages needn't string-match a code that could grow a member.
    """
    return {
        "ordinal": block["ordinal"],
        "reason": _no_proposal_reason(block, malformed),
        "blank_page": block["blank_page"],
        **_label_fields(block["label"], block["label_declared"]),
        "text_digest": structure_answer.text_digest(block["text"]),
        "text_length": len(block["text"]),
        "nested_bbox_count": len(block["nested_bboxes"]),
    }


# The prompt's nineteen labels plus the vendor parser's default for an
# undeclared one, taken from the carried grammar rather than restated here.
_LABEL_VOCABULARY: Final[frozenset[str]] = frozenset(chandra_layout.OCR_LAYOUT_LABELS) | {
    chandra_layout.UNLABELLED_BLOCK_LABEL
}


def _label_fields(label: str, declared: bool) -> dict[str, Any]:
    """The chair's label: as a member of the vendor's own list, and as a digest.

    `label_vocabulary` is a closed-range membership test, not the chair's raw
    string, which is why it can be published in clear where `label_digest` and
    `label_length` are all this stage may say about a string the chair wrote.
    `null` is a real signal: a label outside the fixed list means the model
    answered outside the grammar it was asked in.
    """
    return {
        "label_vocabulary": label if label in _LABEL_VOCABULARY else None,
        "label_declared": declared,
        "label_digest": structure_answer.text_digest(label) if declared else None,
        "label_length": len(label) if declared else None,
    }


def dedupe_rectangles(
    acts: list[_Rectangle],
) -> tuple[list[_Rectangle], list[dict[str, Any]]]:
    """Mint each distinct rectangle once, recording the later ordinals as findings.

    Written over `MintedRectangle` rather than `StructureProposal` so the
    resumed pass reduces sealed act rows through this same function, not a
    second copy of the rule. Two identical rectangles are one crop; the first
    is minted, every later one becomes a `duplicate-rectangle` finding naming
    both ordinals rather than a refusal, which would lose every other act on
    the page over one the chair drew twice.
    """
    unique: list[_Rectangle] = []
    first_by_rectangle: dict[tuple[int, int, int, int], int] = {}
    findings: list[dict[str, Any]] = []
    for act in acts:
        bounds = act["raw_bounds"]
        key = (bounds["x"], bounds["y"], bounds["w"], bounds["h"])
        prior = first_by_rectangle.get(key)
        if prior is not None:
            findings.append(
                _designator_finding(
                    {
                        "kind": DUPLICATE_RECTANGLE_FINDING,
                        "ordinals": [prior, act["ordinal"]],
                    }
                )
            )
            continue
        first_by_rectangle[key] = act["ordinal"]
        unique.append(act)
    return unique, findings


def _designator_finding(finding: Mapping[str, Any]) -> dict[str, Any]:
    """One finding as a Designator artifact may carry it, refused if undeclared.

    Every kind is checked against `STRUCTURE_FINDING_KINDS`; the one field that
    carries model-written bytes (`malformed-bbox`'s quoted `data_bbox`) is
    reduced to a digest before publishing, matching this stage's standing rule
    that no string the chair wrote is published as text. `data_bbox_truncated`
    travels with the digest since `chandra_layout` quotes under a length bound.
    """
    kind = finding["kind"]
    if kind not in STRUCTURE_FINDING_KINDS:
        raise ContractError(
            f"the structure pass would publish a finding of kind {kind!r}, which is not one "
            f"of its declared kinds {sorted(STRUCTURE_FINDING_KINDS)}; a finding this stage "
            "cannot name is not one it can publish"
        )
    if kind != "malformed-bbox":
        return dict(finding)
    quoted = finding["data_bbox"]
    return {
        "kind": kind,
        "ordinal": finding["ordinal"],
        "reason": finding["reason"],
        "data_bbox_digest": None if quoted is None else structure_answer.text_digest(quoted),
        "data_bbox_truncated": finding["data_bbox_truncated"],
    }


def _overlap_area(a: Mapping[str, int], b: Mapping[str, int]) -> int:
    x0, y0 = max(a["x"], b["x"]), max(a["y"], b["y"])
    x1 = min(a["x"] + a["w"], b["x"] + b["w"])
    y1 = min(a["y"] + a["h"], b["y"] + b["h"])
    return max(0, x1 - x0) * max(0, y1 - y0)


def touches_ink(rectangle: Mapping[str, int], analysis: Mapping[str, Any]) -> bool:
    """Whether any ink pixel the page's own scan counted lies inside `rectangle`.

    A pixel test, not a bounding-box test: a component's box can overlap a
    rectangle that touches none of its ink. Components' boxes only bound where
    pixels are looked for, so a rectangle over blank paper costs nothing to
    test.

    The threshold is this page's own derived `ink_margin`, not the fixed
    `structure.PRIMARY_MARGIN` constant: a fixed threshold left a median of 39%
    of a real photographed page below it, making every rectangle touch ink by
    construction rather than by measurement.
    """
    background = analysis["background"]
    if background is None:
        return False
    threshold = background - analysis["ink_margin"]
    rows = analysis["rows"]
    for component in analysis["components"]:
        box = component["bounds"]
        x0, y0 = max(box["x"], rectangle["x"]), max(box["y"], rectangle["y"])
        x1 = min(box["x"] + box["w"], rectangle["x"] + rectangle["w"])
        y1 = min(box["y"] + box["h"], rectangle["y"] + rectangle["h"])
        for y in range(y0, y1):
            row = rows[y]
            for x in range(x0, x1):
                if row[x] <= threshold:
                    return True
    return False


# The share of an answer's overlapping 12-character fragments that may be the
# single most repeated one before it is read as a loop. Measured over four
# real pages: completed pages scored 0.003-0.004, looped ones 0.061-0.292; the
# floor sits an order of magnitude above the healthy pair and below the looping
# ones, since these registers really do repeat their own formulae.
DEGENERATE_REPETITION_SHARE: Final = 0.02
_REPETITION_WINDOW: Final = 12
# Answers shorter than this many windows score zero rather than being judged --
# the only answers this measure is asked about are cut-off ones that already
# ran to five figures of characters, so the floor excludes nothing real.
_MIN_WINDOWS_TO_JUDGE: Final = 48


def repetition_share(text: str) -> float:
    """How much of an answer is its own single most repeated fragment.

    Overlapping windows, so a run of one phrase scores by the length of the run
    rather than by how many whole copies fit. Short answers score zero: a
    fragment repeated twice in a sentence is not evidence of anything, and the
    only answers this question is asked of are ones that spent a whole context.
    """
    windows = len(text) - _REPETITION_WINDOW + 1
    if windows < _MIN_WINDOWS_TO_JUDGE:
        return 0.0
    counts = Counter(text[i : i + _REPETITION_WINDOW] for i in range(windows))
    return counts.most_common(1)[0][1] / windows


def _finish_reason_disposition(finish_reason: str | None, answer: str | None = None) -> str | None:
    """`None` for a complete or unreported stop; a held code for a cut-off.

    The engine's stop vocabulary is closed, and a word outside it is refused
    rather than folded into either bucket. A cut-off answer that spent its
    context repeating one fragment is held as `HELD_DEGENERATE` rather than
    `HELD_CUT_OFF`, since the fix for a loop is different from the fix for a
    page too dense to describe; a completed answer is never reclassified by
    this text measure.
    """
    if finish_reason is None or finish_reason in ENGINE_STOP_COMPLETE:
        return None
    if finish_reason in ENGINE_STOP_CUT_OFF:
        if answer is not None and repetition_share(answer) >= DEGENERATE_REPETITION_SHARE:
            return HELD_DEGENERATE
        return HELD_CUT_OFF
    raise ContractError(
        f"the structure chair's response carries finish_reason {finish_reason!r}, which is "
        f"neither {sorted(ENGINE_STOP_COMPLETE)} nor {sorted(ENGINE_STOP_CUT_OFF)}; an engine "
        "stop word outside the closed vocabulary is not a page outcome this pass can name"
    )


def _act_record(act: StructureProposal) -> dict[str, Any]:
    """One proposal as the published record carries it: geometry, text only by digest.

    Both free strings the chair returned (`text`, `label`) are reduced to a
    digest and a length, never published as text, since the Designator never
    establishes the authoritative transcription. `nested_bbox_count` counts
    `data-bbox` attributes below the block's own top level -- evidence the
    vendor otherwise deletes, kept visible without opening the retained blob.
    """
    return {
        "ordinal": act["ordinal"],
        "box_1000": list(act["box_1000"]),
        "raw_bounds": dict(act["raw_bounds"]),
        "text_digest": structure_answer.text_digest(act["text"]),
        "text_length": len(act["text"]),
        **_label_fields(act["label"], act["label_declared"]),
        "nested_bbox_count": act["nested_bbox_count"],
    }


def _refused_page_answer(
    client: ChairClient,
    *,
    page_id: str,
    ordinal: int,
    page_w: int,
    page_h: int,
    capacity: Mapping[str, Any],
    temperature: int | float,
    decoding_config_sha256: str,
    provenance: Mapping[str, Any],
    attempt_ordinal: int,
    attempt_seed: int,
    attempt_policy: Mapping[str, Any],
    presentation_ref: Mapping[str, str],
) -> "PageAnswer":
    """The record for a page whose request never went on the wire.

    Every field describing a response is explicitly null: no call was made.
    The receipt, served model id, and whole capacity record are what remain,
    and are enough on their own to reproduce the hold.
    """

    record = {
        "schema": STRUCTURE_ANSWER_RECORD_SCHEMA_V3,
        "page_id": page_id,
        "page_ordinal": ordinal,
        "page_w": page_w,
        "page_h": page_h,
        "prompt_version": structure_prompt.STRUCTURE_PROMPT_VERSION,
        "prompt_sha256": structure_prompt.prompt_sha256(),
        "answer_schema": structure_prompt.STRUCTURE_ANSWER_GRAMMAR,
        "text_view": chandra_layout.LAYOUT_TEXT_VIEW,
        "vendor": structure_prompt.vendor_identity(),
        "call_record_ref": None,
        "raw_response_ref": None,
        "custody_ref": None,
        "custody_problem": None,
        "receipt_ref": dict(client.handle.receipt_reference),
        "request_sha256": None,
        "finish_reason": None,
        "served_model_id": client.handle.profile.served_model_id,
        "call_problem": None,
        "parse_state": STRUCTURE_ANSWER_REFUSED,
        "parse_outcome": None,
        "disposition": DISPOSITION_HELD,
        "reason_code": HELD_REQUEST_TOO_LARGE,
        "block_count": 0,
        "act_count": 0,
        "acts": [],
        "blocks_without_proposal": [],
        "findings": [],
        "quantization": structure_answer.QUANTIZATION_RULE,
        "page_text_rule": structure_answer.PAGE_TEXT_RULE,
        "decoding": {
            "policy": STRUCTURE_DECODING_POLICY,
            "temperature": temperature,
            "decoding_config_sha256": decoding_config_sha256,
        },
        "provenance": dict(provenance),
        "capacity": dict(capacity),
        "attempt_ordinal": attempt_ordinal,
        "attempts": [],
        "attempt_seed": attempt_seed,
        "attempt_policy": dict(attempt_policy),
        "presentation_ref": dict(presentation_ref),
    }
    return PageAnswer(
        ordinal=ordinal,
        page_id=page_id,
        disposition=DISPOSITION_HELD,
        reason_code=HELD_REQUEST_TOO_LARGE,
        mint=(),
        record=record,
    )


def _failed_call_page_answer(
    *,
    page_id: str,
    ordinal: int,
    page_w: int,
    page_h: int,
    capacity: Mapping[str, Any],
    temperature: int | float,
    decoding_config_sha256: str,
    provenance: Mapping[str, Any],
    attempt_ordinal: int,
    attempt_seed: int,
    attempt_policy: Mapping[str, Any],
    presentation_ref: Mapping[str, str],
    refusal: ChairResponseRefusal | ChairTransportFailure,
) -> "PageAnswer":
    """Retain one failed dispatched call as a terminal held attempt."""
    record = {
        "schema": STRUCTURE_ANSWER_RECORD_SCHEMA_V3,
        "page_id": page_id,
        "page_ordinal": ordinal,
        "page_w": page_w,
        "page_h": page_h,
        "prompt_version": structure_prompt.STRUCTURE_PROMPT_VERSION,
        "prompt_sha256": structure_prompt.prompt_sha256(),
        "answer_schema": structure_prompt.STRUCTURE_ANSWER_GRAMMAR,
        "text_view": chandra_layout.LAYOUT_TEXT_VIEW,
        "vendor": structure_prompt.vendor_identity(),
        "call_record_ref": dict(refusal.call_record_ref),
        "raw_response_ref": (
            None if refusal.raw_response_ref is None else dict(refusal.raw_response_ref)
        ),
        "custody_ref": None,
        "custody_problem": None,
        "receipt_ref": dict(refusal.receipt_ref),
        "request_sha256": refusal.request_sha256,
        "finish_reason": None,
        "served_model_id": refusal.served_model_id,
        "call_problem": refusal.code,
        "parse_state": STRUCTURE_ANSWER_REFUSED,
        "parse_outcome": None,
        "disposition": DISPOSITION_HELD,
        "reason_code": HELD_CALL_UNUSABLE,
        "block_count": 0,
        "act_count": 0,
        "acts": [],
        "blocks_without_proposal": [],
        "findings": [],
        "quantization": structure_answer.QUANTIZATION_RULE,
        "page_text_rule": structure_answer.PAGE_TEXT_RULE,
        "decoding": {
            "policy": STRUCTURE_DECODING_POLICY,
            "temperature": temperature,
            "decoding_config_sha256": decoding_config_sha256,
        },
        "provenance": dict(provenance),
        "capacity": dict(capacity),
        "attempt_ordinal": attempt_ordinal,
        "attempts": [],
        "attempt_seed": attempt_seed,
        "attempt_policy": dict(attempt_policy),
        "presentation_ref": dict(presentation_ref),
    }
    return PageAnswer(
        ordinal=ordinal,
        page_id=page_id,
        disposition=DISPOSITION_HELD,
        reason_code=HELD_CALL_UNUSABLE,
        mint=(),
        record=record,
    )


def sealed_page_answer(record: Mapping[str, Any]) -> PageAnswer:
    """The answer a previous pass already published for this page, read back.

    A live chair cannot reproduce its own bytes: every answer embeds a
    `receipt_ref`/`call_record_ref`/`custody_ref` that moves when the chair is
    started again, so a resumed pass that asked a second time would build
    different bytes under an artifact identity the store already fixed and die
    on `IncompatibleReuse`. Reading the record back instead makes the pass
    idempotent per disposition:

    * `detected` -- status, crops, act groups and seal rows all come from the
      rectangles and provenance this record carries.
    * `fallback-tiles` -- the answer decides only that the page is tiled; the
      grid is rebuilt from the page's own dimensions and clipped against
      already-published regions, excluding the page's own fallback act so a
      second pass doesn't subtract the tiles from themselves.
    * `held` -- no crop was cut, so none is reproduced; the reason code and
      ink residual are unchanged.

    `mint` is rebuilt by putting the published act rows back through
    `dedupe_rectangles`, the same reduction the first pass applied. Only a
    `detected` page mints anything.
    """
    disposition = record["disposition"]
    minted: tuple[MintedRectangle, ...] = ()
    if disposition == DISPOSITION_DETECTED:
        rows = [cast(MintedRectangle, dict(act)) for act in record["acts"]]
        minted = tuple(dedupe_rectangles(rows)[0])
        if not minted:
            # detected means "at least one rectangle was proposed", so a
            # record carrying that word with no act row can't be reproduced.
            raise ContractError(
                f"the sealed structure answer for page {record['page_id']} says "
                f"{DISPOSITION_DETECTED!r} and carries no act to mint; a resumed pass cannot "
                "reproduce the crops that record's own page already has"
            )
    return PageAnswer(
        ordinal=record["page_ordinal"],
        page_id=record["page_id"],
        disposition=disposition,
        reason_code=record["reason_code"],
        mint=minted,
        record=dict(record),
    )


def ask_page(
    context: Any,
    client: ChairClient,
    page_record: Mapping[str, Any],
    ordinal: int,
    page_bytes: bytes,
    analysis: Mapping[str, Any],
    *,
    temperature: int | float,
    decoding_config_sha256: str,
    provenance: Mapping[str, Any],
    attempt_ordinal: int = 1,
    attempt_policy: Mapping[str, Any] | None = None,
) -> PageAnswer:
    """Ask the chair about one sealed page and decide what the answer does to it.

    In order: derive and retain Chandra's native request image; compute
    request capacity against the sealed serving row from those exact pixels
    (a page that can't fit is held under `HELD_REQUEST_TOO_LARGE` with no
    request built or sent, since the engine's own answer would be HTTP 400);
    build and send the request; bind the response under custody to the
    chair's receipt; parse and dispatch through SPEC_D §1.4's table.

    Three refusal scopes stay distinct: a transport failure or an HTTP/source
    refusal after dispatch becomes one terminal held attempt (so resume can't
    duplicate a possibly completed inference); a pre-client serving failure
    aborts, since no durable call record exists to publish (the request-image
    artefact remains); a custody refusal is one
    page's outcome, not the run's -- the bytes were retained but the binding
    that proves which call produced them couldn't be, so only this page holds.
    """
    page_id = page_record["subject_id"]
    page_w, page_h = analysis["width"], analysis["height"]
    if attempt_policy is None:
        attempt_policy = {"max_attempts": 1, "seed_schedule": "fixed-base"}
    if (
        not isinstance(attempt_ordinal, int)
        or isinstance(attempt_ordinal, bool)
        or not 1 <= attempt_ordinal <= attempt_policy.get("max_attempts", 0)
    ):
        raise ContractError("structure attempt ordinal must be a positive integer")
    schedule = attempt_policy.get("seed_schedule")
    if schedule == "fixed-base":
        attempt_seed = client.handle.profile.seed
    elif schedule == "base-plus-attempt-ordinal-minus-one":
        attempt_seed = client.handle.profile.seed + attempt_ordinal - 1
    else:
        raise ContractError(f"unsupported structure recovery seed schedule {schedule!r}")
    request_image, presented, presentation_ref = prepare_page_request_image(
        context, page_record, page_bytes, page_w, page_h
    )
    resize = presented["transform"]["resize"]
    capacity = page_capacity(
        client.handle.profile,
        resize["target_width_px"],
        resize["target_height_px"],
    )
    if not capacity["fits"]:
        return _refused_page_answer(
            client,
            page_id=page_id,
            ordinal=ordinal,
            page_w=page_w,
            page_h=page_h,
            capacity=capacity,
            temperature=temperature,
            decoding_config_sha256=decoding_config_sha256,
            provenance=provenance,
            attempt_ordinal=attempt_ordinal,
            attempt_seed=attempt_seed,
            attempt_policy=attempt_policy,
            presentation_ref=presentation_ref,
        )
    request = page_request(
        request_image,
        presented["image_sha256"],
        temperature=temperature,
        capacity=capacity,
        structure_recovery_seed=attempt_seed if attempt_ordinal > 1 else None,
    )
    try:
        response: ChairResponse = client.read(request)
    except (ChairResponseRefusal, ChairTransportFailure) as error:
        if (
            error.call_record_ref is not None
            and error.request_sha256 is not None
            and error.receipt_ref is not None
            and error.served_model_id is not None
            and (isinstance(error, ChairTransportFailure) or error.raw_response_ref is not None)
        ):
            return _failed_call_page_answer(
                page_id=page_id,
                ordinal=ordinal,
                page_w=page_w,
                page_h=page_h,
                capacity=capacity,
                temperature=temperature,
                decoding_config_sha256=decoding_config_sha256,
                provenance=provenance,
                attempt_ordinal=attempt_ordinal,
                attempt_seed=attempt_seed,
                attempt_policy=attempt_policy,
                presentation_ref=presentation_ref,
                refusal=error,
            )
        raise ContractError(
            f"the structure chair refused page {ordinal} without a durable call record: {error}"
        ) from error
    except (ServingError, EndpointUnavailable) as error:
        raise ContractError(
            f"the structure chair could not be asked about page {ordinal}: {error}; nothing was "
            "published for the page"
        ) from error
    # A custody refusal holds this page rather than aborting the run: the bytes
    # were already retained, and losing only the binding costs exactly this page.
    custody: dict[str, Any] | None = None
    custody_problem: str | None = None
    try:
        custody = retain_chandra_response(
            context.tree,
            response.raw_response,
            dict(client.handle.receipt_reference),
            page_id=page_id,
            page_ordinal=ordinal,
        )
    except SchemaRefusal as error:
        custody_problem = str(error)

    parsed: chandra_layout.ParsedLayout | None = None
    parse_outcome: str | None = None
    proposals: list[StructureProposal] = []
    without_proposal: list[dict[str, Any]] = []
    if response.parse_problem is None:
        content = response.content if response.content is not None else ""
        result = chandra_layout.parse_layout_html(content.encode("utf-8"))
        if chandra_layout.is_refusal(result):
            parse_outcome = result["parse_outcome"]  # type: ignore[index]
        else:
            parsed = result  # type: ignore[assignment]
            proposals, without_proposal = blocks_to_proposals(parsed, page_w, page_h)

    mint: list[StructureProposal] = []
    # Carried onto the record regardless of disposition: a held page's own
    # findings are as much a fact as a scanned page's.
    findings: list[dict[str, Any]] = (
        [] if parsed is None else [_designator_finding(f) for f in parsed["findings"]]
    )
    if custody_problem is not None:
        disposition, reason_code = DISPOSITION_HELD, HELD_RESPONSE_NOT_RETAINED
    elif response.parse_problem is not None:
        disposition, reason_code = DISPOSITION_HELD, HELD_CALL_UNUSABLE
    else:
        # Stop word checked before parse outcome: a cut-off body that also
        # fails to parse is still a cut-off, not a parse refusal.
        cut_off = _finish_reason_disposition(response.finish_reason, response.content)
        if cut_off is not None:
            disposition, reason_code = DISPOSITION_HELD, cut_off
        elif parsed is None:
            disposition, reason_code = DISPOSITION_HELD, f"structure-answer-{parse_outcome}"
        else:
            unique, duplicates = dedupe_rectangles(proposals)
            findings = findings + duplicates
            if not unique:
                disposition, reason_code = DISPOSITION_FALLBACK_TILES, None
            elif analysis["structure_evidence"] == DISPOSITION_DETECTED and not any(
                touches_ink(act["raw_bounds"], analysis) for act in unique
            ):
                # The tripwire: the scan found ink and nothing the chair drew
                # touches any of it -- zero pixels, page-wide, not a threshold.
                disposition, reason_code = DISPOSITION_HELD, HELD_NO_INK_OVERLAP
            else:
                disposition, reason_code = DISPOSITION_DETECTED, None
                mint = unique
    if reason_code is not None and reason_code not in STRUCTURE_HELD_CODES:
        raise ContractError(  # pragma: no cover - closed by construction above
            f"page {ordinal} would be held under {reason_code!r}, which is not a declared "
            "structure hold code"
        )

    acts_record = [_act_record(act) for act in proposals]
    # Every block the grammar read is either a proposal or a named
    # non-proposal; a mismatch means one was lost between reader and record.
    block_count = 0 if parsed is None else len(parsed["blocks"])
    if len(acts_record) + len(without_proposal) != block_count:
        raise ContractError(  # pragma: no cover - closed by `blocks_to_proposals`
            f"page {ordinal}'s answer read {block_count} blocks but published "
            f"{len(acts_record)} proposals and {len(without_proposal)} blocks without one; "
            "a block that is in neither list has been lost"
        )
    record = {
        "schema": STRUCTURE_ANSWER_RECORD_SCHEMA_V3,
        "page_id": page_id,
        "page_ordinal": ordinal,
        "page_w": page_w,
        "page_h": page_h,
        "prompt_version": structure_prompt.STRUCTURE_PROMPT_VERSION,
        "prompt_sha256": structure_prompt.prompt_sha256(),
        "answer_schema": structure_prompt.STRUCTURE_ANSWER_GRAMMAR,
        # The rule each block's text_digest was taken under: this project's own
        # text view, not the vendor's.
        "text_view": chandra_layout.LAYOUT_TEXT_VIEW,
        # The vendor code whose prompt bytes were sent and grammar was read,
        # beside the serving block's model identity below.
        "vendor": structure_prompt.vendor_identity(),
        "call_record_ref": dict(response.call_record_ref),
        "raw_response_ref": None if custody is None else dict(custody["response_ref"]),
        "custody_ref": None if custody is None else dict(custody["custody_ref"]),
        "custody_problem": custody_problem,
        "receipt_ref": dict(response.receipt_ref),
        "request_sha256": response.request_sha256,
        "finish_reason": response.finish_reason,
        "served_model_id": response.served_model_id,
        "call_problem": response.parse_problem,
        "parse_state": STRUCTURE_ANSWER_PARSED if parsed is not None else STRUCTURE_ANSWER_REFUSED,
        "parse_outcome": parse_outcome,
        "disposition": disposition,
        "reason_code": reason_code,
        # block_count vs. act_count is the page's own denominator; the gap is
        # explained by blocks_without_proposal.
        "block_count": block_count,
        "act_count": len(acts_record),
        "acts": acts_record,
        "blocks_without_proposal": without_proposal,
        "findings": findings,
        # These two keep their v1 names (they name rules, not that retired
        # contract): the arithmetic is unchanged, so renaming it here would
        # falsely tell every stored record that it had moved.
        "quantization": structure_answer.QUANTIZATION_RULE,
        "page_text_rule": structure_answer.PAGE_TEXT_RULE,
        "decoding": {
            "policy": STRUCTURE_DECODING_POLICY,
            "temperature": temperature,
            "decoding_config_sha256": decoding_config_sha256,
        },
        "provenance": dict(provenance),
        "capacity": capacity,
        "attempt_ordinal": attempt_ordinal,
        "attempts": [],
        "attempt_seed": attempt_seed,
        "attempt_policy": dict(attempt_policy),
        "presentation_ref": dict(presentation_ref),
    }
    return PageAnswer(
        ordinal=ordinal,
        page_id=page_id,
        disposition=disposition,
        reason_code=reason_code,
        mint=tuple(mint),
        record=record,
    )


# --- minting geometry ----------------------------------------------------------


def validated_rectangle(act: MintedRectangle, page_w: int, page_h: int) -> Bounds:
    """The chair's rectangle in page pixels, checked against the page it was drawn on.

    Checked again on a resumed pass, over the rectangle read from the sealed
    record: a stored bounds that no longer fits its page is refused, not cut.
    """
    bounds = dict(act["raw_bounds"])
    geometry.validate_bounds(bounds, page_w, page_h, "structure-chair rectangle")
    return bounds  # type: ignore[return-value]


def proposal_act_key(page_ordinal: int, act_ordinal: int) -> str:
    """A label for a reviewer's eye and the seal's duplicate-key refusal, never identity.

    `act_ordinal` is the answer's block ordinal, so keys on a page whose
    earlier blocks proposed nothing aren't contiguous (`proposal:2:0` beside
    `proposal:2:3` is an ordinary page, not a lost act) -- the price of one
    shared ordinal namespace with the grammar's own.
    """
    return f"proposal:{page_ordinal}:{act_ordinal}"


def model_evidence_blocks(
    analysis: Mapping[str, Any], proposals: list[tuple[str, Mapping[str, int]]]
) -> list[dict[str, Any]]:
    """The structural-evidence block for every chair rectangle on one page. Never raises.

    Computed for the whole page at once so a merged boundary is recorded on
    *both* acts: when one ink group covers half of two proposed rectangles,
    neither is `detected` -- both are `shared-detection` (recorded, not
    refused, since here the chair proposes and the scan only corroborates).
    `model-only` is a rectangle no group covers half of. `split-detection` is
    the mirror case: two or more regions each cover half of one rectangle, so
    no single region corroborates it and picking the largest would be a picker.
    """
    # A fallback-tiled page's "groups" are the predetermined grid bands, not
    # ink components: they would corroborate any rectangle drawn on the page,
    # so every proposal there is recorded `model-only` instead.
    if analysis["structure_evidence"] != DISPOSITION_DETECTED:
        return [_model_only_block() for _ in proposals]
    groups = analysis["groups"]
    covering: list[dict[str, Any] | str | None] = []
    for _act_key, bounds in proposals:
        area = bounds["w"] * bounds["h"]
        # The one group covering half of this rectangle, if any; two or more
        # is recorded as split-detection rather than resolved in favour of one.
        halves = [group for group in groups if _overlap_area(group["bounds"], bounds) * 2 >= area]
        if len(halves) == 1:
            covering.append(halves[0])
        else:
            covering.append(EVIDENCE_SPLIT_DETECTION if halves else None)
    claimants: dict[str, list[str]] = {}
    for (act_key, _bounds), group in zip(proposals, covering, strict=True):
        if isinstance(group, dict):
            claimants.setdefault(digest_of(group), []).append(act_key)
    blocks = []
    for group in covering:
        if group is None:
            blocks.append(_model_only_block())
            continue
        if group == EVIDENCE_SPLIT_DETECTION:
            blocks.append(
                _uncorroborated_block(EVIDENCE_SPLIT_DETECTION, _SPLIT_DETECTION_RATIONALE)
            )
            continue
        shared = len(claimants[digest_of(group)]) > 1
        blocks.append(
            {
                "structure_evidence": EVIDENCE_SHARED_DETECTION if shared else DISPOSITION_DETECTED,
                "detected_bounds": dict(group["bounds"]),
                "body_member_count": len(group["body_members"]),
                "anchor_count": len(group["anchors"]),
                "rationale": _SHARED_DETECTION_RATIONALE if shared else group["rationale"],
            }
        )
    return blocks


def _uncorroborated_block(evidence: str, rationale: str) -> dict[str, Any]:
    """No single scanned region stands behind this rectangle, for one named reason.

    Null bounds and zero counts: reporting a region (either side of a split, or
    a union) would claim something nothing measured, and pick between them.
    """
    return {
        "structure_evidence": evidence,
        "detected_bounds": None,
        "body_member_count": 0,
        "anchor_count": 0,
        "rationale": rationale,
    }


def _model_only_block() -> dict[str, Any]:
    return _uncorroborated_block(EVIDENCE_MODEL_ONLY, _MODEL_ONLY_RATIONALE)
