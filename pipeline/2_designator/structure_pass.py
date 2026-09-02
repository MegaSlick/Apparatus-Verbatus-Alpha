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
invariant 3). No tiling: the only tiling policy in this tree is Surya's, and
the chair this pass serves is a page-level model. No `max_tokens`: the engine
bounds generation by `max_model_len`, so a `"length"` stop then honestly
means the answer did not fit, and the page is held on it rather than read
short (GOALS 1: a truncated act list is a missed act).

**What each answer does to the page** is the closed table in SPEC_D §1.4,
implemented by `ask_page`: a parsed, complete answer with acts marks the page
`scanned`/`detected`; a parsed, complete answer with no acts marks it
`scanned`/`fallback-tiles` and the page is cut into its predetermined crops
(Tyrel, 2026-08-11); a cut-off, an unparseable answer, an unusable call, or a
parsed answer whose rectangles touch none of the ink the scan itself found
holds the page under a name from `STRUCTURE_HELD_CODES`. A transport or
serving refusal is fatal, with nothing published for the page. Nothing is
repaired, retried, or re-asked (GOVERNANCE 7).

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
from typing import Any, Final, Mapping

import geometry
import structure
import structure_prompt

from common import structure_answer
from common.chairs.models import AbsentChair, ChairIdentity
from common.chandra_custody import retain_chandra_response
from common.contracts.canonical import digest_bytes, digest_of
from common.contracts.errors import ContractError
from common.contracts.serving import ENGINE_STOP_COMPLETE, ENGINE_STOP_CUT_OFF
from common.contracts.stages import DESIGNATOR
from common.decoding import load_decoding_policy
from common.imaging import Bounds
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
STRUCTURE_HELD_CODES: Final = frozenset(
    {HELD_CUT_OFF, HELD_CALL_UNUSABLE, HELD_NO_INK_OVERLAP}
    | {f"structure-answer-{outcome}" for outcome in structure_answer.PARSE_OUTCOMES}
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


def page_request(page_bytes: bytes, source_sha256: str) -> ChairRequest:
    """One whole-page structure request: the sealed prompt plus the sealed page.

    The image digest claimed beside the request is the Exemplar's own
    `source_sha256`, so the client's digest check binds the request to the
    sealed page rather than to whatever bytes happened to be read.
    """
    system, user = structure_prompt.messages()
    messages = (
        {"role": system["role"], "content": system["content"]},
        {
            "role": user["role"],
            "content": [
                {"type": "text", "text": user["content"]},
                {"type": "image_url", "image_url": {"url": _data_uri(page_bytes)}},
            ],
        },
    )
    return ChairRequest(
        kind=STRUCTURE_CALL_KIND,
        messages=messages,
        image_sha256s=(source_sha256,),
        generation_declared={},
        generation_sent={},
    )


# --- the answer -----------------------------------------------------------------


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
    mint: tuple[structure_answer.ParsedAct, ...]
    record: dict[str, Any]


def dedupe_rectangles(
    acts: list[structure_answer.ParsedAct],
) -> tuple[list[structure_answer.ParsedAct], list[dict[str, Any]]]:
    """Mint each distinct rectangle once, recording the later ordinals as findings.

    The class-and-bounds identity has no ordinal namespace
    (`common/contracts/identities.py::act_bindings`), so two identical
    rectangles on one page are one crop. The first is minted; every later one
    is a `duplicate-rectangle` finding naming both ordinals, and its text stays
    in the retained blob. Not a refusal: GOVERNANCE 7, and a refusal here would
    lose every other act on the page over one the chair drew twice.
    """
    unique: list[structure_answer.ParsedAct] = []
    first_by_rectangle: dict[tuple[int, int, int, int], int] = {}
    findings: list[dict[str, Any]] = []
    for act in acts:
        bounds = act["raw_bounds"]
        key = (bounds["x"], bounds["y"], bounds["w"], bounds["h"])
        prior = first_by_rectangle.get(key)
        if prior is not None:
            findings.append({"kind": "duplicate-rectangle", "ordinals": [prior, act["ordinal"]]})
            continue
        first_by_rectangle[key] = act["ordinal"]
        unique.append(act)
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

    In the order the contract fixes: the request is built and sent through the
    client (which retains the raw bytes and the call record before parsing);
    the response is bound under custody to the chair's receipt
    (`common/chandra_custody.py`, the one-receipt binding the Attestatores'
    intake reads back); then the answer is parsed with the closed contract and
    dispatched through SPEC_D §1.4's table. A serving or transport refusal
    propagates as this stage's own fatal refusal with nothing published for
    the page.
    """
    page_id = page_record["subject_id"]
    payload = page_record["payload"]
    page_w, page_h = analysis["width"], analysis["height"]
    request = page_request(page_bytes, payload["source_sha256"])
    try:
        response: ChairResponse = client.read(request)
    except (ServingError, EndpointUnavailable) as error:
        raise ContractError(
            f"the structure chair could not be asked about page {ordinal}: {error}; nothing was "
            "published for the page"
        ) from error
    custody = retain_chandra_response(
        context.tree,
        response.raw_response,
        dict(client.handle.receipt_reference),
        page_id=page_id,
        page_ordinal=ordinal,
    )

    parsed: structure_answer.ParsedAnswer | None = None
    parse_outcome: str | None = None
    if response.parse_problem is None:
        content = response.content if response.content is not None else ""
        result = structure_answer.parse(content.encode("utf-8"), page_w=page_w, page_h=page_h)
        if "parse_outcome" in result:
            parse_outcome = result["parse_outcome"]
        else:
            parsed = result  # type: ignore[assignment]

    mint: list[structure_answer.ParsedAct] = []
    findings: list[dict[str, Any]] = []
    if response.parse_problem is not None:
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
            unique, findings = dedupe_rectangles(parsed["acts"])
            if not unique:
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

    acts_record = [
        {
            "ordinal": act["ordinal"],
            "box_1000": list(act["box_1000"]),
            "raw_bounds": dict(act["raw_bounds"]),
            "text_digest": structure_answer.text_digest(act["text"]),
            "text_length": len(act["text"]),
            "label": act["label"],
        }
        for act in (parsed["acts"] if parsed is not None else [])
    ]
    record = {
        "schema": STRUCTURE_ANSWER_RECORD_SCHEMA,
        "page_id": page_id,
        "page_ordinal": ordinal,
        "page_w": page_w,
        "page_h": page_h,
        "prompt_version": structure_prompt.STRUCTURE_PROMPT_VERSION,
        "prompt_sha256": structure_prompt.prompt_sha256(),
        "answer_schema": structure_answer.STRUCTURE_ANSWER_SCHEMA,
        "call_record_ref": dict(response.call_record_ref),
        "raw_response_ref": dict(custody["response_ref"]),
        "custody_ref": dict(custody["custody_ref"]),
        "receipt_ref": dict(response.receipt_ref),
        "request_sha256": response.request_sha256,
        "finish_reason": response.finish_reason,
        "served_model_id": response.served_model_id,
        "call_problem": response.parse_problem,
        "parse_state": STRUCTURE_ANSWER_PARSED if parsed is not None else STRUCTURE_ANSWER_REFUSED,
        "parse_outcome": parse_outcome,
        "disposition": disposition,
        "reason_code": reason_code,
        "act_count": len(acts_record),
        "acts": acts_record,
        "findings": findings,
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


def validated_rectangle(act: structure_answer.ParsedAct, page_w: int, page_h: int) -> Bounds:
    """The chair's rectangle in page pixels, checked against the page it was drawn on."""
    bounds = dict(act["raw_bounds"])
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
    """
    if analysis["structure_evidence"] != DISPOSITION_DETECTED:
        return [_model_only_block() for _ in proposals]
    groups = analysis["groups"]
    covering: list[dict[str, Any] | None] = []
    for _act_key, bounds in proposals:
        area = bounds["w"] * bounds["h"]
        # The one group covering at least half of this rectangle, if any: a
        # correspondence test between a proposal and the scan, the same
        # majority-overlap rule `run.py::_match_structural_group` applies to a
        # declared act, and not a ranking of anything -- two groups each
        # covering half is a tie the fixture path refuses and this path records
        # as no single corroborating region.
        halves = [group for group in groups if _overlap_area(group["bounds"], bounds) * 2 >= area]
        covering.append(halves[0] if len(halves) == 1 else None)
    claimants: dict[str, list[str]] = {}
    for (act_key, _bounds), group in zip(proposals, covering, strict=True):
        if group is not None:
            claimants.setdefault(digest_of(group), []).append(act_key)
    blocks = []
    for group in covering:
        if group is None:
            blocks.append(_model_only_block())
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


def _model_only_block() -> dict[str, Any]:
    return {
        "structure_evidence": EVIDENCE_MODEL_ONLY,
        "detected_bounds": None,
        "body_member_count": 0,
        "anchor_count": 0,
        "rationale": _MODEL_ONLY_RATIONALE,
    }
