"""Attestatores: retain every witness attempt without changing its history.

Each Testimonium has two deliberately separate witness-facing fields. ``payload``
is the witness's native response, retained without shaping it into an imagined
common body schema. ``witness_reported`` is a witness's own confidence or status
claim, retained as a claim but never used to compute channel health. The latter is
computed here from the native response and the transport boundary.

Attempts are append-only. A re-read receives a new ordinal and artifact identity;
there is no current pointer to update. Consumers derive current from the newest
contiguous ordinal, so a later failed attempt remains visibly failed while the
earlier reading remains intact in history.

Two write paths, and both append. `--attempt-ordinal N` is the whole pass: every
configured chair, every expected act, at that one ordinal — the same command
twice writes the same bytes. `--operation reread --act <id> --chair <role>` moves
exactly one chair on one act, at the ordinal that chair's own history says comes
next; a reread happens because one witness failed on one act, and re-witnessing
the other chairs to reach it would re-read ink nobody doubted.

    python pipeline/3_attestatores/run.py --run-root <dir> --run-id <id>
    python pipeline/3_attestatores/run.py ... --operation reread --act <id> --chair <role>
"""

import json
import sys
from pathlib import Path
from typing import Any, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.alignment import align_to_anchor, load_alignment_limits, markup_text_view  # noqa: E402
from common.chairs.models import AbsentChair, ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal  # noqa: E402
from common.contracts.identities import artifact_id, attempt_id  # noqa: E402
from common.contracts.stages import ATTESTATORES, DESIGNATOR, PERLECTOR  # noqa: E402
from common.exemplar_boundary import verify_exemplar_crop_lineage  # noqa: E402
from common.fixture_identity import page_identity  # noqa: E402
from common.stage import (  # noqa: E402
    ATTEMPTED_WITNESS_OUTCOMES,
    EXIT_COMPLETE,
    EXIT_HELD,
    WITNESS_READING_OUTCOMES,
    expected_acts,
    fixture_serving_details,
    latest_attempt,
    open_context,
    run_stage,
    stage_parser,
    validate_serving_provenance,
)

# A witness may report one of these ordinal self-assessments. They are retained
# as testimony about its own response, never promoted into a model ranking or
# used to choose a witness. Six plain levels keep fixture and future adapters
# interoperable while refusing an unbounded integer scale or invented prose.
# `uncertain` and `unsure` are deliberately both admitted: real adapters emit
# both spellings, and collapsing them to one is R3's call when it meets those
# adapters, not this stage's.
WITNESS_CONFIDENCE_ORDINALS = frozenset({"certain", "high", "medium", "low", "uncertain", "unsure"})

DEFAULT_FORMAT_CAPABILITIES = {
    "can_express_uncertainty": False,
    "can_express_layout": False,
}


def _confidence_problem(value: Any, path: str = "witness_reported") -> str | None:
    """Validate every confidence claim in retained witness self-report JSON."""
    if isinstance(value, dict):
        for key in sorted(value):
            item = value[key]
            if key == "confidence" and (
                not isinstance(item, str) or item not in WITNESS_CONFIDENCE_ORDINALS
            ):
                return (
                    f"{path}.confidence is not a member of the closed ordinal set "
                    f"{sorted(WITNESS_CONFIDENCE_ORDINALS)}"
                )
            if problem := _confidence_problem(item, f"{path}.{key}"):
                return problem
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if problem := _confidence_problem(item, f"{path}[{index}]"):
                return problem
    return None


# The two write paths this program implements, checked against by name because
# `--operation` carries no argparse `choices` — the same parser serves every
# stage — so an unrecognized one otherwise falls through to the whole pass and a
# mistyped reread re-reads nothing while exiting 0.
OPERATIONS = frozenset({"initial", "reread"})

# A witness response is untrusted input: a several-thousand-deep nested value
# drives `_native_problem` past Python's recursion limit, and `RecursionError` is
# not a `ContractError`, so nothing between here and process exit catches it —
# one adversarial witness takes down the whole folder rather than its own attempt.
# Real transcription output nests a handful of levels deep, so this is headroom
# rather than a fit.
_MAX_NATIVE_DEPTH = 64


def proposed_regions(context, act_id: str) -> list[dict]:
    """Every original Designator region the chair was actually shown.

    A later recovery region is intentionally not substituted. A Testimonium binds
    to these exact pixel blobs, not to whichever crop happens to be current when a
    later consumer reads the run tree.
    """
    regions = []
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] == "region" and entry["subject_id"] == act_id:
            record = context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            validate_serving_provenance(
                context,
                record.get("payload", {}).get("provenance"),
                producer_stage=DESIGNATOR,
                require_receipt=True,
            )
            verify_exemplar_crop_lineage(context.tree, context.run, record)
            regions.append(record)
    proposed = [record for record in regions if record["payload"]["origin"] == "proposal"]
    if not proposed:
        raise ContractError(f"act {act_id} has no proposed region for a witness to read")
    return sorted(proposed, key=_region_ordinal)


def _region_ordinal(record: dict) -> int:
    ordinal = record.get("payload", {}).get("attempt_ordinal")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool):
        raise SchemaRefusal("a Designator region carries no integer attempt ordinal to order by")
    return ordinal


def region_references(regions: list[dict]) -> list[dict[str, str]]:
    """The public identity facts of the exact crops a chair saw."""
    return [
        {
            "region_id": record["payload"]["region_id"],
            "image_path": record["payload"]["image_path"],
            "image_sha256": record["payload"]["image_sha256"],
        }
        for record in regions
    ]


def region_inputs(context, regions: list[dict]) -> list[dict[str, str]]:
    """Bind each distinct crop blob once while retaining every region in payloads."""
    inputs = {}
    for record in regions:
        reference = context.input_ref(record["payload"]["image_path"])
        inputs[reference["relative_path"]] = reference
    return sorted(inputs.values(), key=lambda item: (item["relative_path"], item["sha256"]))


def _declared_for_ordinal(row: dict[str, Any], ordinal: int) -> bool:
    """Whether a fixture declaration belongs to this immutable attempt.

    Older fixture rows mean attempt one explicitly. Silently applying a declared
    first-attempt failure to every re-read would make the test seam a mutable
    outcome selector rather than a description of one attempt.
    """
    declared = row.get("attempt_ordinal", 1)
    if not isinstance(declared, int) or isinstance(declared, bool) or declared < 1:
        raise SchemaRefusal("a fixture witness declaration has no positive attempt ordinal")
    return declared == ordinal


def _declared_pairs(context, ordinal: int, fixture_key: str) -> set[tuple[str, str]]:
    """The (act, chair) pairs one fixture table declares for this exact attempt."""
    pairs = set()
    for row_number, row in enumerate(context.fixture.get(fixture_key, []), start=1):
        scenario = row.get("scenario")
        if not isinstance(scenario, str) or not scenario:
            raise SchemaRefusal(
                f"fixture [[{fixture_key}]] row {row_number} has no scenario: {row!r}"
            )
        if scenario == context.scenario and _declared_for_ordinal(row, ordinal):
            pair = (row["act_key"], row["chair"])
            if pair in pairs:
                raise SchemaRefusal(
                    f"fixture [[{fixture_key}]] declares {pair!r} twice for attempt ordinal "
                    f"{ordinal}; a repeated declaration is a copy-paste error or two answers "
                    "to one question, and neither may collapse silently into one"
                )
            pairs.add(pair)
    return pairs


# A Designator page-fallback act used to be recognized here, by its derived
# identity, and given `genuinely-empty` for every configured chair without any
# response boundary being consulted (Sol-S1). Nothing in this stage asks what
# kind of act it is reading any more: a fallback crop is a proposed region like
# any other, its chairs are asked like any other, and what comes back decides
# the outcome. The identity check that guarded the branch went with the branch --
# an unforgeable selector for a branch that must not exist is still the branch.


def declared_malformed(context, ordinal: int) -> dict[tuple[str, str], str]:
    """Fixture stand-in for a provider response the recording channel could not keep."""
    rows: dict[tuple[str, str], str] = {}
    fixture_key = "witness_malformed"
    for row_number, row in enumerate(context.fixture.get(fixture_key, []), start=1):
        scenario = row.get("scenario")
        if not isinstance(scenario, str) or not scenario:
            raise SchemaRefusal(
                f"fixture [[{fixture_key}]] row {row_number} has no scenario: {row!r}"
            )
        if scenario != context.scenario or not _declared_for_ordinal(row, ordinal):
            continue
        key = (row["act_key"], row["chair"])
        if key in rows:
            raise SchemaRefusal(f"fixture declares malformed witness output twice for {key!r}")
        reason = row.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise SchemaRefusal("a malformed witness declaration has no reason")
        rows[key] = reason
    return rows


def testimony_for(context, act_key: str, chair: str, ordinal: int) -> dict[str, Any] | None:
    """Return the fixture's response for this exact attempt.

    A scenario-specific declaration overrides a scenario-agnostic declaration.
    """
    base_matches = []
    scenario_matches = []
    for row in context.fixture["testimony"]:
        if row["act_key"] != act_key or row["chair"] != chair:
            continue
        if not _declared_for_ordinal(row, ordinal):
            continue
        declared_scenario = row.get("scenario")
        if declared_scenario is None:
            base_matches.append(row)
        elif declared_scenario == context.scenario:
            scenario_matches.append(row)
    matches = scenario_matches or base_matches
    if len(matches) > 1:
        raise SchemaRefusal(f"fixture declares more than one response for {(act_key, chair)!r}")
    return matches[0] if matches else None


def declared_response(
    context, act_key: str, chair: str, declarations: dict[str, Any]
) -> dict[str, Any] | None:
    """The one response the fixture declares this chair returned for this request.

    Two tables reach this boundary and both declare a *response*, never an
    outcome. `[[testimony]]` carries whatever the witness returned;
    `[[witness_empty]]` carries the one response whose whole body is the empty
    string, kept as its own table so a reviewer reading the fixture can see at a
    glance which chairs returned nothing. Either way the declared bytes come back
    through `prepared_response` and `resolve_attempt` derives the outcome from
    what was actually retained.

    The tables handled before this one -- `witness_failure`, `witness_not_run`,
    `witness_malformed` -- are its exact complement: each declares the *absence*
    of a usable response, which is the only thing that may name an outcome with
    nothing retained. That line is what this stage's `genuinely-empty` no longer
    crosses; there is no third shape, and in particular no act identity, that
    mints a completed reading without a response to it.

    Every `witness_empty` row is scenario-scoped, so it sits at the same
    precedence as a scenario-specific `[[testimony]]` row and overrides the
    scenario-agnostic base response for its own scenario exactly as one does --
    that is how a shipped blank scenario says "this chair returned nothing here"
    over the base table's declared text. Two declarations at *that* precedence
    are two answers to one question, and the elif chain would have resolved them
    silently in `witness_empty`'s favour. `declarations_for`'s cross-table check
    cannot see this pair, because `[[testimony]]` is looked up per request rather
    than collected into a declared-pair set.
    """
    response = testimony_for(context, act_key, chair, declarations["ordinal"])
    if (act_key, chair) not in declarations["empty"]:
        return response
    if response is not None and response.get("scenario") == context.scenario:
        raise SchemaRefusal(
            "fixture declares both an empty response and a scenario response for "
            f"{(act_key, chair)!r} at attempt ordinal {declarations['ordinal']}"
        )
    return {"payload": ""}


def _native_problem(value: Any, path: str = "payload", *, depth: int = 0) -> str | None:
    """Return why a native response cannot be retained as canonical JSON.

    The generic artifact writer rejects floats and malformed Unicode later, but a
    witness response must become a retained ``failed`` attempt rather than make a
    whole folder crash or be quietly repaired with ``str()``, replacement Unicode,
    or a shared text schema. This is deliberately strict until Spec 04 defines a
    binary provider-body contract.
    """
    if depth > _MAX_NATIVE_DEPTH:
        return f"{path} nests deeper than {_MAX_NATIVE_DEPTH} levels"
    if value is None or isinstance(value, (bool, int)):
        return None
    if isinstance(value, str):
        try:
            value.encode("utf-8", "strict")
        except UnicodeEncodeError:
            return f"{path} contains text that is not valid UTF-8"
        return None
    if isinstance(value, list):
        for index, item in enumerate(value):
            if problem := _native_problem(item, f"{path}[{index}]", depth=depth + 1):
                return problem
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                return f"{path} has a non-string object key"
            try:
                key.encode("utf-8", "strict")
            except UnicodeEncodeError:
                return f"{path} has an object key that is not valid UTF-8"
            if problem := _native_problem(item, f"{path}.{key}", depth=depth + 1):
                return problem
        return None
    return f"{path} has unsupported native type {type(value).__name__!r}"


def _native_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


# Every health field of a chair with no native response except the reason, so the
# writer below and the reader in `validate_content_health` cannot disagree about
# what "no response" looks like.
NO_RESPONSE_HEALTH = {
    "native_type": None,
    "encoding": "not-applicable",
    "recordable": None,
    "empty": None,
    "blank": None,
    "truncated": None,
    "characters": None,
}


def no_response_health(*, reason: str) -> dict[str, Any]:
    """Health for a chair with no native response, never an empty reading."""
    return {**NO_RESPONSE_HEALTH, "truncation_basis": reason}


# A `genuinely-empty` reading has no witness text at all -- there is nothing to
# locate in the anchor, so nothing was lost finding it either. Shared by the
# trivial-attachment branch below so a zero-length alignment always reads as
# exactly as empty as it is.
_ZERO_ALIGNMENT_LOSS: dict[str, int] = {
    "markup_characters": 0,
    "whitespace_characters": 0,
    "unicode_reencoded_characters": 0,
}


def content_health(native_payload: Any, *, completed: bool | None = None) -> dict[str, Any]:
    """Compute deterministic channel facts from native output alone.

    ``witness_reported`` intentionally is not an argument. A witness's assertion
    that it was confident, complete, or uncertain cannot become health merely by
    being present. The synthetic fixture is an explicit complete transport seam;
    real serving code must pass a trusted response-boundary fact or leave
    truncation unknown.
    """
    if (problem := _native_problem(native_payload)) is not None:
        return {
            "native_type": _native_type(native_payload),
            "encoding": "invalid-or-unrecordable",
            "recordable": False,
            "empty": None,
            "blank": None,
            "truncated": None,
            "characters": None,
            "truncation_basis": problem,
        }

    if isinstance(native_payload, str):
        empty = native_payload == ""
        blank = native_payload.strip() == ""
        characters: int | None = len(native_payload)
    elif isinstance(native_payload, (dict, list)):
        empty = len(native_payload) == 0
        blank = None
        characters = None
    else:
        empty = False
        blank = None
        characters = None
    return {
        "native_type": _native_type(native_payload),
        "encoding": "utf-8-json-native",
        "recordable": True,
        "empty": empty,
        "blank": blank,
        "truncated": None if completed is None else not completed,
        "characters": characters,
        "truncation_basis": (
            "trusted-response-boundary" if completed is not None else "not-recorded"
        ),
    }


def validate_content_health(native_payload: Any, health: Any) -> None:
    """Refuse a resealed health record that is not this stage's deterministic shape.

    A self-hashed artifact can still have been re-sealed with a damaged health
    field.  The tally must not count it as known simply because its envelope and
    JSON syntax remain valid.  Whether an unrecordable channel is an accounted
    failure or #23's UNKNOWN is not decided here — that is
    `require_accounted_unrecordable_channel`, which reads the outcome this
    function deliberately does not.
    """
    if not isinstance(health, dict):
        raise SchemaRefusal("a Testimonium carries no object content_health record")
    required = set(NO_RESPONSE_HEALTH) | {"truncation_basis"}
    if missing := sorted(required - set(health)):
        raise SchemaRefusal(f"a Testimonium content_health record lacks field(s) {missing}")
    if unexpected := sorted(set(health) - required):
        raise SchemaRefusal(
            f"a Testimonium content_health record carries unknown field(s) {unexpected}; "
            "health is computed here and its schema is closed, so a field nothing "
            "validates is a self-report wearing a computed field's name"
        )

    recordable = health["recordable"]
    if recordable is True:
        if problem := _native_problem(native_payload):
            raise SchemaRefusal(problem)
        expected = content_health(native_payload, completed=None)
        for field in ("native_type", "encoding", "recordable", "empty", "blank", "characters"):
            if health[field] != expected[field]:
                raise SchemaRefusal(f"a Testimonium has inconsistent content_health.{field}")
        truncated = health["truncated"]
        basis = health["truncation_basis"]
        if truncated is None:
            if basis != "not-recorded":
                raise SchemaRefusal(
                    "a Testimonium with unknown truncation lacks the not-recorded basis"
                )
        elif isinstance(truncated, bool):
            if basis != "trusted-response-boundary":
                raise SchemaRefusal(
                    "a Testimonium with a known truncation state lacks a trusted boundary"
                )
        else:
            raise SchemaRefusal("a Testimonium content_health.truncated is not boolean or null")
        return

    if recordable is None:
        if native_payload is not None:
            raise SchemaRefusal("a no-response Testimonium retains a native payload")
        for field, value in NO_RESPONSE_HEALTH.items():
            if health[field] != value:
                raise SchemaRefusal(
                    f"a no-response Testimonium has inconsistent content_health.{field}"
                )
        if (
            not isinstance(health["truncation_basis"], str)
            or not health["truncation_basis"].strip()
        ):
            raise SchemaRefusal("a no-response Testimonium has no health reason")
        return

    if recordable is False:
        # The narrowest record this stage writes: nothing of what the witness
        # returned could be kept, so there is nothing left to measure and every
        # remaining field is fixed. Leaving them free would let a resealed record
        # take this branch and then assert a character count, a truncation state
        # and a retained payload — self-reported facts wearing the name of the
        # computed ones spec 07 requires these to be.
        if native_payload is not None:
            raise SchemaRefusal(
                "a Testimonium whose native channel was unrecordable retains a native "
                "payload; either nothing could be kept or something could"
            )
        if health["encoding"] != "invalid-or-unrecordable":
            raise SchemaRefusal("an unrecordable Testimonium channel claims a valid encoding")
        for field in ("empty", "blank", "truncated", "characters"):
            if health[field] is not None:
                raise SchemaRefusal(
                    f"an unrecordable Testimonium channel asserts content_health.{field}, "
                    "which nothing was able to measure"
                )
        for field in ("native_type", "truncation_basis"):
            if not isinstance(health[field], str) or not health[field].strip():
                raise SchemaRefusal(f"an unrecordable Testimonium channel has no {field}")
        return
    raise SchemaRefusal("a Testimonium content_health.recordable is not boolean or null")


def format_capabilities_for(row: dict[str, Any]) -> dict[str, Any]:
    """The output format's declared expressiveness, not a confidence score."""
    capabilities = row.get("format_capabilities", DEFAULT_FORMAT_CAPABILITIES)
    if not isinstance(capabilities, dict):
        raise SchemaRefusal("a witness format_capabilities declaration is not an object")
    for field in ("can_express_uncertainty", "can_express_layout"):
        if not isinstance(capabilities.get(field), bool):
            raise SchemaRefusal(f"witness format_capabilities.{field} is not a boolean")
    if problem := _native_problem(capabilities, "format_capabilities"):
        raise SchemaRefusal(problem)
    return capabilities


def prepared_response(
    row: dict[str, Any],
) -> tuple[Any, Any, dict[str, Any] | None, dict[str, Any], str | None]:
    """Return native output and any recording defect without normalizing either.

    The final element is a reason that turns this one attempt into ``failed``.
    The raw object is left untouched for every recordable response, including an
    unexpected-but-parseable JSON shape.
    """
    if "payload" not in row:
        health = no_response_health(reason="fixture response declared no native payload")
        return (
            None,
            None,
            DEFAULT_FORMAT_CAPABILITIES,
            health,
            "the witness response had no native payload",
        )
    native_payload = row["payload"]
    health = content_health(native_payload, completed=True)
    if health["recordable"] is not True:
        return None, None, None, health, str(health["truncation_basis"])
    witness_reported = row.get("witness_reported")
    report_problem = _native_problem(witness_reported, "witness_reported")
    if report_problem is None:
        report_problem = _confidence_problem(witness_reported)
    if report_problem is not None:
        witness_reported = None
    try:
        capabilities = format_capabilities_for(row)
    except SchemaRefusal as error:
        reason = f"the witness format capabilities could not be retained: {error}"
        if report_problem is not None:
            reason = f"{reason}; the witness self-report could not be retained: {report_problem}"
        return native_payload, witness_reported, None, health, reason
    if report_problem is not None:
        return (
            native_payload,
            None,
            capabilities,
            health,
            f"the witness self-report could not be retained: {report_problem}",
        )
    return native_payload, witness_reported, capabilities, health, None


def provenance_for(context, resolved: ChairIdentity | AbsentChair, *, attempted: bool) -> dict:
    """The exact configured identity and actual serving moment for one outcome."""
    if isinstance(resolved, AbsentChair):
        return {
            "chair": resolved.role,
            "chair_state": "absent",
            "absence": resolved.to_record(),
            "resolved_identity": None,
            "resolved_revision": None,
            "receipt_ref": None,
            "adapter_revision": context.adapter_revision,
        }
    if not isinstance(resolved, ChairIdentity):
        raise ContractError("witness resolution returned neither an identity nor an absence")
    receipt_ref = (
        context.write_serving_receipt(resolved, fixture_serving_details(resolved))
        if attempted
        else None
    )
    return {
        "chair": resolved.role,
        "chair_state": "configured",
        "resolved_identity": resolved.to_record(),
        "resolved_revision": {
            "kind": resolved.receipt_revision_kind,
            "value": resolved.receipt_revision,
        },
        "receipt_ref": receipt_ref,
        "adapter_revision": context.adapter_revision,
    }


# Every field a Testimonium payload must carry, whatever the outcome. `reason` and
# the `reported` bridge below are conditional and deliberately outside it.
TESTIMONIUM_FIELDS = frozenset(
    {
        "chair",
        "act_key",
        "attempt_ordinal",
        "regions",
        "provenance",
        "format_capabilities",
        "payload",
        "witness_reported",
        "content_health",
    }
)
# `page_witness` marks a page chair's act-scoped compatibility record and is
# validated here. `scope` and `page_ordinal` are deliberately NOT listed: they
# belong to the page-scoped kind, which this closed act-level payload never
# carries, and allowing them here let a resealed act record wear page clothing.
OPTIONAL_TESTIMONIUM_FIELDS = frozenset({"reason", "reported", "page_witness"})


def testimonium_payload(
    *,
    chair: str,
    act_key: str,
    ordinal: int,
    regions: list[dict[str, str]],
    provenance: dict[str, Any],
    format_capabilities: dict[str, Any] | None,
    native_payload: Any,
    witness_reported: Any,
    health: dict[str, Any],
    outcome: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Build the stage schema without letting a compatibility field define it."""
    record: dict[str, Any] = {
        "chair": chair,
        "act_key": act_key,
        "attempt_ordinal": ordinal,
        "regions": regions,
        "provenance": provenance,
        "format_capabilities": format_capabilities,
        "payload": native_payload,
        "witness_reported": witness_reported,
        "content_health": health,
    }
    if reason is not None:
        record["reason"] = reason

    # Temporary consumer bridge: Perlector's current skeleton input contract only
    # accepts a textual `reported` field. It is a projection of a *textual native
    # payload*, never a self-report, and structured native payloads deliberately
    # receive no coerced text substitute. The bridge can be deleted only by the
    # Perlector owner when its consumer reads `payload` natively.
    if outcome in WITNESS_READING_OUTCOMES and isinstance(native_payload, str):
        record["reported"] = native_payload
    return record


AttemptHistory = dict[tuple[str, str], list[dict[str, Any]]]


class AttemptIndex(NamedTuple):
    """This stage's own prior output, indexed once per invocation."""

    stage_has_artifacts: bool
    by_pair: AttemptHistory
    attachments_by_act: dict[str, list[dict[str, Any]]]


def _attempt_history(context) -> AttemptIndex:
    """Index immutable Testimonia and derived attachments once for this invocation.

    The independent tally deliberately rebuilds and validates the inventory for
    accounting. This index serves only append/collision decisions, whose repeated
    per-pair manifest walks otherwise make a pass quadratic in the folder size —
    which is why the derived act-attachments travel in the same walk rather than
    in a second one per act.
    """
    manifest = context.tree.build_manifest(ATTESTATORES)
    by_pair: AttemptHistory = {}
    attachments_by_act: dict[str, list[dict[str, Any]]] = {}
    for entry in manifest["artifacts"]:
        if entry["kind"] == "act-attachment":
            attachments_by_act.setdefault(entry["subject_id"], []).append(
                context.tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"])
            )
            continue
        if entry["kind"] != "testimonium":
            continue
        record = context.tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        payload = record.get("payload")
        # No `payload["scope"] == "page"` skip here. Page-scoped Testimonia are a
        # kind of their own and the filter above already excludes them, so the
        # skip could only ever fire for an act-scoped record that *claimed* page
        # scope — and it would then drop that record out of the append/collision
        # history on the strength of one self-reported field, which is exactly
        # the disguise F-O5 closed in `attempt_tally`. The identical line was
        # left standing here. Found in fresh-context review (P2).
        chair = payload.get("chair") if isinstance(payload, dict) else None
        if isinstance(chair, str):
            by_pair.setdefault((entry["subject_id"], chair), []).append(record)
    return AttemptIndex(bool(manifest["artifacts"]), by_pair, attachments_by_act)


def require_appendable_ordinal(
    history: AttemptHistory, act_id: str, chair: str, ordinal: int
) -> None:
    """Allow only a rerun of an attempt that exists, or exactly the next one.

    Ordinals are the contiguous run 1..N — `latest_attempt` refuses a gap — so any
    ordinal at or below the current one names an attempt that is already on disk,
    and rewriting it is a resume: the RunTree refuses it outright if the bytes
    differ. Only `current + 1` adds anything.

    The bound is `<= current + 1` rather than `in {current, current + 1}` because a
    targeted reread moves one chair's ordinal without moving any other's. Insisting
    every pair be at the same ordinal would mean the orchestrator — which always
    asks for ordinal 1 — held the whole folder from the moment one chair was
    reread, over writes that would every one have been byte-identical no-ops.
    """
    records = history.get((act_id, chair), [])
    if not records:
        if ordinal != 1:
            raise SchemaRefusal(
                f"Testimonium for {(act_id, chair)!r} has no attempt 1; cannot append ordinal "
                f"{ordinal} across a missing history"
            )
        return
    current = latest_attempt(
        records, f"Testimonium for {(act_id, chair)!r}", operation=f"read:{chair}"
    )
    current_ordinal = current["payload"]["attempt_ordinal"]
    if ordinal > current_ordinal + 1:
        raise SchemaRefusal(
            f"Testimonium for {(act_id, chair)!r} is current at ordinal {current_ordinal}; "
            f"ordinal {ordinal} is neither a rerun of an attempt it holds nor its next "
            "append-only attempt"
        )


def _refuse_write_collision(
    history: AttemptHistory,
    act: dict[str, Any],
    chair: str,
    ordinal: int,
    attempt: "Attempt",
) -> None:
    """Refuse before any write if this pass would seal different bytes than an
    attempt already recorded at this exact (act, chair, ordinal) identity.

    A targeted reread and a whole pass can reach the very same identity with a
    different honest outcome — `resolve_attempt`'s docstring says so plainly: an
    undeclared response is `failed` under a reread and `not-run` under a whole
    pass, because the two write paths mean different things by silence.
    `RunTree.publish_artifact` already refuses a colliding write, but only when
    it is reached, mid-pass, after every earlier pair in this invocation has
    already been published — a half-written attempt layer whose stored manifest
    was never rewritten to describe it. Checking every pair against what already
    exists, before any of them is written, keeps a doomed pass from writing
    anything at all rather than stranding the folder partway through.

    Only a pair whose target ordinal already holds a record can collide;
    `require_appendable_ordinal` already refuses any ordinal beyond that, so
    this only ever compares against a genuine resume attempt. Compared on the
    fields the two write paths can actually disagree on, not the full sealed
    envelope: provenance does not vary with the `reread` flag, only what
    `resolve_attempt` decided did.
    """
    existing = [
        record
        for record in history.get((act["act_id"], chair), [])
        if record["payload"]["attempt_ordinal"] == ordinal
    ]
    if not existing:
        return
    (record,) = existing
    payload = record["payload"]
    if (
        record["outcome"] != attempt.outcome
        or payload.get("payload") != attempt.native_payload
        or payload.get("witness_reported") != attempt.witness_reported
        or payload.get("format_capabilities") != attempt.format_capabilities
        or payload.get("content_health") != attempt.health
        or payload.get("reason") != attempt.reason
    ):
        raise SchemaRefusal(
            f"a whole pass at ordinal {ordinal} would record a different attempt for "
            f"{(act['act_key'], chair)!r} than the one already sealed there: sealed outcome "
            f"{record['outcome']!r}, this pass would write {attempt.outcome!r}. Nothing was "
            "written for this pass"
        )


def pass_would_append(history: AttemptHistory, act_id: str, chairs, ordinal: int) -> bool:
    """Would a whole pass at this ordinal add an attempt to this act, or repeat one?

    The bound in `require_appendable_ordinal` admits both, deliberately — a
    targeted reread moves one chair's ordinal without moving any other's, so the
    orchestrator's ordinal-1 pass has to stay a byte-identical resume over a
    folder one chair has been reread in. The rules that close an act's witness
    layer apply to the append and not to the repeat, so they need this told apart
    rather than assumed.
    """
    for chair in chairs:
        records = history.get((act_id, chair), [])
        if not records:
            return True
        current = latest_attempt(
            records, f"Testimonium for {(act_id, chair)!r}", operation=f"read:{chair}"
        )
        if ordinal > current["payload"]["attempt_ordinal"]:
            return True
    return False


def require_shared_whole_pass_ordinal(
    index: "AttemptIndex", act: dict[str, Any], chairs, ordinal: int
) -> None:
    """Refuse an appending whole pass on an act a targeted reread has moved.

    The whole pass is a run-level instrument: every configured chair, every
    expected act, at one ordinal. It also re-derives each act's act-attachment at
    that same ordinal. A targeted reread moves exactly one chair and re-derives
    the attachment one ordinal above whatever it found, so after a reread the
    whole pass's next attachment ordinal is already taken — by a record describing
    a different state.

    `RunTree.publish_artifact` would refuse that write correctly but *late*: in
    the derived-record loop, after every Testimonium of the pass had been written
    and before `context.finish()` rewrote the inventory to describe them. The
    folder would then hold attempts its own manifest does not name, which the next
    pass can only report as UNKNOWN. Refused here instead, before anything is
    written.

    The condition is the model rather than the mechanism: **a targeted reread
    takes its act off the shared whole-pass ordinal**, and an act is off it
    exactly when its chairs no longer agree on one current ordinal. A pass that
    only *repeats* attempts already sealed is untouched (see `pass_would_append`),
    which is what keeps the orchestrator's ordinal-1 resume working over a folder
    one chair has been reread in, and a partly-lost attempt layer — where some
    pairs have no record at all — is a repair at the ordinal its surviving pairs
    already share, not a mixed act.

    One residual is deliberately left to the RunTree rather than checked here:
    reread *every* chair on one act up to the same ordinal and the act agrees
    again — and a whole pass at that ordinal is then a REPEAT, so
    `pass_would_append` skips both new rules entirely and the pass proceeds to
    the attachment derivation, whose next ordinal the rereads already took.
    Reaching that collision needs each chair's whole-pass attempt to be
    byte-identical to its reread attempt — otherwise `_refuse_write_collision`
    stops the pass first — so the pass that survives had nothing to add. The
    outcome is a loud fatal refusal with `RunTree.write_manifest` as the recorded
    one-step recovery, and a check for it would cost a second derivation of every
    attachment in preflight to close a case whose worst outcome is a noisy stop.
    """
    current: dict[str, int] = {}
    for chair in chairs:
        records = index.by_pair.get((act["act_id"], chair), [])
        if not records:
            continue
        current[chair] = latest_attempt(
            records, f"Testimonium for {(act['act_id'], chair)!r}", operation=f"read:{chair}"
        )["payload"]["attempt_ordinal"]
    if len(set(current.values())) <= 1:
        return
    raise SchemaRefusal(
        f"act {act['act_id']} ({act['act_key']}) carries chairs at different current "
        f"ordinals {dict(sorted(current.items()))}: that act was reread, which takes it off "
        f"the shared whole-pass ordinal. A whole pass at ordinal {ordinal} would re-derive "
        "its act-attachment over the one the reread already sealed. Nothing was written "
        "for this pass"
    )


def preflight_appendable_ordinals(
    context,
    acts: list[dict[str, Any]],
    ordinal: int,
    declarations: dict[str, Any],
    index: "AttemptIndex",
) -> tuple[
    dict[str, tuple[list[dict], str | None]],
    dict[tuple[str, str], "Attempt"],
]:
    """Refuse a damaged history, or a colliding write, before adding any new
    attempt to this invocation.

    The ordinal bound alone is not enough: it lets a whole pass through at an
    ordinal a targeted reread has already sealed a *different* record at for one
    chair, and the collision then surfaces reactively, mid-pass, at whichever
    pair the loop reaches it on — see `_refuse_write_collision`. The caller
    supplies one declaration set for both this preflight and `attempt_pass`,
    since the two must agree on what this ordinal means. The returned region map
    lets publication use the exact regions preflight already verified instead of
    walking and hashing Designator again.
    """
    regions_by_act: dict[str, tuple[list[dict], str | None]] = {}
    attempts_by_pair: dict[tuple[str, str], Attempt] = {}
    appending = [
        act
        for act in acts
        if pass_would_append(index.by_pair, act["act_id"], context.witness_chairs, ordinal)
    ]
    closed = witness_bound_reading_acts(context) if appending else frozenset()
    for act in appending:
        # The whole pass is the other write path that can add testimony to an act,
        # so it meets the same closed-layer rule the targeted reread does. Only an
        # append: a whole pass that rewrites attempts already sealed at this
        # ordinal is the orchestrator's ordinary resume, moves no chair's current
        # record, and is untouched by this.
        require_open_witness_layer(closed, act, f"a whole pass at ordinal {ordinal}")
    for act in acts:
        regions: list[dict] = []
        if act["outcome"] == "held":
            not_read: str | None = (
                "the Designator held this act; its incomplete proposal was not shown "
                "to any configured witness"
            )
        else:
            try:
                regions = proposed_regions(context, act["act_id"])
                not_read = None
            except ContractError as error:
                if isinstance(error, FatalAccounting):
                    raise
                not_read = f"the proposed region was refused before this chair ran: {error}"
        regions_by_act[act["act_id"]] = (regions, not_read)
        for chair in context.witness_chairs:
            require_appendable_ordinal(index.by_pair, act["act_id"], chair, ordinal)
            resolved = context.registry.resolve(chair)
            attempt = (
                not_read_attempt(resolved, not_read)
                if not_read is not None
                else resolve_attempt(
                    context,
                    act,
                    chair,
                    resolved,
                    declarations,
                )
            )
            attempts_by_pair[(act["act_id"], chair)] = attempt
            _refuse_write_collision(index.by_pair, act, chair, ordinal, attempt)
    # Last, so a genuine witness-attempt disagreement is named for what it is
    # rather than reported as its consequence one derivation downstream: the
    # attachment collides *because* a reread already sealed an attempt this pass
    # would contradict, and where that contradiction exists `_refuse_write_collision`
    # says which chair and which two outcomes.
    for act in appending:
        require_shared_whole_pass_ordinal(index, act, context.witness_chairs, ordinal)
    return regions_by_act, attempts_by_pair


def validate_tallied_testimonium(
    context,
    record: dict[str, Any],
    act: dict[str, Any],
    regions_by_act: dict[str, list[dict]],
) -> None:
    """Refuse a resealed Testimonium that this stage could not have produced.

    The generic envelope proves a record is syntactically sealed; the attempt
    tally also has to prove its stage-specific channel remains interpretable
    before authorizing another immutable append. This deliberately validates no
    witness's *content* and makes no quality decision. `regions_by_act` retains
    that independent verification once per act while the tally checks each chair.
    """
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise SchemaRefusal("a Testimonium tally record has no object payload")
    if missing := sorted(TESTIMONIUM_FIELDS - set(payload)):
        raise SchemaRefusal(f"a Testimonium tally record lacks required field(s) {missing}")
    allowed = TESTIMONIUM_FIELDS | OPTIONAL_TESTIMONIUM_FIELDS
    if unexpected := sorted(set(payload) - allowed):
        raise SchemaRefusal(
            f"a Testimonium tally record carries unknown field(s) {unexpected}; this stage "
            "writes a closed payload, and a field nothing validates is a field nothing "
            "downstream can trust"
        )
    chair = payload["chair"]
    if not isinstance(chair, str) or chair not in context.witness_chairs:
        raise SchemaRefusal("a Testimonium tally record names no configured chair")
    if payload["act_key"] != act["act_key"]:
        raise SchemaRefusal("a Testimonium tally record disagrees with its act key")
    ordinal = payload["attempt_ordinal"]
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
        raise SchemaRefusal("a Testimonium tally record has no positive attempt ordinal")
    if payload["format_capabilities"] is None:
        if record["outcome"] != "failed":
            raise SchemaRefusal("a non-failed Testimonium carries no format_capabilities record")
    else:
        format_capabilities_for({"format_capabilities": payload["format_capabilities"]})
    if problem := _native_problem(payload["witness_reported"], "witness_reported"):
        raise SchemaRefusal(problem)
    validate_content_health(payload["payload"], payload["content_health"])
    if record["outcome"] in WITNESS_READING_OUTCOMES and isinstance(payload["payload"], str):
        if payload.get("reported") != payload["payload"]:
            raise SchemaRefusal(
                "a textual Testimonium's compatibility projection differs from its "
                "verbatim native payload"
            )
    elif "reported" in payload:
        raise SchemaRefusal(
            "a non-textual or non-reading Testimonium carries a compatibility projection"
        )
    if record["outcome"] in {"failed", "dead", "not-run"}:
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise SchemaRefusal(
                f"a {record['outcome']} Testimonium records no reason for its non-reading outcome"
            )
    attempted = record["outcome"] in ATTEMPTED_WITNESS_OUTCOMES
    validate_serving_provenance(
        context,
        payload["provenance"],
        producer_stage=ATTESTATORES,
        require_receipt=attempted,
    )
    if attempted:
        if act["outcome"] != "proposed":
            raise SchemaRefusal("a Testimonium attempted a Designator-held act")
        regions = regions_by_act.get(act["act_id"])
        if regions is None:
            regions = proposed_regions(context, act["act_id"])
            regions_by_act[act["act_id"]] = regions
        if payload["regions"] != region_references(regions) or record["inputs"] != region_inputs(
            context, regions
        ):
            raise SchemaRefusal(
                "a Testimonium tally record does not bind exactly the proposal regions and inputs"
            )
    elif payload["regions"] != [] or record["inputs"] != []:
        raise SchemaRefusal("a non-attempted Testimonium tally record carries regions or inputs")
    if record["outcome"] == "dead" and payload["provenance"].get("chair_state") != "absent":
        raise SchemaRefusal("a dead Testimonium tally record does not retain an absent chair")
    if record["outcome"] == "not-run" and payload["provenance"].get("chair_state") != "configured":
        raise SchemaRefusal("a not-run Testimonium tally record does not retain a configured chair")


def require_accounted_unrecordable_channel(record: dict[str, Any], payload: dict[str, Any]) -> None:
    """Tell a witness whose output could not be kept from an evidence channel nobody can read.

    Two of spec 07's requirements look contradictory here and are not, because
    they are about different channels. Its isolation bullet — "one bad crop, one
    dead witness, one malformed response never kills the folder ... recorded as a
    failed attempt and refused, not repaired silently" — is about one witness's
    own output. Invariant #23's — "a damaged or unrecordable evidence channel
    makes the count UNKNOWN, and UNKNOWN holds the folder" — is about the attempt
    tally, the independent count of what was attempted.

    So an unrecordable response is accounted inside an honestly `failed` attempt
    that says why: countable, counted, visibly failed, its act under-witnessed and
    its run partial. Holding the whole folder instead would stop the Perlector
    reading ink nobody doubts because one witness of three failed. Only a record
    claiming to be a *reading* while saying nothing could retain what it read is
    incoherent, and that one is #23's UNKNOWN.
    """
    if record["outcome"] in WITNESS_READING_OUTCOMES:
        raise SchemaRefusal(
            f"a Testimonium claims outcome {record['outcome']!r} while recording that its own "
            "native channel was unrecordable; a reading nothing could retain is not a reading, "
            "and its tally cannot be counted as known"
        )
    if record["outcome"] != "failed":
        raise SchemaRefusal(
            f"a Testimonium with outcome {record['outcome']!r} records an unrecordable native "
            "channel; only an attempted-and-failed reading has a channel to be unrecordable"
        )
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise SchemaRefusal(
            "a failed Testimonium with an unrecordable native channel records no reason; an "
            "absence with no reason is the silent loss this stage exists to refuse"
        )


def attempt_tally(
    tree,
    *,
    context=None,
    acts: list[dict[str, Any]] | None = None,
    chairs: list[str] | None = None,
) -> dict[str, Any]:
    """Rebuild and check the stage's attempt inventory.

    The stored manifest is derived state, not the evidence: it is checked against
    a fresh tree walk and the immutable Testimonia. An unreadable, malformed,
    missing or divergent inventory makes the count UNKNOWN, and a caller must hold
    rather than turn that uncertainty into a favourable count.

    A witness whose own output could not be retained is a different fact and does
    not make the count unknown — see `require_accounted_unrecordable_channel`.

    `chairs` supplies the act/chair denominator and is optional independently of
    `acts`, because *whether every pair is accounted for* is a closing check and
    not a precondition. Demanding it before a pass deadlocks the one thing that
    could satisfy it: a pass interrupted before its manifest was written leaves a
    partial inventory, and the pass that would complete it was refused on the
    grounds that it was incomplete. Every record on disk is still validated
    either way; only the denominator moves.
    """
    if chairs is not None and acts is None:
        raise SchemaRefusal("an attempt tally denominator names chairs but no expected acts")
    try:
        stored_path = tree.resolve(tree.manifest_path(ATTESTATORES))
        stored = json.loads(stored_path.read_bytes().decode("utf-8"))
        rebuilt = tree.build_manifest(ATTESTATORES)
    except FatalAccounting:
        raise
    except (ContractError, OSError, UnicodeDecodeError, ValueError, RecursionError) as error:
        # RecursionError beside the others for the same reason `_read_json` in
        # common/runtree/store.py added it: json's scanner recurses per nesting
        # level, so a stored manifest an attacker or a damaged write replaced with
        # deeply nested JSON raises it here directly, on this stage's own read,
        # rather than through the shared reader. Uncaught, that is a traceback
        # where #23 promises UNKNOWN + hold.
        return {"state": "UNKNOWN", "count": None, "hold": True, "reason": str(error)}
    if stored != rebuilt:
        return {
            "state": "UNKNOWN",
            "count": None,
            "hold": True,
            "reason": "the stored Attestatores manifest does not equal its rebuilt inventory",
        }

    # Page-scoped Testimonia are independently retained source evidence under
    # their own kind, so this kind filter keeps the act-level walk to act
    # attempts alone. There was also a `payload["scope"] == "page"` skip inside
    # the loop below, which this filter made unreachable -- and which, had
    # anything reached it, would have carried an act-scoped record past every
    # check in this function on the strength of one self-reported field. Found
    # in audit; F-O5.
    testimonia = [entry for entry in rebuilt["artifacts"] if entry["kind"] == "testimonium"]
    by_act = {act["act_id"]: act for act in acts or ()}
    try:
        by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
        regions_by_act: dict[str, list[dict]] = {}
        for entry in testimonia:
            record = tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
            payload = record.get("payload")
            if not isinstance(payload, dict):
                raise SchemaRefusal("a Testimonium carries no object payload")
            if missing := sorted(TESTIMONIUM_FIELDS - set(payload)):
                raise SchemaRefusal(f"a Testimonium carries no required field(s) {missing}")
            allowed = TESTIMONIUM_FIELDS | OPTIONAL_TESTIMONIUM_FIELDS
            if unexpected := sorted(set(payload) - allowed):
                raise SchemaRefusal(
                    f"a Testimonium carries unknown field(s) {unexpected}; this stage writes a "
                    "closed payload, and a field nothing validates is a field nothing downstream "
                    "can trust"
                )
            chair = payload.get("chair")
            if not isinstance(chair, str) or not chair:
                raise SchemaRefusal("a Testimonium carries no named chair")
            by_pair.setdefault((record["subject_id"], chair), []).append(record)
            health = record.get("payload", {}).get("content_health")
            validate_content_health(payload["payload"], health)
            if health["recordable"] is False:
                require_accounted_unrecordable_channel(record, payload)
            if context is not None:
                if acts is None:
                    raise SchemaRefusal(
                        "a contextual attempt tally has no expected-act denominator"
                    )
                act = by_act.get(record["subject_id"])
                if act is None:
                    raise SchemaRefusal("a Testimonium tally record names no expected act")
                validate_tallied_testimonium(context, record, act, regions_by_act)
        if chairs is not None:
            expected_pairs = {(act["act_id"], chair) for act in acts for chair in chairs}
            if set(by_pair) != expected_pairs:
                raise SchemaRefusal(
                    "the rebuilt Testimonium inventory does not account for every expected "
                    "act/chair pair"
                )
        for (act_id, chair), records in by_pair.items():
            latest_attempt(
                records,
                f"Testimonium tally for {(act_id, chair)!r}",
                operation=f"read:{chair}",
            )
    except ContractError as error:
        # `FatalAccounting` is a `ContractError`, and `latest_attempt` raises it in five
        # places directly inside this block — so without this the broadest handler here
        # turned invariant #10 into a hold. The two are not interchangeable: a hold says
        # *the count is unknown*, while an accounting imbalance says *the partition
        # itself is broken*, and the error class exists to keep those apart. Its own
        # docstring is the rule — "nothing may catch this and carry on". Two other sites
        # in this file already re-raise it the same way; this one was missed. Found by
        # CodeRabbit reviewing the rebased branch.
        if isinstance(error, FatalAccounting):
            raise
        return {"state": "UNKNOWN", "count": None, "hold": True, "reason": str(error)}
    except OSError as error:
        return {"state": "UNKNOWN", "count": None, "hold": True, "reason": str(error)}
    return {
        "state": "KNOWN",
        "count": sum(len(records) for records in by_pair.values()),
        "hold": False,
        "reason": None,
    }


def _positive_ordinal(value: str) -> int:
    try:
        ordinal = int(value)
    except ValueError as error:
        raise ValueError("attempt ordinal must be an integer") from error
    if ordinal < 1:
        raise ValueError("attempt ordinal must be positive")
    return ordinal


class Attempt(NamedTuple):
    """One chair's resolved outcome for one act on one attempt.

    One shape shared by every constructor — `dead_attempt`, `not_read_attempt`,
    and `resolve_attempt` — so the whole-pass write path and the targeted reread
    cannot drift on what a witness attempt is. It describes one chair and reads
    no other chair's record: nothing here compares, ranks, or chooses among
    witnesses, and there is no argument through which it could.
    """

    outcome: str
    native_payload: Any
    witness_reported: Any
    format_capabilities: dict[str, Any] | None
    health: dict[str, Any]
    reason: str | None


def dead_attempt(resolved: AbsentChair) -> Attempt:
    """A chair the roster declares absent: unavailable before any attempt reached it."""
    return Attempt(
        outcome="dead",
        native_payload=None,
        witness_reported=None,
        format_capabilities=DEFAULT_FORMAT_CAPABILITIES,
        health=no_response_health(reason="not-attempted"),
        reason=f"chair is explicitly absent: {resolved.reason}",
    )


def not_read_attempt(resolved: ChairIdentity | AbsentChair, reason: str) -> Attempt:
    """One chair on an act no witness was shown: unavailable, or not asked.

    An absent chair stays `dead` whatever kept the act from being read, because
    the two facts are independent — holding the act does not turn an unreachable
    witness into a merely unasked one.
    """
    if isinstance(resolved, AbsentChair):
        return dead_attempt(resolved)
    return Attempt(
        outcome="not-run",
        native_payload=None,
        witness_reported=None,
        format_capabilities=DEFAULT_FORMAT_CAPABILITIES,
        health=no_response_health(reason="not-attempted"),
        reason=reason,
    )


def declarations_for(context, ordinal: int) -> dict[str, Any]:
    """Every fixture declaration that applies to this exact attempt ordinal.

    Read once per pass rather than once per chair, and bound to the ordinal, so a
    declared first-attempt failure cannot silently describe a later reread.

    `empty` is a declared empty *response* — the fixture's stand-in for a
    provider that returned an empty body — and `declared_response` sends it back
    through the same retention boundary as any declared text, so the outcome is
    derived from what was retained rather than named here. `not_run` is a
    configured chair deliberately never asked for this attempt.
    """
    declarations = {
        "ordinal": ordinal,
        "failures": _declared_pairs(context, ordinal, "witness_failure"),
        "empty": _declared_pairs(context, ordinal, "witness_empty"),
        "not_run": _declared_pairs(context, ordinal, "witness_not_run"),
        "malformed": declared_malformed(context, ordinal),
    }
    outcome_sets = {
        name: set(value) if isinstance(value, dict) else value
        for name, value in declarations.items()
        if name != "ordinal"
    }
    names = sorted(outcome_sets)
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            if overlap := sorted(outcome_sets[left] & outcome_sets[right]):
                raise SchemaRefusal(
                    f"fixture declares conflicting witness outcomes {left!r} and {right!r} "
                    f"for {overlap!r} at attempt ordinal {ordinal}"
                )
    return declarations


def resolve_attempt(
    context,
    act: dict[str, Any],
    chair: str,
    resolved: ChairIdentity | AbsentChair,
    declarations: dict[str, Any],
    *,
    reread: bool = False,
) -> Attempt:
    """What one configured chair's attempt at one act came to.

    Every branch ends in exactly one member of the closed six-outcome vocabulary,
    because a chair that simply does not appear is the silent skip this stage
    exists to refuse.

    `reread` decides which member an undeclared response lands on, and the two
    write paths genuinely differ there. A whole pass asks the fixture what each
    chair returned at this ordinal, and silence means the chair was not asked:
    `not-run`. A targeted reread names one chair on one act, so the invocation
    *is* the attempt and silence is an attempt that produced no usable
    Testimonium — which spec 07 gives to `failed`, "as against ... `not-run`
    (configured, never attempted)".
    """
    if isinstance(resolved, AbsentChair):
        return dead_attempt(resolved)

    key = (act["act_key"], chair)
    native_payload: Any = None
    witness_reported: Any = None
    capabilities = DEFAULT_FORMAT_CAPABILITIES
    health = no_response_health(reason="not-attempted")
    reason: str | None = None

    if key in declarations["not_run"]:
        outcome = "not-run"
        reason = "fixture declares that this configured chair was never attempted"
    elif key in declarations["failures"]:
        outcome = "failed"
        health = no_response_health(reason="attempted-but-no-usable-response")
        reason = "the chair returned no usable response"
    elif key in declarations["malformed"]:
        outcome = "failed"
        health = {
            "native_type": "unrecordable",
            "encoding": "invalid-or-unrecordable",
            "recordable": False,
            "empty": None,
            "blank": None,
            "truncated": None,
            "characters": None,
            "truncation_basis": declarations["malformed"][key],
        }
        reason = (
            f"the provider response was refused without repair: {declarations['malformed'][key]}"
        )
    else:
        response = declared_response(context, act["act_key"], chair, declarations)
        if response is None and reread:
            outcome = "failed"
            health = no_response_health(reason="attempted-but-no-usable-response")
            reason = "the reread reached this chair and it returned no response"
        elif response is None:
            outcome = "not-run"
            reason = "no attempt was made for this configured chair"
        else:
            (
                native_payload,
                witness_reported,
                capabilities,
                health,
                recording_problem,
            ) = prepared_response(response)
            if recording_problem is not None:
                outcome = "failed"
                reason = f"the provider response was refused without repair: {recording_problem}"
            elif isinstance(native_payload, str) and native_payload == "":
                # Derived from the response this chair actually returned, never
                # asserted about it. `genuinely-empty` is the one completed
                # outcome whose whole content is an absence, so it is the one
                # most easily minted from something other than evidence -- and
                # it used to be: a Designator page-fallback act took this
                # outcome for every configured chair from its own derived
                # identity, before any response boundary was consulted at all
                # (Sol-S1). Nothing reaches here without a retained, recordable
                # response to this exact request; a missing one is the
                # `not-run`/`failed` above and holds the act.
                outcome = "genuinely-empty"
            else:
                outcome = "read"

    return Attempt(outcome, native_payload, witness_reported, capabilities, health, reason)


def declared_page_witness_chairs(context) -> set[str]:
    """The fixture's page-witness declaration, validated before any use.

    One accessor for both write paths (the act-scoped compatibility flag in
    `publish_attempt` and the page join in
    `publish_page_testimonia_and_attachments`), so a malformed declaration is a
    named refusal before the first attempt publishes rather than a TypeError
    mid-pass after some artifacts already sealed (CodeRabbit chain-end review;
    host disposition: fixed).
    """
    declared = context.fixture.get("page_witness_chairs", [])
    if (
        not isinstance(declared, list)
        or len(declared) != len(set(declared))
        or any(not isinstance(chair, str) for chair in declared)
    ):
        raise SchemaRefusal("fixture page_witness_chairs is not a unique string list")
    return set(declared)


def publish_attempt(
    context,
    *,
    act: dict[str, Any],
    chair: str,
    resolved: ChairIdentity | AbsentChair,
    ordinal: int,
    regions: list[dict],
    attempt: Attempt,
) -> None:
    """Seal one immutable Testimonium. The only write path for an attempt."""
    attempted = attempt.outcome in ATTEMPTED_WITNESS_OUTCOMES
    payload = testimonium_payload(
        chair=chair,
        act_key=act["act_key"],
        ordinal=ordinal,
        regions=region_references(regions) if attempted else [],
        provenance=provenance_for(context, resolved, attempted=attempted),
        format_capabilities=attempt.format_capabilities,
        native_payload=attempt.native_payload,
        witness_reported=attempt.witness_reported,
        health=attempt.health,
        outcome=attempt.outcome,
        reason=attempt.reason,
    )
    if chair in declared_page_witness_chairs(context):
        # This is the fixture's interim act view of an immutable page witness.
        # Its attachment points at the retained page Testimonium; R4 replaces
        # this declared view with alignment, not with another witness kind.
        payload["page_witness"] = True
    context.publish(
        kind="testimonium",
        subject_id=act["act_id"],
        outcome=attempt.outcome,
        attempt=attempt_id(act["act_id"], f"read:{chair}", ordinal),
        inputs=region_inputs(context, regions) if attempted else [],
        payload=payload,
    )


def _raw_span_from_normalized(
    offset_map: list[int | None], start: int, end: int
) -> tuple[int, int] | None:
    """Translate a `[start, end)` span over `markup_text_view`'s normalized text
    back into the raw text's own character indices.

    `align_to_anchor`'s matching runs on the normalized (whitespace-collapsed)
    text, so a matched block's `start`/`end` are normalized-text offsets. Storing
    them as-is under `witness_span` -- which every later reader (this stage's own
    `span` field, and the Recensor's page-Testimonium content-coverage check)
    indexes into the RAW page text -- silently shifts by however much leading or
    internal whitespace the normalization collapsed. `offset_map[i]` is `None`
    only for a synthesized separator character with no raw counterpart, so the
    real span is the min/max raw index actually mapped inside the range.
    """
    raw_indices = [
        offset_map[index] for index in range(start, end) if offset_map[index] is not None
    ]
    if not raw_indices:
        return None
    return min(raw_indices), max(raw_indices) + 1


class PageJoin(NamedTuple):
    """R0's synthetic page reading for one chair: the text, what it amounts to,
    and every act attempt the join could not carry."""

    native_payload: str
    outcome: str
    unjoined_act_attempts: list[dict[str, Any]]
    # How many attempts the join DID carry. Carried rather than derived, because
    # `page_failure_reason` cannot tell "some acts joined empty" from "nothing
    # joined at all" out of the unjoined list alone, and the difference is the
    # difference between a page read as blank and a page not read.
    joined_act_attempts: int


def page_failure_reason(unjoined_act_attempts: list[dict[str, Any]], joined: int) -> str:
    """Why a page record failed, derived from the unjoined attempts' own outcomes.

    **Never from their count.** Two different things land in
    `unjoined_act_attempts`: an attempt that was not a reading at all, and a
    reading this chair genuinely delivered as a structured native object that the
    synthetic text join cannot concatenate (`page_join` above spells the second
    out in each row's own `reason`). Counting them together called both "unread",
    which is false of the second and sends an operator hunting a provider failure
    that never happened — the same defect as the "page witness had no recordable
    response" wording this stage already replaced, one case over.

    So the page-level reason reads the partition the rows already carry rather
    than re-deriving a worse one from a length comparison.

    **`joined` is needed and is not derivable from the list.** The first version of
    this function reported "the page join carried only empty readings" for a page
    where *nothing* joined — every attempt a failure, no reading of any kind — which
    is the same misdescription one case further along, introduced by the commit that
    fixed the previous one. An unjoined list of non-readings is identical in both
    cases; only the joined count separates a page read as blank from a page not read
    (CodeRabbit CLI, PR #63).
    """

    unread = [
        row for row in unjoined_act_attempts if row["outcome"] not in WITNESS_READING_OUTCOMES
    ]
    unjoinable = len(unjoined_act_attempts) - len(unread)
    if not unjoined_act_attempts:
        return "the page join carried no textual reading"
    if not unread:
        return (
            "every act this chair reported was a structured native reading the page join "
            "could not concatenate; the page was read and no part of it is claimed unread"
        )
    if unjoinable:
        return (
            f"the page join could not carry {len(unjoined_act_attempts)} act attempts: "
            f"{len(unread)} were not readings, and {unjoinable} were structured native "
            "readings the join cannot concatenate; a completed absence is not claimed "
            "while either kind is outstanding"
        )
    if not joined:
        return (
            f"no act attempt on this page was a reading at all: {len(unread)} attempts, "
            "none of them carrying a reading this join could take; the page is unread "
            "rather than read and empty"
        )
    return (
        "the page join carried only empty readings and could not carry every act attempt; "
        "a completed absence is not claimed over a page partly unread"
    )


def page_join(pairs: list[tuple[dict[str, Any], Attempt]]) -> PageJoin:
    """Concatenate one chair's delivered act readings into its page reading.

    Only a genuine reading contributes text. An attempt whose *outcome* is
    `failed` can still carry a parsed `native_payload` string (a bad
    `witness_reported`/`format_capabilities` fails the whole attempt without
    clearing the text `prepared_response` already parsed) -- filtering on
    `isinstance(..., str)` alone let that failed act's own text be silently
    folded into a page witness's "read" testimony, laundering a recorded failure
    into apparent coverage (D2/D3; GOVERNANCE 2). Found in audit; F-S1.

    The disclosure is the exact complement of that filter, computed from one
    partition rather than from a second predicate. They were two predicates, and
    they did not agree: the join also dropped an act whose reading is a
    structured native object (a dict or list rather than text), while the closed
    `unjoined_act_attempts` list named only non-reading OUTCOMES. In the shipped
    `structured-witness` scenario that made attestator_1's page-1 record report
    `read`, carry act a2's text alone, and disclose nothing -- act a1 gone behind
    a successful status, which is F-P3's own defect through F-S1's own door.
    Found in audit; F-O7.

    **A separator is not a reading.** The join used to be `"\n".join(readable)`
    over every joined payload including the empty ones, and the outcome was
    `read` whenever `readable` was non-empty -- so a page whose every act this
    chair genuinely read as empty produced `payload="\n"` and a `read` page
    Testimonium: characters no act delivered, under an outcome claiming a
    reading of them (CodeRabbit W44). Separators are therefore placed only
    *between* delivered characters, and the outcome is derived from the joined
    text rather than from the length of the list that produced it:

    - `failed`: no act attempt joined at all. Nothing on this page was read by
      this chair, so there is no page reading and no receipt to carry.
    - `genuinely-empty`: acts joined and every one of them delivered an empty
      body. The chair read the page's acts and reported nothing on each, which
      is the same fact at page scope that the act-scoped outcome records, and
      `payload=""` is what `genuinely-empty` means everywhere in this stage.
    - `read`: the joined text carries at least one delivered character.

    A joined-but-empty act is not listed in `unjoined_act_attempts`: it was
    carried, faithfully, and its zero characters are in the text. What it read
    stays visible in its own act-scoped Testimonium and in the act attachment.
    """
    joined: list[tuple[dict[str, Any], Attempt]] = []
    unjoined: list[tuple[dict[str, Any], Attempt]] = []
    for act, attempt in pairs:
        target = (
            joined
            if attempt.outcome in WITNESS_READING_OUTCOMES
            and isinstance(attempt.native_payload, str)
            else unjoined
        )
        target.append((act, attempt))
    native_payload = "\n".join(
        attempt.native_payload for _, attempt in joined if attempt.native_payload
    )
    if not joined:
        outcome = "failed"
    elif native_payload == "":
        # A completed absence is claimed only over a page this chair's join
        # fully carried: `genuinely-empty` says "read the page's acts and
        # reported nothing on each", and an act the join could not carry is an
        # act this record did not read — a proved absence over unread ground
        # would be the fabrication defect one scope up (invariant 6). `read`
        # beside unjoined rows stays honest because delivered characters plus
        # a disclosure claim less, not more. Nothing seals on a page record's
        # outcome today; the act-scoped Testimonia carry the read-empty facts
        # either way.
        outcome = "genuinely-empty" if not unjoined else "failed"
    else:
        outcome = "read"
    return PageJoin(
        native_payload=native_payload,
        outcome=outcome,
        joined_act_attempts=len(joined),
        unjoined_act_attempts=[
            {
                "act_id": act["act_id"],
                "act_key": act["act_key"],
                "outcome": attempt.outcome,
                # A non-reading attempt always carries its own reason. A reading
                # the join could not carry has none to borrow, and an omission
                # with no reason is the silent loss this list exists to refuse --
                # so the join states its own limit instead.
                "reason": attempt.reason
                if attempt.outcome not in WITNESS_READING_OUTCOMES
                else (
                    "this chair delivered a structured native reading for the act; R0's "
                    "synthetic page join concatenates delivered text only"
                ),
            }
            for act, attempt in unjoined
        ],
    )


def act_scoped_attachment_entry(
    context,
    act: dict[str, Any],
    chair: str,
    attempt: "Attempt",
    ordinal: int,
) -> dict[str, Any]:
    """One act-scoped chair's derived attachment view of one attempt.

    Shared by the whole pass and the targeted reread, because both derive the
    same view of the same per-(act, chair) attempt stream and a second spelling
    is how the two would come to disagree about what the derived record says —
    the drift F-O1 was, one layer down.

    An act-scoped chair reads the act crop directly, so there is no page reading
    to place it inside and `alignment` is deliberately absent. The span is this
    chair's own complete delivered reading, the interim measure that stands until
    R4's alignment computes a true covered span (F-S2: it is derived from the
    response, never fixture-declared).
    """
    attached = act["outcome"] == "proposed" and attempt.outcome in WITNESS_READING_OUTCOMES
    act_attempt = attempt_id(act["act_id"], f"read:{chair}", ordinal)
    return {
        "chair": chair,
        "page_witness": False,
        "testimonium_ref": context.artifact_ref(
            ATTESTATORES,
            "testimonium",
            artifact_id(ATTESTATORES, "testimonium", act["act_id"], act_attempt),
        ),
        "attached": attached,
        "content_health": attempt.health,
        "alignment": None,
        "span": (
            {
                "start": 0,
                "end": len(attempt.native_payload)
                if isinstance(attempt.native_payload, str)
                else 0,
            }
            if attached
            else None
        ),
    }


def publish_page_testimonia_and_attachments(
    context,
    *,
    acts: list[dict[str, Any]],
    ordinal: int,
    attempts_by_pair: dict[tuple[str, str], Attempt],
) -> None:
    """Retain page testimony and derive one attachment record for every act.

    R0 uses each successful chair's complete delivered act reading as an interim
    span so the custody chain is real before R4 owns text alignment. The fixture
    declares no spans. The act-scoped records for chairs 1 and 3 remain a temporary
    compatibility view for the current Perlector; each is explicitly linked below
    to the immutable page Testimonium that supplied it.
    """
    page_chairs = declared_page_witness_chairs(context) & set(context.witness_chairs)
    limits, limits_digest = load_alignment_limits(context.args.alignment_config)
    context.require_sealed_config("alignment", limits_digest)
    page_records: dict[tuple[int, str], dict[str, str]] = {}
    page_texts: dict[tuple[int, str], str] = {}
    # The anchor is a page fact, not a chair's report, and it is kept in its own
    # map for that reason: parked in `page_texts` under a reserved chair slot it
    # shared a key space with the configured roster, so a chair carrying that
    # name would have had its retained page reading silently overwritten by the
    # anchor markup and then been aligned against itself.
    anchor_texts: dict[int, str] = {}
    page_alignments: dict[tuple[int, str], dict[str, Any]] = {}
    anchor_ranges: dict[tuple[int, str], dict[str, int]] = {}
    by_page: dict[int, list[dict[str, Any]]] = {}
    for act in acts:
        if act["outcome"] == "proposed":
            by_page.setdefault(act["page_ordinal"], []).append(act)

    for page_ordinal, page_acts in sorted(by_page.items()):
        page_subject = page_identity(context.fixture, page_ordinal)
        for chair in sorted(page_chairs):
            join = page_join([(act, attempts_by_pair[(act["act_id"], chair)]) for act in page_acts])
            native_payload, outcome = join.native_payload, join.outcome
            unjoined_act_attempts = join.unjoined_act_attempts
            reading = outcome in WITNESS_READING_OUTCOMES
            failure_reason = page_failure_reason(unjoined_act_attempts, join.joined_act_attempts)
            page_texts[(page_ordinal, chair)] = native_payload
            health = content_health(native_payload, completed=reading)
            resolved = context.registry.resolve(chair)
            page_attempt = attempt_id(page_subject, f"read:{chair}", ordinal)
            context.publish(
                kind="page-testimonium",
                subject_id=page_subject,
                outcome=outcome,
                attempt=page_attempt,
                payload={
                    **testimonium_payload(
                        chair=chair,
                        act_key=f"page-{page_ordinal}",
                        ordinal=ordinal,
                        regions=[],
                        provenance=provenance_for(context, resolved, attempted=reading),
                        format_capabilities=DEFAULT_FORMAT_CAPABILITIES,
                        native_payload=native_payload if reading else None,
                        witness_reported=None,
                        health=health if reading else no_response_health(reason=failure_reason),
                        outcome=outcome,
                        reason=None if reading else failure_reason,
                    ),
                    "scope": "page",
                    "page_ordinal": page_ordinal,
                    # R0 synthesizes this fixture page record by joining the
                    # chair's successful act attempts. The joined text's own
                    # content_health cannot reveal which acts were omitted, so
                    # retain every unjoined attempt explicitly (GOVERNANCE 2).
                    "unjoined_act_attempts": unjoined_act_attempts,
                },
            )
            page_records[(page_ordinal, chair)] = context.artifact_ref(
                ATTESTATORES,
                "page-testimonium",
                artifact_id(ATTESTATORES, "page-testimonium", page_subject, page_attempt),
            )
        anchors = [
            row
            for row in context.fixture.get("chandra_anchor", [])
            if row.get("page_ordinal") == page_ordinal
        ]
        if len(anchors) > 1:
            # Skipping a malformed declaration is not the same fact as an absent
            # one: it would detach every page witness on the page from every act
            # on it, and record `missing-chandra-page-anchor` for an anchor that
            # is present on disk -- a default substituted for malformed evidence
            # (GOVERNANCE 2/10).
            raise SchemaRefusal(
                f"page {page_ordinal} declares {len(anchors)} Chandra anchors; a page has "
                "one anchor, and skipping a duplicated declaration would detach every "
                "page witness on it under a reason naming an absent anchor"
            )
        if anchors and not isinstance(anchors[0].get("html"), str):
            raise SchemaRefusal(
                f"the Chandra anchor for page {page_ordinal} carries no anchor markup "
                "text; a malformed anchor is not an absent one"
            )
        if anchors:
            anchor = anchors[0]
            anchor_texts[page_ordinal] = anchor["html"]
            normalized_anchor = markup_text_view(anchor["html"])["text"]
            # `lines` is declared in reading order (ARCHITECTURE: Chandra's own
            # `ocr_layout` reading flow). Searching each line from where the
            # previous one ended, rather than from the start of the page every
            # time, means a phrase repeated across two acts on the same page
            # (a formulaic register opening, most plainly) resolves to its own
            # occurrence in order instead of both lines collapsing onto the
            # first match `str.find` would return from position 0.
            search_from = 0
            for line in anchor.get("lines", []):
                # The same malformed-versus-absent rule as the anchor checks
                # above: a skipped row leaves the act reporting
                # act-anchor-line-not-located for a line that sits malformed on
                # disk, and leaves `search_from` behind the malformed line's
                # span so the next act's formulaic opening can resolve into it.
                if not isinstance(line, dict) or not isinstance(line.get("act_key"), str):
                    raise SchemaRefusal(
                        f"a Chandra anchor line for page {page_ordinal} names no act key; "
                        "skipping it would detach an act under a reason naming an absent line"
                    )
                source = line.get("text")
                if not isinstance(source, str):
                    raise SchemaRefusal(
                        f"the Chandra anchor line for act {line['act_key']} on page "
                        f"{page_ordinal} carries no text; a malformed line is not an absent one"
                    )
                # The haystack is the markup-stripped, whitespace-collapsed
                # view, so the needle must be the same view of the same
                # declaration -- searching raw declared text inside the
                # normalized anchor failed for any line carrying a tag, an
                # entity, or a double space, and it failed SILENTLY: nothing
                # recorded the miss, the act reported
                # act-anchor-line-not-located for a line sitting on disk, and
                # `search_from` stayed behind the unlocated line's span. An
                # unlocatable declared line is malformed evidence, not an
                # absent act line.
                needle = markup_text_view(source)["text"]
                start = normalized_anchor.find(needle, search_from) if needle else -1
                act = next((item for item in page_acts if item["act_key"] == line["act_key"]), None)
                if start < 0:
                    raise SchemaRefusal(
                        f"the Chandra anchor line for act {line['act_key']} on page "
                        f"{page_ordinal} does not occur in the page's own anchor text at or "
                        "after the previous line; an unlocatable declared line is malformed "
                        "evidence, not an absent act line"
                    )
                if start >= 0:
                    if act is not None:
                        if (page_ordinal, act["act_id"]) in anchor_ranges:
                            # The same malformed-vs-absent rule as every branch
                            # above: keeping the last line would drop the first
                            # line's span and geometry without a record, and the
                            # dropped half's characters would read as witness
                            # departure. The day an act genuinely owns several
                            # anchor lines, line_geometry carries all of them --
                            # it does not keep the last.
                            raise SchemaRefusal(
                                f"page {page_ordinal} declares more than one Chandra anchor "
                                f"line for act {line['act_key']}; keeping the last one would "
                                "drop the first line's span and geometry without a record"
                            )
                        bbox = {key: line.get(key) for key in ("x", "y", "w", "h")}
                        if (
                            any(
                                not isinstance(value, int) or isinstance(value, bool)
                                for value in bbox.values()
                            )
                            or bbox["x"] < 0
                            or bbox["y"] < 0
                            or bbox["w"] <= 0
                            or bbox["h"] <= 0
                        ):
                            # A null, non-integer, or negative coordinate is a
                            # default standing in for geometry nobody measured;
                            # published as this act's line_geometry it would be
                            # indistinguishable from a real rectangle -- or be a
                            # rectangle nothing can draw, refused two stages
                            # later as a type error at the consumer instead of
                            # here, at the declaration (GOVERNANCE 2/10).
                            raise SchemaRefusal(
                                f"the Chandra anchor line for act {line['act_key']} on page "
                                f"{page_ordinal} declares an unusable rectangle; only measured "
                                "non-negative integer geometry can be published as this act's "
                                "line geometry"
                            )
                        anchor_ranges[(page_ordinal, act["act_id"])] = {
                            "start": start,
                            "end": start + len(needle),
                            "bbox": bbox,
                        }
                    # A located line advances the cursor whether or not it maps
                    # to a proposed act on this page -- an anchor line for an
                    # unproposed act still occupies its span of the page, and
                    # leaving the cursor behind it would let the NEXT act's
                    # formulaic opening resolve into this line's text.
                    search_from = start + len(needle)

    for act in acts:
        entries: list[dict[str, Any]] = []
        for chair in context.witness_chairs:
            attempt = attempts_by_pair[(act["act_id"], chair)]
            page_witness = chair in page_chairs and act["outcome"] == "proposed"
            alignment: dict[str, Any] | None = None
            attached = act["outcome"] == "proposed" and attempt.outcome in WITNESS_READING_OUTCOMES
            if page_witness:
                act_anchor = anchor_ranges.get((act["page_ordinal"], act["act_id"]))
                if attempt.outcome not in WITNESS_READING_OUTCOMES:
                    # There is no reading to place. Running the page alignment
                    # here would manufacture an `aligned` status for text this
                    # chair never delivered on this act, and the Perlector
                    # refuses exactly that shape (`attached: False` beside an
                    # aligned alignment) -- one failed attempt would stop the
                    # act for a reason that has nothing to do with the ink.
                    # The attempt's own outcome is the explicit unaligned
                    # reason instead.
                    alignment = {
                        "status": "unaligned",
                        "reason": f"non-reading-page-attempt-{attempt.outcome}",
                    }
                elif attempt.outcome == "genuinely-empty":
                    # There is no witness text to place, which is a different fact
                    # from text that was placed and searched for in vain: bounded
                    # alignment can never succeed against an empty string (an empty
                    # `SequenceMatcher` sequence has no matching block of positive
                    # size), so running it here would turn an honest "nothing was
                    # here" into a permanent, unrecoverable "unaligned" -- silently
                    # dropping a genuine blank corroboration below the witness
                    # floor (GOVERNANCE 2/10). Attach trivially at a zero-length
                    # span instead, exactly as the act-scoped branch below already
                    # does for the same outcome.
                    alignment = {
                        "status": "aligned",
                        # A trivial attach with no located anchor line says so,
                        # and says WHICH absence: an ink-free or fallback page
                        # legitimately has no Chandra anchor at all
                        # (`no-page-anchor` -- blank confirmation stays open),
                        # while a page whose anchor exists but locates no line
                        # for this act is geometry that does not reconcile
                        # (`act-line-not-located` -- `blank_corroboration`
                        # refuses to seal a terminal blank on it). Without the
                        # distinction the Recensor and the export could not
                        # tell either from a computed alignment (GOVERNANCE
                        # 2/10).
                        "anchor_basis": (
                            "act-anchor"
                            if act_anchor is not None
                            else (
                                "no-page-anchor"
                                if anchor_texts.get(act["page_ordinal"]) is None
                                else "act-line-not-located"
                            )
                        ),
                        "anchor_span": (
                            {"start": act_anchor["start"], "end": act_anchor["start"]}
                            if act_anchor is not None
                            else {"start": 0, "end": 0}
                        ),
                        "witness_span": {"start": 0, "end": 0},
                        "line_geometry": (
                            [
                                {
                                    "bbox": {
                                        key: act_anchor["bbox"][key] for key in ("x", "y", "w", "h")
                                    }
                                }
                            ]
                            if act_anchor is not None
                            else []
                        ),
                        "loss": {"witness": _ZERO_ALIGNMENT_LOSS, "anchor": _ZERO_ALIGNMENT_LOSS},
                        "offset_maps": {"witness": [], "anchor": []},
                    }
                else:
                    page_text = page_texts.get((act["page_ordinal"], chair))
                    anchor_text = anchor_texts.get(act["page_ordinal"])
                    if page_text is None or anchor_text is None:
                        result = {"status": "unaligned", "reason": "missing-chandra-page-anchor"}
                    elif act_anchor is None:
                        # The page anchor exists; this act's line was not located
                        # in it (or the fixture declared none for it). Saying
                        # "missing-chandra-page-anchor" here sent an operator
                        # looking for an anchor file that exists.
                        result = {"status": "unaligned", "reason": "act-anchor-line-not-located"}
                    else:
                        # One alignment per (page, chair), not per (act, chair):
                        # the inputs do not depend on the act, and the design
                        # doc's own measurement puts a scattered-difference
                        # `SequenceMatcher` near CUBIC in length. Recomputing it
                        # once per act turned a page of forty acts into forty
                        # identical full-page alignments per page witness.
                        result = page_alignments.get((act["page_ordinal"], chair))
                        if result is None:
                            result = align_to_anchor(page_text, anchor_text, limits)
                            page_alignments[(act["page_ordinal"], chair)] = result
                    if result["status"] == "aligned":
                        # CLIPPED to this act's anchor range, then carried back
                        # through each block's own witness/anchor offset, rather
                        # than hulling whole overlapping blocks (R4 audit,
                        # F-X2: the hull handed every act the chair's entire
                        # page reading, inverting the dissent instrument), THEN
                        # translated from the markup-stripped normalized space
                        # the matcher measured in back to RAW page-text indices
                        # through the alignment's own offset_map (R6 audit,
                        # F-G2: every consumer of witness_span -- the Perlector
                        # comparison views, the Recensor content coverage, the
                        # act-scoped `span` mirror -- indexes the RAW retained
                        # text). Wave composition per R6-Opus's recorded
                        # verdict: clip in normalized space first, translate at
                        # this one storage point, spans stay RAW everywhere.
                        clipped = []
                        for span in result["spans"]:
                            start = max(span["anchor"]["start"], act_anchor["start"])
                            end = min(span["anchor"]["end"], act_anchor["end"])
                            if start < end:
                                shift = span["witness"]["start"] - span["anchor"]["start"]
                                clipped.append((start + shift, end + shift))
                        if clipped:
                            # Still a hull ACROSS the clipped fragments: when the
                            # act's anchor range matches the witness in two
                            # separate places, the span also covers whatever the
                            # witness wrote between them, and the comparison view
                            # may carry a few of a neighbour's characters into
                            # the dissent row as a departure. That direction is
                            # deliberate -- it overstates disagreement and never
                            # hides it, which is what an instrument watching for
                            # a reader that learned to agree with witnesses
                            # needs. Do not "fix" this towards agreement.
                            normalized_start = min(start for start, _ in clipped)
                            normalized_end = max(end for _, end in clipped)
                            raw_span = _raw_span_from_normalized(
                                result["witness"]["offset_map"], normalized_start, normalized_end
                            )
                            if raw_span is None:
                                result = {
                                    "status": "unaligned",
                                    "reason": "no-raw-counterpart-for-aligned-span",
                                }
                            else:
                                witness_start, witness_end = raw_span
                                alignment = {
                                    "status": "aligned",
                                    "anchor_basis": "act-anchor",
                                    "anchor_span": {
                                        key: act_anchor[key] for key in ("start", "end")
                                    },
                                    "witness_span": {"start": witness_start, "end": witness_end},
                                    "line_geometry": [
                                        {
                                            "bbox": {
                                                key: act_anchor["bbox"][key]
                                                for key in ("x", "y", "w", "h")
                                            }
                                        }
                                    ],
                                    "loss": {
                                        "witness": result["witness"]["loss"],
                                        "anchor": result["anchor"]["loss"],
                                    },
                                    "offset_maps": {
                                        "witness": result["witness"]["offset_map"],
                                        "anchor": result["anchor"]["offset_map"],
                                    },
                                }
                        else:
                            result = {"status": "unaligned", "reason": "no-overlap-with-act-anchor"}
                    if result["status"] == "unaligned":
                        alignment = {"status": "unaligned", "reason": result["reason"]}
                attached = (
                    alignment["status"] == "aligned" and attempt.outcome in WITNESS_READING_OUTCOMES
                )
            if page_witness:
                reference = page_records[(act["page_ordinal"], chair)]
                entries.append(
                    {
                        "chair": chair,
                        "page_witness": True,
                        "testimonium_ref": reference,
                        "attached": attached,
                        "content_health": attempt.health,
                        "alignment": alignment,
                        "span": (
                            {
                                "start": alignment["witness_span"]["start"],
                                "end": alignment["witness_span"]["end"],
                            }
                            if attached
                            else None
                        ),
                    }
                )
            else:
                entries.append(act_scoped_attachment_entry(context, act, chair, attempt, ordinal))
        context.publish(
            kind="act-attachment",
            subject_id=act["act_id"],
            outcome="read",
            attempt=attempt_id(act["act_id"], "act-attachment", ordinal),
            # The attachment payload retains each page/act Testimonium reference.
            # It deliberately does not make the derived record's immutable
            # publication depend on a later testimonio history surviving: the
            # tally must diagnose that missing evidence itself, not have the
            # manifest rebuild fail before it reaches the denominator check.
            inputs=[],
            payload={
                "act_key": act["act_key"],
                "attempt_ordinal": ordinal,
                "attachments": entries,
            },
        )


def attempt_pass(
    context,
    acts: list[dict[str, Any]],
    ordinal: int,
    regions_by_act: dict[str, tuple[list[dict], str | None]],
    attempts_by_pair: dict[tuple[str, str], Attempt],
) -> tuple[int, bool]:
    """Every configured chair's attempt at every expected act, at one ordinal.

    Returns how many records were written and whether any proposal crop was
    refused — the second is reported, never swallowed, because an act whose crop
    no chair could be shown is a different fact from an act every chair read. The
    region and attempt maps are the result of this invocation's no-write
    preflight. Publication therefore seals the exact attempt whose collision was
    checked, while publication order and the single write path remain unchanged.
    """
    recorded = 0
    isolated_crop_failure = False
    for act in acts:
        regions, not_read = regions_by_act[act["act_id"]]
        if not_read is not None and act["outcome"] != "held":
            # A refused crop is isolated to its act. No witness is claimed to
            # have read pixels whose lineage failed; every chair instead receives
            # an explicit non-reading record and the other acts proceed.
            isolated_crop_failure = True

        for chair in context.witness_chairs:
            resolved = context.registry.resolve(chair)
            publish_attempt(
                context,
                act=act,
                chair=chair,
                resolved=resolved,
                ordinal=ordinal,
                regions=regions,
                attempt=attempts_by_pair[(act["act_id"], chair)],
            )
            recorded += 1
    return recorded, isolated_crop_failure


def witness_bound_reading_acts(context) -> frozenset[str]:
    """Every act whose reading was already established from this act's testimony.

    The one question the attempt model turns on. `pipeline/4_perlector/run.py::
    _next_attempt` derives the reading ordinal from the act's *crop* history —
    one reading of the proposal, plus one for each recovery crop cut since — and
    the Recensor, Archetypus and Armarium each enforce that same identity. So a
    Testimonium that arrives after such a reading has nowhere to go: the Perlector
    recomputes the same ordinal, builds a different payload from the new
    testimony, and the run tree refuses the write against the record it already
    sealed. There is no forward path, because the Perlectio that would have to
    change is itself immutable (GOVERNANCE 4).

    That is the model rather than an oversight. A Testimonium is a clue that
    primes a reading, never the ink the reading is established from
    (ARCHITECTURE; GOVERNANCE 3), so a second look by a witness does not make a
    second reading exist — and re-reading an act because a witness spoke again is
    the re-roll GOVERNANCE 11 refuses ("never re-rolled until it looks better").
    The witness layer of an act is open until the act is read and closed
    afterwards.

    **Closed by a reading that cites testimony, not by any Perlectio at all.** A
    held act and an absent Perlector chair both publish `not-run` records with no
    witness basis; their bytes do not depend on the testimony, so new testimony
    wedges nothing there and a whole second pass over a run holding one held act
    must not be refused on its account.

    One walk for the whole invocation, and read from the Perlector's own artifacts
    rather than from a flag, so a folder assembled or resumed in any order answers
    the same way.
    """
    closed = set()
    for entry in context.tree.build_manifest(PERLECTOR)["artifacts"]:
        if entry["kind"] != "perlectio" or entry["subject_id"] in closed:
            continue
        record = context.tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
        basis = record.get("payload", {}).get("basis")
        if isinstance(basis, dict) and basis.get("testimonia"):
            closed.add(entry["subject_id"])
    return frozenset(closed)


def require_open_witness_layer(closed: frozenset[str], act: dict[str, Any], what: str) -> None:
    """Refuse a new witness attempt on an act the Perlector has already read.

    At entry, before anything is written. The alternative is what the audit found:
    the attempt is appended, and the wedge it makes is discovered three stages
    later as an immutability refusal on a reading identity nothing can move — with
    no forward path, because the Perlectio that would have to change is itself
    immutable (GOVERNANCE 4).

    A *rerun* of an attempt already sealed is untouched by this: it rewrites
    byte-identical bytes, changes no chair's current record, and is how the
    orchestrator resumes. Only an append is refused.
    """
    if act["act_id"] in closed:
        raise ContractError(
            f"act {act['act_id']} ({act['act_key']}) already carries a Perlectio, so its "
            f"witness layer is closed: {what} would append testimony no reading can be "
            "established from. A witness is only ever shown the act's original proposal "
            "crop, so a second look adds priming, never coverage — and re-reading an act "
            "because a witness spoke again is the re-roll GOVERNANCE 11 refuses. "
            "Re-witness before the Perlector reads the act, or let this reading stand; "
            "the doubt has no dedicated review channel today beyond the run's own records"
        )


def next_attempt_ordinal(history: AttemptHistory, act_id: str, chair: str) -> int:
    """The ordinal a reread of this one chair appends at.

    Derived from that chair's own history on disk, exactly as
    `pipeline/2_designator/run.py::_next_region_ordinal` derives the next crop
    ordinal — so append-only is a property of what already exists rather than of
    how many times this program has been invoked.
    """
    records = history.get((act_id, chair), [])
    if not records:
        raise ContractError(
            f"a reread named chair {chair!r} on act {act_id!r}, which has no prior attempt for "
            "that chair to follow — a reread is a second attempt, and there is no first"
        )
    current = latest_attempt(
        records, f"Testimonium for {(act_id, chair)!r}", operation=f"read:{chair}"
    )
    return current["payload"]["attempt_ordinal"] + 1


def reread_pass(
    context,
    acts: list[dict[str, Any]],
    act_id: str,
    chair: str,
    index: "AttemptIndex",
) -> int:
    """Append one new attempt for one named chair on one named act.

    The whole-pass `--attempt-ordinal` is the wrong instrument for this. A reread
    happens because *one* chair failed on *one* act; re-witnessing every chair on
    every act to reach it re-reads ink nobody doubted, costs a provider call per
    chair per act, and moves every other chair's derived-current record for no
    reason. This path moves exactly the chair named, and every other chair's
    current record stays the attempt it already was.

    Everything else matches the whole pass: same declaration tables at the new
    ordinal, same regions the first attempt was shown (a reread is a second look
    at the original proposal, never a first look at ink a recovery uncovered),
    same single write path, and no pointer anywhere — "current" stays derived.
    """
    act = next((row for row in acts if row["act_id"] == act_id), None)
    if act is None:
        raise ContractError(
            f"a reread named act {act_id!r}, which the Designator proposal seal does not"
        )
    if chair not in context.witness_chairs:
        raise ContractError(f"a reread named chair {chair!r}, which this run is not sealed with")
    if act["outcome"] == "held":
        raise ContractError(f"act {act_id} is held; no witness was shown a reading there to reread")
    resolved = context.registry.resolve(chair)
    if isinstance(resolved, AbsentChair):
        raise ContractError(
            f"chair {chair!r} is explicitly absent: {resolved.reason}; there is no witness "
            "to reread"
        )
    if chair in declared_page_witness_chairs(context):
        # A page witness reports one reading of one page. Its act-level view is
        # *derived* — the page join, then the alignment of that join against the
        # page anchor — so there is no act-scoped request to put to it a second
        # time, and re-deriving one act's view from an attempt the page record
        # does not describe would leave the page Testimonium and the attachment
        # disagreeing about the same chair. No operation exists today to re-ask
        # a page witness about anything: building one would be new, page-scoped
        # Attestatores work, and it is deliberately not half-performed here.
        # (The recovery vocabulary's `page-level-reread` is a PERLECTOR
        # operation — a different concept whose name must not be borrowed for
        # this one; one word per concept.)
        raise ContractError(
            f"chair {chair!r} is a page witness on this fixture: it reports one reading per "
            "page and its act-level view is derived from that page reading, so there is no "
            f"act-scoped attempt for act {act_id} to repeat. No operation exists to re-ask "
            "a page witness; building one would be new page-scoped Attestatores work, and "
            "an act-scoped reread of a derived view is not it"
        )
    require_open_witness_layer(
        witness_bound_reading_acts(context), act, f"a reread of chair {chair!r}"
    )

    # No `require_appendable_ordinal` here: `next_attempt_ordinal` returns the
    # current ordinal plus one, off the same history, so the bound cannot fire.
    ordinal = next_attempt_ordinal(index.by_pair, act_id, chair)
    attempt = resolve_attempt(
        context,
        act,
        chair,
        resolved,
        declarations_for(context, ordinal),
        reread=True,
    )
    next_ordinal, entries = prepared_act_attachment(context, index, act, chair)
    publish_attempt(
        context,
        act=act,
        chair=chair,
        resolved=resolved,
        ordinal=ordinal,
        regions=proposed_regions(context, act_id),
        attempt=attempt,
    )
    republish_act_attachment(context, act, chair, attempt, ordinal, next_ordinal, entries)
    return 1


def prepared_act_attachment(
    context,
    index: "AttemptIndex",
    act: dict[str, Any],
    chair: str,
) -> tuple[int, list[dict[str, Any] | None]]:
    """Every refusal for the reread's re-derived attachment, WITHOUT writing.

    Split from the publication deliberately: all three refusals here depend only
    on state that existed before the reread writes anything, and running them
    after `publish_attempt` is how a damaged tree could strand a sealed
    Testimonium its manifest does not yet name — the exact failure
    `require_shared_whole_pass_ordinal`'s docstring condemns on the whole-pass
    path. The reread preflights this first, publishes the Testimonium second,
    and publishes the attachment last, so a refusal leaves the folder untouched.

    The reread chair's own slot comes back as `None`: its re-derived entry
    references the NEW Testimonium by digest, so it can only be built after the
    publish (`republish_act_attachment` fills it). The other chairs' attempts
    did not move, so their entries are carried forward — but *checked* first,
    so a stale entry is refused rather than laundered into a newer record by a
    reread that has nothing to do with it.
    """
    records = index.attachments_by_act.get(act["act_id"], [])
    if not records:
        raise ContractError(
            f"act {act['act_id']} has no act-attachment for the reread to re-derive; a "
            "targeted reread follows a whole pass and never stands in for one"
        )
    current = latest_attempt(
        records, f"act-attachment for {act['act_id']}", operation="act-attachment"
    )
    attachments = current.get("payload", {}).get("attachments")
    if not isinstance(attachments, list) or {
        item.get("chair") if isinstance(item, dict) else None for item in attachments
    } != set(context.witness_chairs):
        raise SchemaRefusal(
            f"act {act['act_id']}'s current act-attachment does not describe this run's "
            "configured witnesses; a reread may not re-derive it"
        )
    entries: list[dict[str, Any] | None] = []
    for item in attachments:
        if item["chair"] == chair:
            entries.append(None)
            continue
        other = latest_attempt(
            index.by_pair.get((act["act_id"], item["chair"]), []),
            f"Testimonium for {(act['act_id'], item['chair'])!r}",
            operation=f"read:{item['chair']}",
        )
        if item.get("content_health") != other["payload"].get("content_health"):
            raise SchemaRefusal(
                f"act {act['act_id']}'s current act-attachment already describes an attempt "
                f"that is no longer chair {item['chair']!r}'s current Testimonium; a reread "
                "of another chair does not make that record current again"
            )
        if (
            not item.get("page_witness")
            and item.get("attached")
            and other["outcome"] not in WITNESS_READING_OUTCOMES
        ):
            # The other half of staleness for an act-scoped carried entry: a
            # positive `attached` over a chair whose current outcome is not a
            # reading. A page witness's `attached` is alignment-derived and may
            # legitimately diverge; the Perlector's own guard holds that case.
            raise SchemaRefusal(
                f"act {act['act_id']}'s current act-attachment claims chair "
                f"{item['chair']!r} attached while its current outcome is "
                f"{other['outcome']!r}; a reread of another chair does not make that "
                "claim current again"
            )
        entries.append(item)
    return current["payload"]["attempt_ordinal"] + 1, entries


def republish_act_attachment(
    context,
    act: dict[str, Any],
    chair: str,
    attempt: "Attempt",
    ordinal: int,
    next_ordinal: int,
    entries: list[dict[str, Any] | None],
) -> None:
    """Publish the attachment `prepared_act_attachment` already checked.

    The reread chair's `None` slot is filled here, after its Testimonium exists
    to be referenced by digest; every other entry was carried and checked in the
    preflight.
    """
    filled = [
        act_scoped_attachment_entry(context, act, chair, attempt, ordinal) if item is None else item
        for item in entries
    ]
    context.publish(
        kind="act-attachment",
        subject_id=act["act_id"],
        outcome="read",
        attempt=attempt_id(act["act_id"], "act-attachment", next_ordinal),
        inputs=[],
        payload={
            "act_key": act["act_key"],
            "attempt_ordinal": next_ordinal,
            "attachments": filled,
        },
    )


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run every configured chair through one attempt, or reread one named chair."""
    parser = stage_parser(__doc__.splitlines()[0], accepts_chair=True)
    parser.add_argument(
        "--attempt-ordinal",
        type=_positive_ordinal,
        # No default ordinal: a reread derives its own from the named chair's
        # history, and a default would make "asked for ordinal 1" and "asked for
        # nothing" the same argv, so the reread could not say it was overridden.
        default=None,
        help="append this ordinal for every act/chair, or repeat the current one byte-identically",
    )
    args = parser.parse_args()
    if args.operation not in OPERATIONS:
        raise ContractError(
            f"the Attestatores has no {args.operation!r} operation; it implements "
            f"{sorted(OPERATIONS)}. A mistyped reread would otherwise run a whole pass, "
            "ignore the act and chair it was given, and report success"
        )
    context = open_context(args, ATTESTATORES, registry_factory=registry_factory)
    acts = expected_acts(context)
    try:
        index = _attempt_history(context)
    except FatalAccounting:
        raise
    except ContractError as error:
        print(f"Attestatores attempt tally UNKNOWN: {error}", file=sys.stderr)
        return EXIT_HELD
    # The stored inventory is evidence that attempts existed, and it is evidence
    # even when none of them is left. Gating the check below on the *walk* finding
    # something meant that losing part of a folder's Testimonium layer held it —
    # stored and rebuilt no longer agree — while losing all of it did not: the
    # first-run path was taken instead, attempt 1 was written for every pair, and
    # `context.finish()` rewrote the inventory that said otherwise. A reread makes
    # that loss material rather than merely re-derivable, because the ordinal it
    # appended does not come back. So the stored manifest's own existence is the
    # second trigger, and `attempt_tally` then says what it always says about an
    # inventory that disagrees with the evidence: UNKNOWN, and hold. Re-deriving it
    # deliberately (`RunTree.write_manifest`) remains the one-step way out, exactly
    # as it is for a pass interrupted before its manifest was written.
    stored_inventory = context.tree.resolve(context.tree.manifest_path(ATTESTATORES)).exists()
    if index.stage_has_artifacts or stored_inventory:
        # No chair denominator here: this pass is what fills it. See `attempt_tally`.
        prior_tally = attempt_tally(context.tree, context=context, acts=acts)
        if prior_tally["hold"]:
            print(f"Attestatores attempt tally UNKNOWN: {prior_tally['reason']}", file=sys.stderr)
            return EXIT_HELD

    isolated_crop_failure = False
    if args.operation == "reread":
        if not args.act or not args.chair:
            raise ContractError(
                "a reread names the one act and the one chair it rereads; without both it "
                "would be a whole second pass wearing a narrower name"
            )
        if args.attempt_ordinal is not None:
            raise ContractError(
                "a reread appends at the ordinal the named chair's own history says comes "
                f"next; --attempt-ordinal {args.attempt_ordinal} names a different attempt "
                "and honouring neither of the two silently is not an option"
            )
        recorded = reread_pass(context, acts, args.act, args.chair, index)
    else:
        if args.act or args.chair:
            raise ContractError(
                "--act and --chair name a targeted reread; a whole pass reads every "
                "configured chair on every expected act and cannot narrow to them"
            )
        ordinal = 1 if args.attempt_ordinal is None else args.attempt_ordinal
        try:
            declarations = declarations_for(context, ordinal)
            regions_by_act, attempts_by_pair = preflight_appendable_ordinals(
                context,
                acts,
                ordinal,
                declarations,
                index,
            )
        except ContractError as error:
            # An ordinary preflight refusal holds this pass before it writes any
            # witness artifact. An accounting imbalance is a broken partition, not
            # a holdable request refusal; it must still reach the fatal boundary.
            if isinstance(error, FatalAccounting):
                raise
            print(f"Attestatores refused this pass: {error}", file=sys.stderr)
            return EXIT_HELD
        recorded, isolated_crop_failure = attempt_pass(
            context,
            acts,
            ordinal,
            regions_by_act,
            attempts_by_pair,
        )
        publish_page_testimonia_and_attachments(
            context, acts=acts, ordinal=ordinal, attempts_by_pair=attempts_by_pair
        )

    if recorded == 0:
        raise ContractError("no chair produced an outcome for any act")

    context.finish()
    tally = attempt_tally(context.tree, context=context, acts=acts, chairs=context.witness_chairs)
    if tally["hold"]:
        print(f"Attestatores attempt tally UNKNOWN: {tally['reason']}", file=sys.stderr)
        return EXIT_HELD
    if isolated_crop_failure:
        # Every chair still has its explicit non-reading artifact, so retention
        # completed and later stages can make that partial state visible. This is
        # distinct from an UNKNOWN evidence tally, which is the only stage-3 hold.
        print("Attestatores recorded one or more refused proposal crops", file=sys.stderr)
    return EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
