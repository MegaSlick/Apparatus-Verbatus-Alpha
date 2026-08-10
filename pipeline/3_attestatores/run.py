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

from common.chairs.models import AbsentChair, ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal  # noqa: E402
from common.contracts.identities import act_id as derive_act_id  # noqa: E402
from common.contracts.identities import attempt_id  # noqa: E402
from common.contracts.stages import ATTESTATORES, DESIGNATOR  # noqa: E402
from common.exemplar_boundary import verify_exemplar_crop_lineage  # noqa: E402
from common.stage import (  # noqa: E402
    ATTEMPTED_WITNESS_OUTCOMES,
    EXIT_COMPLETE,
    EXIT_HELD,
    FALLBACK_PAGE_ACT_ORDINAL,
    WITNESS_READING_OUTCOMES,
    expected_acts,
    fixture_serving_details,
    latest_attempt,
    open_context,
    run_stage,
    stage_parser,
    validate_serving_provenance,
)

DEFAULT_FORMAT_CAPABILITIES = {
    "can_express_uncertainty": False,
    "can_express_layout": False,
}

# The two write paths this program implements, named as a closed set because
# `--operation` carries no `choices`: the fixture, not argparse, is the authority
# on the *scenario* list, and the same parser serves every stage. An unrecognized
# operation used to fall through to the whole pass, so a mistyped reread re-read
# nothing, ignored the `--act` and `--chair` it was given, and exited 0.
OPERATIONS = frozenset({"initial", "reread"})

# A witness response is untrusted input, and unbounded recursion over it is a
# resource-exhaustion hole in the same family as the ones this stage already
# refuses (bad UTF-8, non-string keys): a several-thousand-deep nested list or
# object drives `_native_problem` past Python's recursion limit and raises
# `RecursionError`, which is not a `ContractError` and so is not caught anywhere
# between here and the process exiting — one adversarial witness would crash the
# whole folder, the exact thing section C's isolation bullet exists to refuse.
# Real transcription output nests a handful of levels deep at most; this cap is
# generous headroom, not a tight fit.
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
    return {
        (row["act_key"], row["chair"])
        for row in context.fixture.get(fixture_key, [])
        if row["scenario"] == context.scenario and _declared_for_ordinal(row, ordinal)
    }


def _page_fallback_bounds(context) -> dict[str, dict]:
    """Index the Designator rectangles already verified by `expected_acts`."""
    bounds_by_act = {}
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] != "page-fallback":
            continue
        act_id = entry["subject_id"]
        if act_id in bounds_by_act:
            raise SchemaRefusal(f"act {act_id} has more than one Designator page-fallback record")
        record = context.tree.read_artifact(DESIGNATOR, "page-fallback", entry["artifact_id"])
        page_bounds = record.get("payload", {}).get("page_bounds")
        if not isinstance(page_bounds, dict):
            raise SchemaRefusal(
                f"act {act_id}'s Designator page-fallback record carries no page rectangle"
            )
        bounds_by_act[act_id] = page_bounds
    return bounds_by_act


def _is_page_fallback(context, act: dict, bounds_by_act: dict[str, dict] | None = None) -> bool:
    """Recognize the reserved minted identity, not merely its human-readable key."""
    if bounds_by_act is None:
        bounds_by_act = _page_fallback_bounds(context)
    page_bounds = bounds_by_act.get(act["act_id"])
    if page_bounds is None:
        return False
    return act["act_id"] == derive_act_id(
        act["page_id"], FALLBACK_PAGE_ACT_ORDINAL, page_bounds
    )
def declared_malformed(context, ordinal: int) -> dict[tuple[str, str], str]:
    """Fixture stand-in for a provider response the recording channel could not keep."""
    rows: dict[tuple[str, str], str] = {}
    for row in context.fixture.get("witness_malformed", []):
        if row["scenario"] != context.scenario or not _declared_for_ordinal(row, ordinal):
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
    """The fixture's one native response declaration for this exact attempt."""
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
    JSON syntax remain valid.  `recordable=False` is allowed through this shape
    check only so the caller can turn that explicitly unrecordable channel into
    UNKNOWN and hold the folder.
    """
    if not isinstance(health, dict):
        raise SchemaRefusal("a Testimonium carries no object content_health record")
    required = set(NO_RESPONSE_HEALTH) | {"truncation_basis"}
    if missing := sorted(required - set(health)):
        raise SchemaRefusal(f"a Testimonium content_health record lacks field(s) {missing}")

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
    try:
        capabilities = format_capabilities_for(row)
    except SchemaRefusal as error:
        reason = f"the witness format capabilities could not be retained: {error}"
        return native_payload, witness_reported, None, health, reason
    if problem := _native_problem(witness_reported, "witness_reported"):
        return (
            native_payload,
            None,
            capabilities,
            health,
            f"the witness self-report could not be retained: {problem}",
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


def _existing_attempts(context, act_id: str, chair: str) -> list[dict[str, Any]]:
    records = []
    for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "testimonium" or entry["subject_id"] != act_id:
            continue
        record = context.tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        if record.get("payload", {}).get("chair") == chair:
            records.append(record)
    return records


def require_appendable_ordinal(context, act_id: str, chair: str, ordinal: int) -> None:
    """Allow only a rerun of an attempt that exists, or exactly the next one.

    Ordinals are the contiguous run 1..N — `latest_attempt` refuses a gap — so any
    ordinal at or below the current one names an attempt that is already on disk,
    and rewriting it is a resume: the RunTree refuses it outright if the bytes
    differ. Only `current + 1` adds anything.

    The bound is `<= current + 1` rather than `in {current, current + 1}` because a
    targeted reread moves one chair's ordinal without moving any other's. Insisting
    every pair be at the same ordinal would mean the orchestrator — which always
    asks for ordinal 1 — held the whole folder from the moment one chair was reread,
    over five writes that would have been byte-identical no-ops.
    """
    records = _existing_attempts(context, act_id, chair)
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


def preflight_appendable_ordinals(context, acts: list[dict[str, Any]], ordinal: int) -> None:
    """Refuse a damaged history before adding any new attempt to this invocation."""
    for act in acts:
        for chair in context.witness_chairs:
            require_appendable_ordinal(context, act["act_id"], chair, ordinal)


def validate_tallied_testimonium(context, record: dict[str, Any], act: dict[str, Any]) -> None:
    """Refuse a resealed Testimonium that this stage could not have produced.

    The generic envelope proves a record is syntactically sealed; the attempt
    tally also has to prove its stage-specific channel remains interpretable
    before authorizing another immutable append. This deliberately validates no
    witness's *content* and makes no quality decision.
    """
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise SchemaRefusal("a Testimonium tally record has no object payload")
    if missing := sorted(TESTIMONIUM_FIELDS - set(payload)):
        raise SchemaRefusal(f"a Testimonium tally record lacks required field(s) {missing}")
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
        regions = proposed_regions(context, act["act_id"])
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

    Spec 07 asks for two things that pull apart if `recordable=False` is read as
    one fact. Its isolation bullet: "one bad crop, one dead witness, one malformed
    response never kills the folder ... Malformed provider output is recorded as a
    failed attempt and refused, not repaired silently". Its retention bullet, on
    invariant #23: "a damaged or unrecordable evidence channel makes the count
    UNKNOWN, and UNKNOWN holds the folder".

    Both are true, because they are about different channels. #23's evidence
    channel is *the attempt tally itself* — the independent count of what was
    attempted, which is what the old pipeline could not produce. A provider
    response this stage could not retain is not that: it is one witness's own
    output, and the `failed` attempt naming it is exactly the record the isolation
    bullet asks for. That attempt is countable, counted, and visibly failed
    downstream; the act it belongs to goes under-witnessed and the run goes
    partial. Holding the whole folder for it would stop the Perlector reading ink
    that is not in doubt because one witness of three returned rubbish — and with
    real providers and damaged registers that is the common case, not the edge
    one.

    So an unrecordable channel is accounted only inside an honestly `failed`
    attempt that says why. A record that claims to be a *reading* while saying its
    own channel could not be recorded is incoherent — the one shape that cannot be
    resolved in the run's favour — and it is #23's UNKNOWN.
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
    except (ContractError, OSError, UnicodeDecodeError, ValueError) as error:
        return {"state": "UNKNOWN", "count": None, "hold": True, "reason": str(error)}
    if stored != rebuilt:
        return {
            "state": "UNKNOWN",
            "count": None,
            "hold": True,
            "reason": "the stored Attestatores manifest does not equal its rebuilt inventory",
        }

    testimonia = [entry for entry in rebuilt["artifacts"] if entry["kind"] == "testimonium"]
    by_act = {act["act_id"]: act for act in acts or ()}
    try:
        by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for entry in testimonia:
            record = tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
            payload = record.get("payload")
            if not isinstance(payload, dict):
                raise SchemaRefusal("a Testimonium carries no object payload")
            if missing := sorted(TESTIMONIUM_FIELDS - set(payload)):
                raise SchemaRefusal(f"a Testimonium carries no required field(s) {missing}")
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
                validate_tallied_testimonium(context, record, act)
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
    except (ContractError, OSError) as error:
        return {"state": "UNKNOWN", "count": None, "hold": True, "reason": str(error)}
    return {"state": "KNOWN", "count": len(testimonia), "hold": False, "reason": None}


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

    Assembled in exactly one place so the whole-pass write path and the targeted
    reread cannot drift on what a witness attempt is. It describes one chair and
    reads no other chair's record: nothing here compares, ranks, or chooses among
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

    `empty` is a completed empty reading, distinct from an absent response;
    `not_run` is a configured chair deliberately never asked for this attempt.
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
    elif _is_page_fallback(context, act) or key in declarations["empty"]:
        outcome = "genuinely-empty"
        native_payload = ""
        health = content_health(native_payload, completed=True)
    else:
        response = testimony_for(context, act["act_key"], chair, declarations["ordinal"])
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
            if recording_problem is None:
                outcome = "read"
            else:
                outcome = "failed"
                reason = f"the provider response was refused without repair: {recording_problem}"

    return Attempt(outcome, native_payload, witness_reported, capabilities, health, reason)


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
    context.publish(
        kind="testimonium",
        subject_id=act["act_id"],
        outcome=attempt.outcome,
        attempt=attempt_id(act["act_id"], f"read:{chair}", ordinal),
        inputs=region_inputs(context, regions) if attempted else [],
        payload=testimonium_payload(
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
        ),
    )


def attempt_pass(context, acts: list[dict[str, Any]], ordinal: int) -> tuple[int, bool]:
    """Every configured chair's attempt at every expected act, at one ordinal.

    Returns how many records were written and whether any proposal crop was
    refused — the second is reported, never swallowed, because an act whose crop
    no chair could be shown is a different fact from an act every chair read.
    """
    declarations = declarations_for(context, ordinal)
    recorded = 0
    isolated_crop_failure = False
    for act in acts:
        regions: list[dict] = []
        not_read: str | None = None
        if act["outcome"] == "held":
            not_read = (
                "the Designator held this act; its incomplete proposal was not shown "
                "to any configured witness"
            )
        else:
            try:
                regions = proposed_regions(context, act["act_id"])
            except ContractError as error:
                if isinstance(error, FatalAccounting):
                    raise
                # A refused crop is isolated to its act. No witness is claimed to
                # have read pixels whose lineage failed; every chair instead
                # receives an explicit non-reading record and the other acts
                # proceed.
                not_read = f"the proposed region was refused before this chair ran: {error}"
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
                attempt=(
                    not_read_attempt(resolved, not_read)
                    if not_read is not None
                    else resolve_attempt(context, act, chair, resolved, declarations)
                ),
            )
            recorded += 1
    return recorded, isolated_crop_failure


def next_attempt_ordinal(context, act_id: str, chair: str) -> int:
    """The ordinal a reread of this one chair appends at.

    Derived from that chair's own history on disk, exactly as
    `pipeline/2_designator/run.py::_next_region_ordinal` derives the next crop
    ordinal — so append-only is a property of what already exists rather than of
    how many times this program has been invoked.
    """
    records = _existing_attempts(context, act_id, chair)
    if not records:
        raise ContractError(
            f"a reread named chair {chair!r} on act {act_id!r}, which has no prior attempt for "
            "that chair to follow — a reread is a second attempt, and there is no first"
        )
    current = latest_attempt(
        records, f"Testimonium for {(act_id, chair)!r}", operation=f"read:{chair}"
    )
    return current["payload"]["attempt_ordinal"] + 1


def reread_pass(context, acts: list[dict[str, Any]], act_id: str, chair: str) -> int:
    """Append one new attempt for one named chair on one named act.

    Lane A's shape, kept because the whole-pass `--attempt-ordinal` is the wrong
    instrument for the thing a reread is actually for. A reread happens because
    *one* chair failed on *one* act; re-witnessing every chair on every act to
    reach it re-reads ink nobody doubted, costs a provider call per chair per
    act, and moves every other chair's derived-current record for no reason. This
    path moves exactly the one chair named, and every other chair's current record
    stays the attempt it already was.

    The rest is unchanged from the whole pass: same declaration tables at the new
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

    # No `require_appendable_ordinal` here: `next_attempt_ordinal` returns the
    # current ordinal plus one, off the same history, so the bound cannot fire.
    ordinal = next_attempt_ordinal(context, act_id, chair)
    publish_attempt(
        context,
        act=act,
        chair=chair,
        resolved=resolved,
        ordinal=ordinal,
        regions=proposed_regions(context, act_id),
        attempt=resolve_attempt(
            context, act, chair, resolved, declarations_for(context, ordinal), reread=True
        ),
    )
    return 1


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run every configured chair through one attempt, or reread one named chair."""
    parser = stage_parser(__doc__.splitlines()[0])
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
        has_existing_attempts = bool(context.tree.build_manifest(ATTESTATORES)["artifacts"])
    except ContractError as error:
        print(f"Attestatores attempt tally UNKNOWN: {error}", file=sys.stderr)
        return EXIT_HELD
    if has_existing_attempts:
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
        recorded = reread_pass(context, acts, args.act, args.chair)
    else:
        if args.act or args.chair:
            raise ContractError(
                "--act and --chair name a targeted reread; a whole pass reads every "
                "configured chair on every expected act and cannot narrow to them"
            )
        ordinal = 1 if args.attempt_ordinal is None else args.attempt_ordinal
        try:
            preflight_appendable_ordinals(context, acts, ordinal)
        except ContractError as error:
            # A damaged existing channel cannot be repaired by adding a replacement
            # attempt. It is an UNKNOWN tally and holds the folder as it stands.
            print(f"Attestatores attempt tally UNKNOWN: {error}", file=sys.stderr)
            return EXIT_HELD
        recorded, isolated_crop_failure = attempt_pass(context, acts, ordinal)

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
