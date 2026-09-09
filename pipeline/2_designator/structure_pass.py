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
own inference code. The instruction itself is now the vendor's own
(`structure_prompt.py` v3, carrying `common/chandra_layout.py`'s
`OCR_LAYOUT_PROMPT`): tonight's ruling is that each witness is asked in its
developers' own bytes, and this chair's occupant is the same model
`attestator_1` seats. No tiling: the only tiling policy in this tree is Surya's,
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

**What comes back** is Chandra's layout HTML, read by
`common/chandra_layout.py::parse_layout_html` -- the one reader of that grammar
in this tree, shared with the page witness, so two readings of one page cannot
disagree about what a `data-bbox` means. Each top-level block whose geometry
resolves becomes one **structure proposal**: its rectangle through
`to_page_bounds` against the sealed page, its vendor label, and its text as a
digest and a length. A block whose `data-bbox` is malformed or absent, and a
block the answer labelled `Blank-Page`, propose nothing -- and are **recorded**,
in `blocks_without_proposal`, with the reason. Never minted, and never given a
substituted rectangle: the vendor's own parser prints "defaulting to full image"
and substitutes `[0, 0, 1, 1]`, which would put a crop a few pixels wide in the
page's corner under a chair's provenance (`chandra_layout.py`'s Departures).
The `content-outside-blocks` finding is carried onto the record for the reason
that module raises it: character data outside every block is in no proposal and
in no span, and a page that parsed clean without it would be a missed act under
a successful status (GOALS 1, GOVERNANCE 2).

**What each answer does to the page** is the closed table in SPEC_D §1.4,
implemented by `ask_page`: a parsed, complete answer with at least one proposal
marks the page `scanned`/`detected`; a parsed, complete answer that proposes
nothing marks it `scanned`/`fallback-tiles` and the page is cut into its
predetermined crops (Tyrel, 2026-08-11); a cut-off, an unparseable answer, an
unusable call, or a parsed answer whose rectangles touch none of the ink the
scan itself found holds the page under a name from `STRUCTURE_HELD_CODES`. A
transport or serving refusal is fatal, with nothing published for the page.
Nothing is repaired, retried, or re-asked (GOVERNANCE 7).

**A page whose blocks all failed to place is tiled, not held**, and the
difference from a page the chair answered `Blank-Page` is in the record rather
than in the disposition: `block_count`, `blocks_without_proposal` and the
findings say which happened. Tiling keeps the page covered by predetermined
crops, which is what GOALS 1 asks for; holding it would cost every act on it
until a reviewer looked. The claim `fallback-tiles` makes is "no rectangle was
proposed", and that claim is true in both cases.

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
"""

from __future__ import annotations

import base64
import dataclasses
from pathlib import Path
from typing import Any, Final, Mapping, TypedDict

import geometry
import structure_prompt

from common import chandra_layout, structure_answer
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
STRUCTURE_HELD_CODES: Final = frozenset(
    {
        HELD_CUT_OFF,
        HELD_CALL_UNUSABLE,
        HELD_NO_INK_OVERLAP,
        HELD_RESPONSE_NOT_RETAINED,
        HELD_REQUEST_TOO_LARGE,
    }
    # Chandra's layout grammar's own refusals, not the retired JSON contract's:
    # the answer is HTML now, so `invalid-json` and `missing-act-list` name
    # failures no answer can have any more, and `no-layout-blocks` and
    # `blocks-not-at-top-level` name two that this pass could not previously
    # tell apart (`chandra_layout.parse_layout_html`).
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

# Why one of the answer's top-level blocks proposed no rectangle. A closed
# vocabulary rather than a sentence, for the reason every other structural field
# here is one: a consumer must be able to count the three cases apart without
# parsing prose, and a block that fell out of the mint for a reason nobody named
# is exactly the silent loss GOVERNANCE 2 forbids. The three are disjoint and
# ordered -- `blank-page` is decided first, because a `Blank-Page` block carries
# no page geometry however well-formed its own `data-bbox` was
# (`chandra_layout.block_page_bounds`), and a block that is both blank and
# malformed is reported here as blank while its malformed box is still carried
# as its own finding.
NO_PROPOSAL_BLANK_PAGE: Final = "blank-page"
NO_PROPOSAL_NO_BBOX: Final = "no-bbox"
NO_PROPOSAL_MALFORMED_BBOX: Final = "malformed-bbox"
NO_PROPOSAL_REASONS: Final = frozenset(
    {NO_PROPOSAL_BLANK_PAGE, NO_PROPOSAL_NO_BBOX, NO_PROPOSAL_MALFORMED_BBOX}
)

# The finding kinds a live page's record can carry: this pass's own
# `duplicate-rectangle` plus the grammar's six.
#
# **Written out rather than derived from `chandra_layout.LAYOUT_FINDING_KINDS`,
# and that is the whole point.** A set built as `{duplicate-rectangle} |
# LAYOUT_FINDING_KINDS` would admit a seventh grammar kind the moment the
# grammar grew one -- `_designator_finding` would pass it through unexamined,
# with no field set closed for it here or in `run.py`, and the first page that
# raised it would be published unchecked or refused after the card had already
# been paid to read it. Enumerated here, a new grammar kind fails
# `_reconcile_finding_kinds` at import instead, which is the moment somebody can
# still decide what this stage should publish for it.
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

    Both directions, because they are different failures. A grammar kind absent
    from `STRUCTURE_FINDING_KINDS` is a finding that would reach a page record
    with nothing having closed its fields. A kind here that the grammar no
    longer raises is a dead branch and, worse, a field set in `run.py` still
    claiming to be reachable -- the sort of check that passes because nothing
    can ever exercise it.

    A `RuntimeError`, not an `assert`: this must survive `python -O`.
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
    size: this pass never resizes what it shows the chair.  The answer budget
    is the measured cost of an answer in this chair's own declared response
    shape over a dense (800-word, six-block) page -- a row that cannot hold that
    cannot mark out a real register page, whatever it does with a sparse one.
    Both terms moved with the vendor grammar and both were re-measured for it:
    the prompt from 325 to 593 tokens and the dense answer from 1,575 to 1,645
    (`common/request_capacity.py` carries the arithmetic and the harness).
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
    # One `user` turn and no system turn, carrying Chandra's own
    # `OCR_LAYOUT_PROMPT` bytes: Chandra's inference code sends exactly that
    # shape, and this chair's occupant is Chandra (`structure_prompt.py`'s
    # docstring carries the evidence and the v3 bump).
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
        # Empty, and no longer because there is nothing to declare: the vendor
        # does declare a generation bound, `chandra/settings.py`'s
        # `MAX_OUTPUT_TOKENS = 12384`, which `sendable_max_tokens` already
        # weighs below. What is not settled here is the *record* shape that
        # bound is retained under, because that is one shape for both Chandra
        # chairs and the other one is set in `live_witness.py`'s Chandra branch.
        # Filling this in from one side would give one model two vendor views.
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
    """One of the chair's layout blocks, resolved to a rectangle on the sealed page.

    `ordinal` is the **block's** ordinal in the answer, in document order, and
    it stays the block's ordinal even where earlier blocks proposed nothing: one
    ordinal namespace for the whole page, so a proposal, a
    `blocks_without_proposal` entry and every finding that names an ordinal all
    join to the same block. A contiguous, re-numbered act ordinal beside the
    grammar's own would be two numbering schemes in one record, and the first
    reader to join them on the wrong one would get a rectangle from a different
    block.
    """

    ordinal: int
    # The vendor's resolved label: the answer's `data-label`, or
    # `chandra_layout.UNLABELLED_BLOCK_LABEL` where it declared none.
    label: str
    label_declared: bool
    box_1000: list[int]
    raw_bounds: Bounds
    text: str
    # How many `data-bbox` attributes this block carried below its own top
    # level. Evidence, never geometry (`chandra_layout`'s third departure).
    nested_bbox_count: int


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

    Geometry comes from `chandra_layout.block_page_bounds`, which is
    `to_page_bounds` against the sealed page and returns `None` -- never a
    substituted rectangle -- for a `Blank-Page` block and for a block whose
    `data-bbox` was absent or malformed. That `None` is the whole selection rule
    here, and it is the grammar's, not this pass's: nothing in this function
    decides which blocks are worth minting, only which ones carry a rectangle at
    all (hard rule 8, GOVERNANCE 3).

    Both halves come back, because a block that proposed nothing is still
    something the chair returned. The second list is the record's
    `blocks_without_proposal`, and it names the reason from
    `NO_PROPOSAL_REASONS` for each -- so a page that minted three of eight
    blocks says what became of the other five without a reader opening the
    retained blob.
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
            # A named refusal rather than a bare `assert`, which `python -O`
            # removes: the two must agree, because a rectangle published beside
            # a null normalized box is a page-pixel claim with no answer behind
            # it.
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

    Blank first: a `Blank-Page` block has no page geometry whatever its own
    `data-bbox` said, so reporting it as a malformed box would name the wrong
    fact about it. Otherwise the grammar's own finding for this block decides,
    and its `data_bbox` is `None` exactly where the answer declared no attribute
    at all -- the one structural signal that tells "the chair drew no box" apart
    from "the chair drew a box nobody could read".
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

    Everything a proposal record carries except the geometry it does not have,
    reduced by the same rule: both of the chair's free strings as a digest and a
    length, never as text. The reason is a closed word, and `blank_page` is kept
    as its own boolean beside it because a consumer counting blank pages should
    not have to string-match a reason code that could grow a fourth member.
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


# The prompt's nineteen labels and the vendor parser's own default for a block
# that declared none. Twenty words, closed, and taken from the carried grammar
# rather than restated here, so a re-pin that changes the vendor's list changes
# this with it.
_LABEL_VOCABULARY: Final[frozenset[str]] = frozenset(chandra_layout.OCR_LAYOUT_LABELS) | {
    chandra_layout.UNLABELLED_BLOCK_LABEL
}


def _label_fields(label: str, declared: bool) -> dict[str, Any]:
    """The chair's label: as a member of the vendor's own list, and as a digest.

    `label_vocabulary` is **not** the chair's string. It is which of the twenty
    words the vendor's grammar admits -- the prompt's nineteen labels plus the
    `block` the vendor's parser defaults an undeclared one to -- the answer
    named, and `null` where it named something outside them. That is a
    membership test with a closed range, not a reading, which is what lets it be
    published in clear where `label_digest` and `label_length` are all a
    Designator artifact may say about a string the chair wrote (`_act_record`
    gives the reason in full).

    `null` here is a real signal rather than an absence: the prompt offers a
    fixed list, so a label outside it is the model answering outside the grammar
    it was asked in, and the digest and length still identify exactly what it
    wrote against the retained bytes.
    """
    return {
        "label_vocabulary": label if label in _LABEL_VOCABULARY else None,
        "label_declared": declared,
        "label_digest": structure_answer.text_digest(label) if declared else None,
        "label_length": len(label) if declared else None,
    }


def dedupe_rectangles(
    acts: list[StructureProposal],
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

    Two things happen here and they are different. Every kind is checked against
    `STRUCTURE_FINDING_KINDS`, which is enumerated rather than derived, so a
    kind nothing in this stage has closed a field set for cannot be published --
    a *grammar* kind is caught earlier still, by `_reconcile_finding_kinds` at
    import, and this catches the rest. And the one field that carries
    model-written bytes -- `malformed-bbox`'s quoted `data_bbox` -- is reduced to
    a digest before it is published.

    The reduction is the Designator's standing rule, not a new one. This stage
    publishes no string the chair wrote (`_act_record`), and a `data-bbox` is a
    string the chair wrote: an attribute the model fills freely, which on a
    misbehaving answer can hold whatever it was reading off the page. That it is
    *usually* four integers is not a property anything enforces -- the finding
    exists precisely because it was not four integers. `data_bbox_truncated`
    travels with the digest because `chandra_layout` quotes under a bound, so a
    reader knows whether the digest covers the whole value or its first
    `MAX_QUOTED_ATTRIBUTE_CHARACTERS`; the bytes themselves are in the retained
    blob either way.
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
    rectangle that touches none of its ink, and the tripwire this feeds
    (SPEC_D §1.4's last row) is about ink, page-wide, at zero pixels. The
    components' boxes only bound where the pixels are looked for, so a
    rectangle over blank paper costs nothing to test and a page the scan found
    no ink on returns False without reading a pixel.

    **The threshold is this page's own, not `structure.PRIMARY_MARGIN`.** It
    used to be the constant, and on photographed material that made the tripwire
    unable to fire: a fixed 20 grey levels below the paper *mode* left a median
    of 39% of a real page below the threshold, so every rectangle a chair could
    draw touched ink and the `model-only` signal was true by construction rather
    than by measurement. `analysis["ink_margin"]` is the
    margin the page derived for itself and the one `primary_scan` counted its
    components at, so this test now asks the same question of a rectangle that
    the scan asked of the page. It is `None` exactly where `background` is --
    the page whose background could not be inferred, which returns False on the
    line above.
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


def _act_record(act: StructureProposal) -> dict[str, Any]:
    """One proposal as the published record carries it: geometry, text only by digest.

    Both free strings the chair returned are reduced the same way. `text` is
    the page's transcription and was never published. `label` is the chair's
    own word for the rectangle, and it is the chair's reading too: a marginal
    name or an index row is a whole act in these books (GLOSSARY, "act"), so a
    label is not a shorter kind of thing than a transcription -- it is the same
    kind of thing, shorter. A Designator artifact publishes neither, because
    the Designator never establishes the authoritative transcription
    (ARCHITECTURE) and `run.py::_refuse_text_fields` can only match field
    *names*. The digest and the length are what let a reader prove what the
    retained blob says without the record saying it, and `null` on both stays
    the honest spelling of "the chair offered no label".

    What the vendor grammar adds is `label_vocabulary` (`_label_fields`), which
    is a closed-range membership answer rather than a string the chair wrote,
    and `nested_bbox_count`: how many `data-bbox` attributes this block carried
    *below* its own top level. The vendor deletes those; `chandra_layout` keeps
    them as evidence and derives no geometry from them, and the count is the
    part of that evidence a text-free record can carry. A block reporting
    twenty nested boxes is a model answering in a shape nobody asked for, and
    that is worth being visible without opening the blob.
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
    # The grammar's own findings are carried onto every page it read, whatever
    # the page's disposition turns out to be: a held page's `content-outside-
    # blocks` or `malformed-bbox` is exactly as much a fact as a scanned page's,
    # and losing it because the page was held is the loss GOVERNANCE 2 forbids.
    findings: list[dict[str, Any]] = (
        [] if parsed is None else [_designator_finding(f) for f in parsed["findings"]]
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
            unique, duplicates = dedupe_rectangles(proposals)
            findings = findings + duplicates
            if not unique:
                # No rectangle was proposed: an answer of nothing but
                # `Blank-Page`, or one whose every block failed to place. The
                # page is tiled either way and the record says which happened
                # (`block_count`, `blocks_without_proposal`, findings).
                disposition, reason_code = DISPOSITION_FALLBACK_TILES, None
            elif analysis["structure_evidence"] == DISPOSITION_DETECTED and not any(
                touches_ink(act["raw_bounds"], analysis) for act in unique
            ):
                # The coordinate-space tripwire: the scan found ink and nothing
                # the chair drew touches any of it. Not a threshold -- zero
                # pixels, page-wide -- and it fires only when the scan itself
                # found ink.
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
    # The record's own arithmetic, checked here rather than asserted in prose:
    # every block the grammar read is either a proposal or a named
    # non-proposal, and a page whose two lists do not add up to its block count
    # has lost one somewhere between the reader and the record.
    block_count = 0 if parsed is None else len(parsed["blocks"])
    if len(acts_record) + len(without_proposal) != block_count:
        raise ContractError(  # pragma: no cover - closed by `blocks_to_proposals`
            f"page {ordinal}'s answer read {block_count} blocks but published "
            f"{len(acts_record)} proposals and {len(without_proposal)} blocks without one; "
            "a block that is in neither list has been lost"
        )
    record = {
        "schema": STRUCTURE_ANSWER_RECORD_SCHEMA,
        "page_id": page_id,
        "page_ordinal": ordinal,
        "page_w": page_w,
        "page_h": page_h,
        "prompt_version": structure_prompt.STRUCTURE_PROMPT_VERSION,
        "prompt_sha256": structure_prompt.prompt_sha256(),
        "answer_schema": structure_prompt.STRUCTURE_ANSWER_GRAMMAR,
        # The rule each block's `text_digest` was taken under. Without it the
        # digest names bytes nobody can re-derive: the grammar's text view is
        # ours, not the vendor's (`chandra_layout`'s docstring states it in
        # full), so the record has to say which one produced its numbers.
        "text_view": chandra_layout.LAYOUT_TEXT_VIEW,
        # The other half of GOVERNANCE 6's provenance, added by tonight's
        # ruling: the serving block below names the model that answered, and
        # this names the vendor code whose prompt bytes were sent and whose
        # grammar was read. A re-parse under a different vendor pin is then
        # visibly different rather than silently so.
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
        # How many top-level blocks the grammar read, against how many of them
        # proposed a rectangle. The pair is the page's own denominator: a
        # `block_count` of eight beside an `act_count` of three says five blocks
        # came back and were not minted, and `blocks_without_proposal` says
        # which five and why.
        "block_count": block_count,
        "act_count": len(acts_record),
        "acts": acts_record,
        "blocks_without_proposal": without_proposal,
        "findings": findings,
        # These two keep their `structure-answer.v1` names, which now outlive
        # the JSON contract that was named after them. They name *rules*, not
        # that contract: the floor/ceil quantization is still exactly
        # `to_page_bounds`, and the page-text join is still
        # `join_delivered_texts` -- and `common/chandra_layout.py` calls both,
        # so the vendor grammar lands in the same page-pixel mapping at the same
        # offsets. Renaming an unchanged rule would tell every stored record
        # that the arithmetic had moved when it had not.
        "quantization": structure_answer.QUANTIZATION_RULE,
        "page_text_rule": structure_answer.PAGE_TEXT_RULE,
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


def validated_rectangle(act: StructureProposal, page_w: int, page_h: int) -> Bounds:
    """The chair's rectangle in page pixels, checked against the page it was drawn on."""
    bounds = dict(act["raw_bounds"])
    geometry.validate_bounds(bounds, page_w, page_h, "structure-chair rectangle")
    return bounds  # type: ignore[return-value]


def proposal_act_key(page_ordinal: int, act_ordinal: int) -> str:
    """A label for a reviewer's eye and the seal's duplicate-key refusal, never identity.

    `act_ordinal` is the answer's **block** ordinal, so the keys on a page whose
    earlier blocks proposed nothing are not contiguous -- `proposal:2:0` beside
    `proposal:2:3` is an ordinary page, not a lost act. They stay unique per
    page, which is all the seal's duplicate-key refusal asks of them, and
    non-contiguity is the price of one ordinal namespace: the alternative was a
    second, re-numbered sequence beside the grammar's own, and a reader joining
    a finding to a proposal on the wrong one would get a different block.
    """
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
