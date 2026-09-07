"""The structure chair's side of the Designator's live pass (SPEC_D §1, §2).

Everything here faces the served chair: which pass a run selects, what is sent
per sealed page, what comes back and what it does to the page, and the
geometry the answer mints. Nothing here cuts a crop or writes a stage artifact
-- `run.py::live_initial_pass` does both, through the same `cut_minted_region`,
`publish_structure_status`, `_publish_page_fallback` and conservation that the
fixture pass uses, so a crop still has exactly one author and a page still has
exactly one status record whichever pass marked it out.

**What is sent.** One `chat-completions` request per sealed page, the whole
page, the exact sealed PNG bytes as a `data:image/png;base64` block, bound to
the Exemplar by `image_sha256s=(source_sha256,)` so the client's own digest
check refuses a request whose image is not the sealed page (ARCHITECTURE
invariant 3), and sent as a single `user` turn with the image block before the
instruction, which is how this chair's occupant -- Chandra -- is called by its
own inference code. No tiling: the only tiling policy in this tree is Surya's,
and the chair this pass serves is a page-level model. The generation bound is
`min(Chandra's own 12,384-token `MAX_OUTPUT_TOKENS`, `max_model_len` less this
request's measured image and prompt cost)`
(`common/request_capacity.py::sendable_max_tokens`), and the row term of that
`min` is expressed by sending no `max_tokens` at all -- which is what the
engine already does with it, measured by the component that holds the
tokenizer. So on every row this catalogue ships, where 12,384 is far above what
the row leaves, nothing is sent and nothing changes; a shorter row would carry
Chandra's own bound. A `"length"` stop still honestly means the answer did not
fit, and the page is held on it rather than read short (GOALS 1: a truncated
act list is a missed act).

**What comes back is Chandra's own layout HTML** (Tyrel, 2026-09-06: each
witness runs as its developers intended). The chair is asked in the vendor's
own `OCR_LAYOUT_PROMPT` bytes (`structure_prompt.py` v3, carried and sealed in
`common/chandra_layout.py`) and answers in the vendor's own grammar: top-level
`<div>` layout blocks carrying `data-bbox` in normalized 0-1000 coordinates and
`data-label` from the prompt's own nineteen. `common/chandra_layout.py::
parse_layout_html` reads it; this pass turns each block into a **structure
proposal** and never into an act's text.

**A block is not automatically a rectangle.** Three kinds of block reach this
pass with nothing to mint, and each is recorded rather than resolved:

* a block whose `data-bbox` could not be read is retained with `bbox_1000:
  null`, `raw_bounds: null` and a `malformed-bbox` finding naming its ordinal.
  It is **never minted**. The vendor substitutes `[0, 0, 1, 1]` here and prints
  a message claiming the full image; a rectangle the chair never drew would be
  cut, filed and read as an act (GOVERNANCE 2, GOALS 2).
* a `Blank-Page` block reports the chair's judgement that there is nothing on
  the page, and by the grammar's own rule carries no page geometry. It is
  retained with a `blank-page-retained` finding and mints nothing.
* character data outside every top-level block is ink in no block and in no
  span. `parse_layout_html` counts it as a `content-outside-blocks` finding and
  this pass carries that finding onto the page's record, because a page that
  parsed cleanly while words sat outside every rectangle is a missed act
  arriving under a successful status.

**What each answer does to the page** is the closed table in SPEC_D §1.4,
implemented by `ask_page`: a parsed, complete answer with at least one
mintable block marks the page `scanned`/`detected`; a parsed, complete answer
whose every block is the chair's own `Blank-Page` marks it
`scanned`/`fallback-tiles` and the page is cut into its predetermined crops
(Tyrel, 2026-08-11); a cut-off, an unparseable answer, an unusable call, an
answer whose every non-blank block lost its geometry, or a parsed answer whose
rectangles touch none of the ink the scan itself found holds the page under a
name from `STRUCTURE_HELD_CODES`. A transport or serving refusal is fatal, with
nothing published for the page. Nothing is repaired, retried, or re-asked
(GOVERNANCE 7).

**Decoding.** The pass runs under `config/decoding.toml`'s `[structure]`
section and never under `reading_of_record` (Tyrel, 2026-09-02): the
Attestatores keep the fixed posture; this pass may vary, sealed and recorded.
The value is read from the bytes the run sealed, rechecked by digest, and
recorded on every page's structure-answer record. What the live reading seam
can execute today is temperature 0 only (`operations/serving/client.py`
records the reading-of-record temperature and puts 0 on the wire), so
`executable_temperature` refuses any other sealed value by name rather than
running at 0 while the record says otherwise (GOVERNANCE 10).

**No picker.** The chair proposes rectangles; the ink scan corroborates them
(`model_evidence_blocks`) and never overrides them; nothing here ranks,
selects among, or repairs what the chair returned. A duplicate rectangle mints
once and is a recorded finding (SPEC_D §2.2), never a choice between the two.
Dropping an unplaceable block from the mint is not a selection either: it is
the absence of geometry, recorded, and the block keeps its row on the record.
"""

from __future__ import annotations

import base64
import dataclasses
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Mapping, TypedDict

import geometry
import structure
import structure_prompt

from common import chandra_layout
from common.chair_wire import chandra_wire_fields
from common.chairs.models import AbsentChair, ChairIdentity
from common.chandra_custody import retain_chandra_response
from common.contracts.canonical import digest_bytes, digest_of
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.serving import ENGINE_STOP_COMPLETE, ENGINE_STOP_CUT_OFF
from common.contracts.stages import DESIGNATOR
from common.decoding import load_decoding_policy
from common.imaging import Bounds
from common.request_capacity import (
    dense_page_answer_budget,
    request_fits,
    sealed_prompt_tokens,
    sendable_max_tokens,
)
from common.stage import (
    DEFAULT_POD_PLACEMENT_CONFIG_PATH,
    DESIGNATOR_CHAIR,
    STRUCTURE_ANSWER_PARSED,
    STRUCTURE_ANSWER_RECORD_SCHEMA,
    STRUCTURE_CALL_KIND,
    STRUCTURE_CALL_SCHEMA,
    STRUCTURE_DECODING_POLICY,
    validate_serving_provenance,
)

# The three pieces of `common/structure_answer.py` that are **not** its retired
# JSON acceptance and outlive it: the page-pixel conversion's declared rule
# name, the newline-between-delivered-texts join rule, and one digest function
# for a chair's free strings. `common/chandra_layout.py` imports the same
# module for `to_page_bounds` and `join_delivered_texts` themselves, so this is
# the shared home for the arithmetic both Chandra readings land in, not a
# leftover dependency on the wire contract this pass just retired.
#
# `QUANTIZATION_RULE`'s value still opens `structure-answer.v1`. That string is
# the identifier of the *conversion rule* -- low edges floored, far edges
# ceiled, into sealed-page pixels -- and the arithmetic behind it has not
# moved, so re-naming it would churn a published value to describe an unchanged
# computation. It shared a name with the wire contract; it was never the wire
# contract.
from common.structure_answer import PAGE_TEXT_RULE, QUANTIZATION_RULE, text_digest
from operations.serving.client import ChairClient, ChairRequest, ChairResponse, serving_mode_for
from operations.serving.config import ServingConfigInputs, ServingRecipes, load_serving_recipes
from operations.serving.errors import ServingError
from operations.serving.http import EndpointUnavailable, UrllibHttpTransport
from operations.serving.manager import ServingManager, StageContextReceiptPublisher
from operations.serving.process import SubprocessLauncher
from operations.serving.residency import FileResidencyLease

# The per-page record's second parse state; the first is `common.stage`'s
# `STRUCTURE_ANSWER_PARSED`, named there because the consumer reads it back.
STRUCTURE_ANSWER_REFUSED: Final = "refused"

# Why a page was held by the live pass, as a closed vocabulary
# (SPEC_D §1.4). These live on `structure-status.reason_code`, never in
# `run.py::HOLD_REASON_CODES`: on the live path there is no declared act to
# hold, only a page, and the page's own record is where its reason belongs.
HELD_CUT_OFF: Final = "structure-answer-cut-off"
HELD_CALL_UNUSABLE: Final = "structure-call-unusable"
HELD_NO_INK_OVERLAP: Final = "structure-answer-no-ink-overlap"
# The response arrived and the client retained it, but custody could not bind
# those bytes to the chair's own serving receipt
# (`common/chandra_custody.py`). Nothing is minted from an answer whose bytes
# no binding proves came from this call: `_verify_proposal_act_row` and every
# later reader are entitled to that binding, and a page minted without it would
# be a rectangle attributed to a call nothing ties it to (GOVERNANCE 6).
HELD_RESPONSE_NOT_RETAINED: Final = "structure-response-not-retained"
# The request does not fit the sealed serving row this chair runs under: the
# page's own image tokens, plus this prompt, plus a real answer, exceed the
# row's `max_model_len` (`common/request_capacity.py`).  Held before anything
# goes on the wire, because the engine's answer to such a request is HTTP 400
# and no reading at all.  A hold rather than a run refusal for the same reason
# every other code here is one: another page may be smaller, and abandoning the
# run would cost every page that fits (GOALS 1).  The page is *never* silently
# downscaled to make it fit -- 300 dpi is what `config/pdf_render.toml` argues
# is needed to read the ink, and trading a measurable refusal for an
# unmeasurable misreading is not this pass's decision to make.
HELD_REQUEST_TOO_LARGE: Final = "structure-request-too-large"
# The answer parsed and returned blocks, but every block that was not the
# chair's own `Blank-Page` declaration lost its geometry: a `data-bbox` this
# stage could not read (`chandra_layout.parse_bbox_attribute` names which rule
# each one failed).  Distinct from `fallback-tiles`, and the distinction is the
# whole reason this code exists.  Tiling a page here would record "the chair
# found nothing on this page" about a chair that found several things and
# described each of them with a rectangle nobody could read -- a wrong reading
# published under a successful status, which GOVERNANCE 2 and 10 both forbid.
# The page is held, its ink reconciles as conservation residual, and every
# block keeps its row and its `malformed-bbox` finding on the answer record.
HELD_BLOCKS_WITHOUT_GEOMETRY: Final = "structure-blocks-without-geometry"
STRUCTURE_HELD_CODES: Final = frozenset(
    {
        HELD_CUT_OFF,
        HELD_CALL_UNUSABLE,
        HELD_NO_INK_OVERLAP,
        HELD_RESPONSE_NOT_RETAINED,
        HELD_REQUEST_TOO_LARGE,
        HELD_BLOCKS_WITHOUT_GEOMETRY,
    }
    | {f"structure-answer-{outcome}" for outcome in chandra_layout.PARSE_OUTCOMES}
)

# What a page's answer did to it. `detected` and `fallback-tiles` are the two
# `structure_evidence` values a scanned page's status carries; `held` is the
# third disposition and carries a reason code instead.
DISPOSITION_DETECTED: Final = "detected"
DISPOSITION_FALLBACK_TILES: Final = "fallback-tiles"
DISPOSITION_HELD: Final = "held"

# The two act-group evidence values only this pass emits (SPEC_D §2.5). A
# fixture act is declared ground truth and a merged ink group under two of them
# is a refusal; here the chair is the proposer and the scan is corroboration,
# so the same fact is recorded rather than refused -- and recorded as *not*
# independent corroboration (GOVERNANCE 10), never as `detected`.
EVIDENCE_SHARED_DETECTION: Final = "shared-detection"
EVIDENCE_SPLIT_DETECTION: Final = "split-detection"
EVIDENCE_MODEL_ONLY: Final = "model-only"

# One card, one resident chair, one lease file for the whole run tree: the
# same names the Attestatores and the Perlector use, so a structure chair
# still running when a witness starts refuses instead of co-residing.
SERVING_LOG_DIRECTORY: Final = "serving-logs"
RESIDENCY_LOCK_FILE: Final = "pod-gpu.lock"

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

    The Attestatores' own check, restated here because a stage may not import
    another stage's module: the rows this stage decides live-or-fixture from
    must be the rows the run's `config_digest` covers, checked at the moment
    they are used, and the placement table beside them for the same reason
    (the manager carries the pair into every launch audit, which
    `StageContext.write_serving_launch_audit` compares to the sealed pair).
    """
    if context.serving_config_inputs is None:
        raise ContractError(
            "this run authority seals no serving configuration inputs, so the catalogue that "
            "decides whether the structure chair is live cannot be proven"
        )
    try:
        recipes = load_serving_recipes(recipes_path)
        placement_bytes = Path(DEFAULT_POD_PLACEMENT_CONFIG_PATH).read_bytes()
        ServingConfigInputs.from_record(dict(context.serving_config_inputs)).require_loaded(
            recipes_sha256=recipes.source_sha256,
            placement_sha256=digest_bytes(placement_bytes),
        )
    except OSError as error:
        raise ContractError(
            f"the sealed serving configuration could not be read: {error}"
        ) from error
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
    the ingress route (SPEC_D §5): the offline end-to-end run drives this live
    pass over fixture pages, and a real submission under the fixture catalogue
    is refused by `run.py::main`, not silently marked out by an ink scan.
    `serving_mode_for`'s refusals -- no row, no tier for a live row, a
    catalogue half fixture for one chair, an unsupported row -- surface in this
    stage's own vocabulary.
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
    """The sealed `[structure]` temperature, refused if the seam cannot execute it.

    `operations/serving/client.py` records the reading-of-record temperature
    and refuses construction under any other, and `request_body` puts 0 on the
    wire for every reading. Until that seam carries a per-call temperature, a
    sealed `[structure]` value other than 0 is a posture this pass would record
    without executing -- GOVERNANCE 10's confusion of a claim with a
    measurement -- so it is refused here, before any chair starts, by name.
    The refusal is the one honest way to make a non-zero setting visible
    rather than a silent zero on every call record.
    """
    temperature = policy["structure"]["temperature"]
    if temperature != 0:
        raise ContractError(
            f"config/decoding.toml [structure] declares temperature {temperature!r}, but the "
            "live reading seam (operations/serving/client.py) records and sends the "
            "reading-of-record temperature 0 only; a structure pass at that value cannot be "
            "executed as sealed, and running at 0 under a record that says otherwise would be "
            "a posture reported rather than executed. Seal 0, or widen the seam to carry the "
            "structure temperature per call"
        )
    return temperature


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
    `run.py::_configured_chair_record`, whose receipt is a declared
    `fixture://` value over a chair nothing called -- on a path that did call
    the chair that would be a fabricated serving moment (GOVERNANCE 6). Checked
    through `validate_serving_provenance` at construction, so the record the
    consumer will hold every structural row to is one this stage already
    proved against the registry, the sealed recipe, the sealed decoding digest
    and the digest-checked receipt.
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

    The client retains before it parses (GOVERNANCE 2). Guarded after the seal
    for the reason `StageContext._write_serving_blob` is: this writes into the
    stage's own blob directory, whose inventory the completion seal witnessed.
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

    Every part of it belongs to the run: the registry that resolved the chair,
    the receipt publisher bound to this `StageContext`, the catalogue the run
    sealed, and the decoding posture its digest covers. Nothing here starts
    anything -- `ChairClient.__enter__` does, later, once. A stage test
    supplies its own factory (`main(serving_factory=...)`), the same in-process
    seam the Attestatores and the Perlector expose, and deliberately not a
    command-line flag.
    """
    policy, decoding_sha256 = load_decoding_policy(context.args.decoding_config)
    manager = ServingManager(
        registry=context.registry,
        recipes=bound_serving_recipes(context, context.args.serving_recipes_config),
        config_inputs=ServingConfigInputs.from_record(dict(context.serving_config_inputs)),
        launcher=SubprocessLauncher(),
        http=UrllibHttpTransport(),
        receipt_publisher=StageContextReceiptPublisher(context),
        log_root=context.tree.resolve(f"2_designator/{SERVING_LOG_DIRECTORY}"),
        residency_lease=FileResidencyLease(context.tree.resolve(RESIDENCY_LOCK_FILE)),
        producer="pipeline/2_designator/run.py",
    )
    return ChairClient(
        manager=manager,
        identity=identity,
        tier=tier,
        retain=lambda data: retain_chair_bytes(context, data),
        decoding_config_sha256=decoding_sha256,
        # The seam's own contract: it records 0 and refuses anything else.
        # `executable_temperature` has already refused a sealed value the seam
        # cannot carry, so this is the sealed value, not a substitute for it.
        record_temperature=executable_temperature(policy),
        read_receipt=lambda reference: context.tree.read_run_receipt(dict(reference)),
    )


# --- the request ----------------------------------------------------------------


def _data_uri(image_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")


def structure_prompt_tokens() -> int:
    """The measured prompt-token cost of this pass's own prompt, digest-checked.

    No tokenizer runs here (`common/request_capacity.py` says why one cannot,
    offline, in this environment).  The constant was measured against the
    structure chair's own tokenizer and chat template at the revision
    `config/models-real.toml` pins, and it is bound to a digest of exactly the
    text `structure_prompt.messages()` returns -- so editing the prompt refuses
    rather than leaving a stale count in force.
    """

    return sealed_prompt_tokens(
        DESIGNATOR_CHAIR, *(message["content"] for message in structure_prompt.messages())
    )


def page_capacity(profile: Any, page_w: int, page_h: int) -> dict[str, Any]:
    """Whether one whole-page structure request fits the sealed serving row.

    The page is the only image this request carries, and it goes at its sealed
    size: this pass never resizes what it shows the chair.  The answer budget is
    the measured cost of a dense (800-word, six-act) page's answer -- a row that
    cannot hold that cannot mark out a real register page, whatever it does with
    a sparse one.

    **That budget was measured over the retired JSON answer, and the chair now
    answers in layout HTML.**  Stated rather than quietly relied on
    (GOVERNANCE 10): `MEASURED_DENSE_PAGE_ANSWER_TOKENS["designator_structure"]`
    is 1,575 tokens of `verbatus-structure-answer.v1`, and HTML pays for tags
    and attributes that the JSON budget never counted, so this number is a
    figure for a shape the chair no longer produces.  Re-measuring every chair's
    answer budget at the pinned tokenizers is one unit's whole job in the
    vendor-systems design and is not split across the units that changed the
    grammars; what this unit re-measured is the *prompt* half, which it owns.
    The direction of the error is the safe one for admission -- a bigger real
    answer means this reserves too little, so a row this admits could still
    overrun -- and an overrun is not silent: it arrives as `finish_reason
    "length"` and holds the page under `structure-answer-cut-off`.
    """

    return request_fits(
        profile,
        [(page_w, page_h)],
        structure_prompt_tokens(),
        dense_page_answer_budget(DESIGNATOR_CHAIR),
    )


def page_request(
    page_bytes: bytes, source_sha256: str, *, capacity: Mapping[str, Any] | None = None
) -> ChairRequest:
    """One whole-page structure request: the sealed prompt plus the sealed page.

    The image digest claimed beside the request is the Exemplar's own
    `source_sha256`, so the client's digest check binds the request to the
    sealed page rather than to whatever bytes happened to be read.

    ``capacity`` is the record this request was admitted on; the client copies
    it onto the retained call record so a run's receipts carry the arithmetic.
    It is also what the generation bound is derived from
    (`common/request_capacity.py::sendable_max_tokens`), which sends Chandra's
    own 12,384-token `MAX_OUTPUT_TOKENS` only where the row leaves strictly
    more than that and otherwise sends nothing, leaving the engine to bound
    generation by `max_model_len` exactly as this pass has always let it. A
    request built with no capacity record carries no bound either, because
    there is then no measured prompt cost to weigh it against and a bound is
    never sent on a guess; every production call site passes one.
    """
    # One `user` turn and no system turn: Chandra's own inference code sends
    # exactly that, and this chair's occupant is Chandra
    # (`structure_prompt.py`'s docstring carries the evidence and the v2 bump).
    (user,) = structure_prompt.messages()
    messages = (
        {
            "role": user["role"],
            # The image block first, then the instruction. The chat template
            # emits a message's parts in list order, so this order is the token
            # sequence the chair sees, and Chandra's own inference code
            # (`chandra/model/vllm.py`, `model/hf.py`) appends the image before
            # the text on every request it was fine-tuned and benchmarked with.
            # This pass sent the reverse until now. No measured token count
            # moves with it: the sealed constant is taken over the message
            # *texts* (`common/request_capacity.py`), and an image part carries
            # none.
            "content": [
                {"type": "image_url", "image_url": {"url": _data_uri(page_bytes)}},
                {"type": "text", "text": user["content"]},
            ],
        },
    )
    return ChairRequest(
        kind=STRUCTURE_CALL_KIND,
        messages=messages,
        image_sha256s=(source_sha256,),
        # Chandra's repository ships no sampling parameters of its own, so this
        # chair has no carried vendor view to retain as evidence.
        generation_declared={},
        # The bound, plus the thinking-mode flag both Chandra chairs send
        # (`common/chair_wire.py` carries the evidence and why it is safe under
        # either of the two chat templates the revision ships).
        generation_sent={
            **({} if capacity is None else sendable_max_tokens(DESIGNATOR_CHAIR, capacity)),
            **chandra_wire_fields(),
        },
        capacity=capacity,
    )


# --- the answer -----------------------------------------------------------------


class StructureProposal(TypedDict):
    """One layout block that carries a rectangle on the sealed page.

    Only geometry and the block's ordinal: the block's own text and label live
    in the retained response bytes and reach the record as a digest and a
    length, never as a value this stage carries around
    (`_block_record`, ARCHITECTURE -- the Designator establishes no
    transcription).  `ordinal` is the block's position in the answer, which is
    what joins a minted rectangle back to its row and to any finding about it.
    """

    ordinal: int
    bbox_1000: list[int]
    raw_bounds: Bounds


@dataclasses.dataclass(frozen=True, slots=True)
class PageAnswer:
    """What one page's answer did to it, and the text-free record of the answer.

    `record` is the `structure-answer` payload (SPEC_D §1.3), built here and
    published by `run.py` behind its own `_refuse_text_fields` boundary.
    `mint` is the list of rectangles the pass cuts, in reading order, one per
    distinct rectangle; `disposition` and `reason_code` are what the page's
    status will say.
    """

    ordinal: int
    page_id: str
    disposition: str
    reason_code: str | None
    mint: tuple[StructureProposal, ...]
    record: dict[str, Any]


def placeable_blocks(
    blocks: list[chandra_layout.LayoutBlock], page_w: int, page_h: int
) -> list[StructureProposal]:
    """Every block the layout grammar gives a sealed-page rectangle for, in order.

    `chandra_layout.block_page_bounds` returns `None` for a `Blank-Page` block
    and for one whose `data-bbox` could not be read, and there is no third
    answer and no default -- so this is the whole of the mint candidacy rule,
    and it is a fact about the grammar rather than a judgement of this pass.
    A block it skips is not discarded: it keeps its row on the answer record
    with null geometry, and the finding that says why is already beside it.

    The `bbox_1000 is None` arm is unreachable while `block_page_bounds` keeps
    its own rule -- it returns `None` for exactly that case -- and it is a plain
    check rather than an `assert` because an assertion vanishes under `python
    -O`, and what it would be guarding is a rectangle reaching `cut_minted_region`
    with no normalized box on its record.
    """
    page_size = (page_w, page_h)
    proposals: list[StructureProposal] = []
    for block in blocks:
        bounds = chandra_layout.block_page_bounds(block, page_size=page_size)
        bbox = block["bbox_1000"]
        if bounds is None or bbox is None:
            continue
        proposals.append(
            {"ordinal": block["ordinal"], "bbox_1000": list(bbox), "raw_bounds": bounds}
        )
    return proposals


def dedupe_rectangles(
    proposals: list[StructureProposal],
) -> tuple[list[StructureProposal], list[dict[str, Any]]]:
    """Mint each distinct rectangle once, recording the later ordinals as findings.

    The class-and-bounds identity has no ordinal namespace
    (`common/contracts/identities.py::act_bindings`), so two identical
    rectangles on one page are one crop. The first is minted; every later one
    is a `duplicate-rectangle` finding naming both ordinals, and its text stays
    in the retained blob. Not a refusal: GOVERNANCE 7, and a refusal here would
    lose every other act on the page over one the chair drew twice.
    """
    unique: list[StructureProposal] = []
    first_by_rectangle: dict[tuple[int, int, int, int], int] = {}
    findings: list[dict[str, Any]] = []
    for proposal in proposals:
        bounds = proposal["raw_bounds"]
        key = (bounds["x"], bounds["y"], bounds["w"], bounds["h"])
        prior = first_by_rectangle.get(key)
        if prior is not None:
            findings.append(
                {"kind": "duplicate-rectangle", "ordinals": [prior, proposal["ordinal"]]}
            )
            continue
        first_by_rectangle[key] = proposal["ordinal"]
        unique.append(proposal)
    return unique, findings


def _overlap_area(a: Mapping[str, int], b: Mapping[str, int]) -> int:
    x0, y0 = max(a["x"], b["x"]), max(a["y"], b["y"])
    x1 = min(a["x"] + a["w"], b["x"] + b["w"])
    y1 = min(a["y"] + a["h"], b["y"] + b["h"])
    return max(0, x1 - x0) * max(0, y1 - y0)


def touches_ink(rectangle: Mapping[str, int], analysis: Mapping[str, Any]) -> bool:
    """Whether any ink pixel the page's own scan counted lies inside `rectangle`.

    A pixel test, not a bounding-box test: a component's box can overlap a
    rectangle that touches none of its ink, and the tripwire this feeds
    (SPEC_D §1.4's last row) is about ink, page-wide, at zero pixels. The
    components' boxes only bound where the pixels are looked for, so a
    rectangle over blank paper costs nothing to test and a page the scan found
    no ink on returns False without reading a pixel.
    """
    background = analysis["background"]
    if background is None:
        return False
    threshold = background - structure.PRIMARY_MARGIN
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


def _finish_reason_disposition(finish_reason: str | None) -> str | None:
    """`None` for a complete or unreported stop; a held code for a cut-off.

    The engine's own vocabulary is closed (`common/contracts/serving.py`), and
    a word outside it is refused rather than folded into either bucket -- the
    same rule the Attestatores apply to a witness's stop word.
    """
    if finish_reason is None or finish_reason in ENGINE_STOP_COMPLETE:
        return None
    if finish_reason in ENGINE_STOP_CUT_OFF:
        return HELD_CUT_OFF
    raise ContractError(
        f"the structure chair's response carries finish_reason {finish_reason!r}, which is "
        f"neither {sorted(ENGINE_STOP_COMPLETE)} nor {sorted(ENGINE_STOP_CUT_OFF)}; an engine "
        "stop word outside the closed vocabulary is not a page outcome this pass can name"
    )


# The chair's own name for its answer, published on every live page record so
# a reader can tell which grammar read the retained bytes without opening them.
# Every value is `common/chandra_layout.py`'s own constant: nothing here names
# a vendor pin a second time, because two places to state one commit is one
# place for them to disagree. A re-parse under a different pin is then visibly
# different rather than silently so (the design's contract boundary, and
# GOVERNANCE 6's rule for the model identity restated for the grammar).
ANSWER_GRAMMAR: Final[Mapping[str, str]] = MappingProxyType(
    {
        "text_view": chandra_layout.LAYOUT_TEXT_VIEW,
        "repository": chandra_layout.VENDOR_REPOSITORY,
        "commit": chandra_layout.VENDOR_COMMIT,
        "licence": chandra_layout.VENDOR_LICENCE,
        "prompt_source": chandra_layout.VENDOR_PROMPT_SOURCE,
        "parser_source": chandra_layout.VENDOR_PARSER_SOURCE,
    }
)


def _block_record(block: chandra_layout.LayoutBlock, page_w: int, page_h: int) -> dict[str, Any]:
    """One layout block as the published record carries it: geometry, text by digest.

    Both free strings the chair returned are reduced the same way. `text` is
    the block's transcription and was never published. `label` is the chair's
    own word for the rectangle, and it is the chair's reading too: a marginal
    name or an index row is a whole act in these books (GLOSSARY, "act"), so a
    label is not a shorter kind of thing than a transcription -- it is the same
    kind of thing, shorter. A Designator artifact publishes neither, because
    the Designator never establishes the authoritative transcription
    (ARCHITECTURE) and `run.py::_refuse_text_fields` can only match field
    *names*. The digest and the length are what let a reader prove what the
    retained blob says without the record saying it.

    **Two structural booleans are published in clear, and they are not the
    label.** `label_declared` says whether the answer carried a `data-label` at
    all -- the vendor's own `if not label: label = "block"` default otherwise
    makes an absent label indistinguishable from one the chair wrote as
    `block`. `blank_page` says whether that label was the grammar's own
    `Blank-Page`, which is the fact that decides whether the block can be
    minted; deriving it downstream would mean publishing the label to let
    someone else compare it. Both are answers to a closed question, carry no
    span of the chair's prose, and are what the `blank-page-retained` finding
    beside them refers to.

    `box_1000` and `raw_bounds` are both `null` for a block whose `data-bbox`
    could not be read and for a `Blank-Page` block. Null is the honest value:
    the vendor substitutes a rectangle here, and a substituted rectangle on
    this record would be cut, filed and read as an act.
    """
    bbox = block["bbox_1000"]
    bounds = chandra_layout.block_page_bounds(block, page_size=(page_w, page_h))
    label = block["label"] if block["label_declared"] else None
    text = block["text"]
    return {
        "ordinal": block["ordinal"],
        "box_1000": None if bbox is None else list(bbox),
        "raw_bounds": None if bounds is None else dict(bounds),
        "label_declared": block["label_declared"],
        "blank_page": block["blank_page"],
        "text_digest": text_digest(text),
        "text_length": len(text),
        "label_digest": None if label is None else text_digest(label),
        "label_length": None if label is None else len(label),
    }


# Every finding kind the layout grammar can raise, mapped to the fields this
# record republishes -- and a kind outside the map refuses rather than
# publishing.  A projection rather than a pass-through for one reason:
# `chandra_layout`'s `malformed-bbox` finding quotes the model-written
# `data-bbox` under a bound, and this stage publishes no model-written string
# at all (`run.py::_refuse_text_fields` and `_block_record` above).  `reason`
# and `detail` survive because they are this repository's own sentences naming
# which rule failed, not bytes the chair wrote.  The retained response blob is
# where the attribute itself lives, and `ordinal` is what joins this finding to
# the block that carries it.
_PUBLISHED_FINDING_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    "malformed-bbox": ("ordinal", "reason"),
    "blank-page-retained": ("ordinal",),
    "nested-bbox-retained": ("blocks", "attributes"),
    "unclosed-block": ("ordinal", "detail"),
    "block-count-mismatch": ("parsed_blocks", "top_level_divs"),
    "content-outside-blocks": ("characters", "detail"),
}


def published_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The grammar's findings as the text-free record carries them, in order.

    Refuses an undeclared kind by name rather than dropping it: a finding the
    grammar raises and this record silently omits is exactly the "lost
    silently" GOVERNANCE 2 forbids, and it would be invisible -- the page would
    publish, parse and mint with one fewer fact on it.
    """
    published: list[dict[str, Any]] = []
    for finding in findings:
        kind = finding["kind"]
        fields = _PUBLISHED_FINDING_FIELDS.get(kind)
        if fields is None:
            raise ContractError(
                f"the layout grammar raised a {kind!r} finding, which this pass does not know "
                f"how to publish; the kinds it publishes are "
                f"{sorted(_PUBLISHED_FINDING_FIELDS)}. A finding is never dropped to let a "
                "page publish"
            )
        published.append({"kind": kind, **{name: finding[name] for name in fields}})
    return published


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
) -> "PageAnswer":
    """The record for a page whose request never went on the wire.

    Every field that describes a response is null, and says so by being null
    rather than by being absent: no call was made, so there is no call record,
    no retained body, no custody binding, no stop word and no parse outcome.
    What the record does carry is the receipt of the chair that would have
    answered, the served model id the row names, and the whole capacity record
    -- which is the entire evidence for the hold and is enough to reproduce it.
    """

    record = {
        "schema": STRUCTURE_ANSWER_RECORD_SCHEMA,
        "page_id": page_id,
        "page_ordinal": ordinal,
        "page_w": page_w,
        "page_h": page_h,
        "prompt_version": structure_prompt.STRUCTURE_PROMPT_VERSION,
        "prompt_sha256": structure_prompt.prompt_sha256(),
        "answer_grammar": dict(ANSWER_GRAMMAR),
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
        "act_count": 0,
        "acts": [],
        "findings": [],
        "quantization": QUANTIZATION_RULE,
        "page_text_rule": PAGE_TEXT_RULE,
        "decoding": {
            "policy": STRUCTURE_DECODING_POLICY,
            "temperature": temperature,
            "decoding_config_sha256": decoding_config_sha256,
        },
        "provenance": dict(provenance),
        "capacity": dict(capacity),
    }
    return PageAnswer(
        ordinal=ordinal,
        page_id=page_id,
        disposition=DISPOSITION_HELD,
        reason_code=HELD_REQUEST_TOO_LARGE,
        mint=(),
        record=record,
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
) -> PageAnswer:
    """Ask the chair about one sealed page and decide what the answer does to it.

    In the order the contract fixes: the request's *capacity* against the
    sealed serving row is computed first, and a page whose image tokens plus
    this prompt plus a real answer cannot fit the row's `max_model_len` is held
    under `HELD_REQUEST_TOO_LARGE` with nothing built and nothing sent -- the
    engine's answer to such a request is HTTP 400 and no reading, so it is
    refused here rather than on a card that bills by the hour. Then the request
    is built and sent through the client (which retains the raw bytes and the call record before parsing);
    the response is bound under custody to the chair's receipt
    (`common/chandra_custody.py`'s one-receipt binding, published on the record
    as `custody_ref`); then the answer is
    parsed with the closed contract and dispatched through SPEC_D §1.4's table.

    Two refusals with two different scopes. A serving or transport refusal
    propagates as this stage's own fatal refusal with nothing published for the
    page: no answer arrived, so there is nothing to publish. A **custody**
    refusal is one page's outcome, not the run's: the bytes arrived and were
    retained, and what could not be established is the binding that proves
    which call they came from, so the page is held under
    `HELD_RESPONSE_NOT_RETAINED` with its record published, and every other
    page keeps its answer.
    """
    page_id = page_record["subject_id"]
    payload = page_record["payload"]
    page_w, page_h = analysis["width"], analysis["height"]
    # Before anything is built or sent: does this page's request fit the sealed
    # row at all? A page whose image tokens plus this prompt plus a real answer
    # exceed `max_model_len` is refused by the engine with HTTP 400 and no
    # reading, so it is held here instead -- on this laptop, for free, with the
    # arithmetic published (GOVERNANCE 2: the refusal is visible, and it names
    # numbers rather than a guess).
    capacity = page_capacity(client.handle.profile, page_w, page_h)
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
        )
    request = page_request(page_bytes, payload["source_sha256"], capacity=capacity)
    try:
        response: ChairResponse = client.read(request)
    except (ServingError, EndpointUnavailable) as error:
        raise ContractError(
            f"the structure chair could not be asked about page {ordinal}: {error}; nothing was "
            "published for the page"
        ) from error
    # A custody refusal holds this page; it does not abort the run. The bytes
    # are not lost either way -- the client retained them and the call record
    # before this line was reached (GOVERNANCE 2, ARCHITECTURE invariant 4) --
    # so what a refusal here costs is the binding that proves *which call* they
    # came from, and that costs exactly this page. Aborting instead would
    # discard every other page's answer over one page's receipt, which is the
    # "lost act" GOALS 1 puts above everything.
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
    if response.parse_problem is None:
        content = response.content if response.content is not None else ""
        result = chandra_layout.parse_layout_html(content.encode("utf-8"))
        if chandra_layout.is_refusal(result):
            parse_outcome = result["parse_outcome"]  # type: ignore[typeddict-item]
        else:
            parsed = result  # type: ignore[assignment]

    mint: list[StructureProposal] = []
    # Computed from the parse and not from the disposition: a held page's
    # findings are the evidence for the hold, and a cut-off or no-ink-overlap
    # page that published an empty finding list would be hiding the
    # `unclosed-block`, `malformed-bbox` and `content-outside-blocks` facts that
    # say what the chair actually returned (GOVERNANCE 2).
    findings: list[dict[str, Any]] = (
        [] if parsed is None else published_findings(parsed["findings"])
    )
    if custody_problem is not None:
        # Checked before the body: an answer this run cannot bind to the call
        # that produced it proposes nothing, whatever it happens to say.
        disposition, reason_code = DISPOSITION_HELD, HELD_RESPONSE_NOT_RETAINED
    elif response.parse_problem is not None:
        disposition, reason_code = DISPOSITION_HELD, HELD_CALL_UNUSABLE
    else:
        # The engine's stop word is checked before the parse outcome: a
        # cut-off body that also fails to parse is still a cut-off, not a
        # parse refusal, and a stop word outside the closed vocabulary is
        # refused whether or not the body happened to parse.
        cut_off = _finish_reason_disposition(response.finish_reason)
        if cut_off is not None:
            # Held even though it may have parsed: a truncated act list is a
            # missed act either way.
            disposition, reason_code = DISPOSITION_HELD, cut_off
        elif parsed is None:
            disposition, reason_code = DISPOSITION_HELD, f"structure-answer-{parse_outcome}"
        else:
            placeable = placeable_blocks(parsed["blocks"], page_w, page_h)
            unique, duplicates = dedupe_rectangles(placeable)
            findings.extend(duplicates)
            if unique:
                if analysis["structure_evidence"] == DISPOSITION_DETECTED and not any(
                    touches_ink(proposal["raw_bounds"], analysis) for proposal in unique
                ):
                    # The coordinate-space tripwire: the scan found ink and
                    # nothing the chair drew touches any of it. Not a threshold
                    # -- zero pixels, page-wide -- and it fires only when the
                    # scan itself found ink.
                    disposition, reason_code = DISPOSITION_HELD, HELD_NO_INK_OVERLAP
                else:
                    disposition, reason_code = DISPOSITION_DETECTED, None
                    mint = unique
            elif any(
                block["bbox_1000"] is None and not block["blank_page"] for block in parsed["blocks"]
            ):
                # At least one block is one the chair meant to place and this
                # stage could not -- a `data-bbox` that was malformed, or absent
                # from a block the prompt asked to carry one. Both are the same
                # fact: the chair found something and said where in a way nobody
                # can read. Tiling here would record "nothing on this page"
                # over an answer that described several things (GOVERNANCE 2,
                # 10).
                disposition, reason_code = DISPOSITION_HELD, HELD_BLOCKS_WITHOUT_GEOMETRY
            else:
                # Nothing to place and nothing lost placing it. Reached only
                # when every block is the chair's own `Blank-Page`: a block that
                # is not blank and has a box is placeable and would be in
                # `unique`, and one that is not blank and has no box is the arm
                # above. The page is cut into its predetermined crops (Tyrel,
                # 2026-08-11), which is what "the chair sees no text" did before
                # this grammar too.
                disposition, reason_code = DISPOSITION_FALLBACK_TILES, None
    if reason_code is not None and reason_code not in STRUCTURE_HELD_CODES:
        raise ContractError(  # pragma: no cover - closed by construction above
            f"page {ordinal} would be held under {reason_code!r}, which is not a declared "
            "structure hold code"
        )

    blocks_record = [
        _block_record(block, page_w, page_h)
        for block in (parsed["blocks"] if parsed is not None else [])
    ]
    record = {
        "schema": STRUCTURE_ANSWER_RECORD_SCHEMA,
        "page_id": page_id,
        "page_ordinal": ordinal,
        "page_w": page_w,
        "page_h": page_h,
        "prompt_version": structure_prompt.STRUCTURE_PROMPT_VERSION,
        "prompt_sha256": structure_prompt.prompt_sha256(),
        "answer_grammar": dict(ANSWER_GRAMMAR),
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
        "act_count": len(blocks_record),
        "acts": blocks_record,
        "findings": findings,
        "quantization": QUANTIZATION_RULE,
        "page_text_rule": PAGE_TEXT_RULE,
        # The posture this call actually ran under, per call: the sealed
        # section by name, the value read from the sealed bytes, and the digest
        # of those bytes (Tyrel, 2026-09-02: sealed and recorded per run).
        "decoding": {
            "policy": STRUCTURE_DECODING_POLICY,
            "temperature": temperature,
            "decoding_config_sha256": decoding_config_sha256,
        },
        "provenance": dict(provenance),
        # The arithmetic this request was admitted on, published beside the
        # answer it produced. Present on every live page record, fit or held.
        "capacity": capacity,
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


def validated_rectangle(proposal: StructureProposal, page_w: int, page_h: int) -> Bounds:
    """The chair's rectangle in page pixels, checked against the page it was drawn on."""
    bounds = dict(proposal["raw_bounds"])
    geometry.validate_bounds(bounds, page_w, page_h, "structure-chair rectangle")
    return bounds  # type: ignore[return-value]


def proposal_act_key(page_ordinal: int, act_ordinal: int) -> str:
    """A label for a reviewer's eye and the seal's duplicate-key refusal, never identity."""
    return f"proposal:{page_ordinal}:{act_ordinal}"


def model_evidence_blocks(
    analysis: Mapping[str, Any], proposals: list[tuple[str, Mapping[str, int]]]
) -> list[dict[str, Any]]:
    """The structural-evidence block for every chair rectangle on one page. Never raises.

    Computed for the whole page at once so the merged-boundary case can be
    recorded on *both* acts: when one ink group covers at least half of two
    proposed rectangles, neither is `detected` -- both are `shared-detection`,
    which says the scan found one region where the chair drew two and did not
    detect the boundary between them. On the fixture path the same fact is a
    refusal (`run.py::_claim_structural_group`), because there the declared
    rectangles are ground truth; here the chair is the proposer and the scan is
    corroboration, so it is recorded (GOVERNANCE 10) and decides nothing.
    `model-only` is a rectangle no group covers half of: null bounds, zero
    counts, the rectangle standing on the proposal alone. A page whose scan
    found nothing (predetermined grid as its groups) corroborates nothing, so
    every rectangle on it is `model-only` rather than matched against bands
    that would cover anything.

    `split-detection` is the mirror of `shared-detection` and the reason the
    two ends of the tie are not one value: two or more scanned regions each
    cover at least half of *one* rectangle, so the chair drew one act where the
    scan found several. It carries null bounds and zero counts like
    `model-only`, because no single region is the corroborating one and picking
    the largest would be a picker (GOVERNANCE 3), but it is a different fact
    from "nothing covers this" and reading it as `model-only` would report a
    scan that found nothing where the scan in fact found too much
    (GOVERNANCE 10).
    """
    if analysis["structure_evidence"] != DISPOSITION_DETECTED:
        return [_model_only_block() for _ in proposals]
    groups = analysis["groups"]
    covering: list[dict[str, Any] | str | None] = []
    for _act_key, bounds in proposals:
        area = bounds["w"] * bounds["h"]
        # The one group covering at least half of this rectangle, if any: a
        # correspondence test between a proposal and the scan, the same
        # majority-overlap rule `run.py::_match_structural_group` applies to a
        # declared act, and not a ranking of anything. Two or more groups each
        # covering half is a tie the fixture path refuses; here it is recorded
        # as `split-detection`, its own fact, and never resolved in favour of
        # one of them.
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

    Null bounds and zero counts, because reporting a region here -- either of
    the two in a split, or a computed union of them -- would be a claim about
    something nothing measured (GOVERNANCE 10) and, in the split case, a choice
    between them (GOVERNANCE 3).
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
