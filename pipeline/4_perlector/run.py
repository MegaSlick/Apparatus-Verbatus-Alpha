"""Perlector: reads the ink, with the testimonia as fallible clues.

The fake here proves wiring and nothing else — its text comes from the fixture, so
it demonstrates exactly zero about reading. What it *does* prove is the shape of
the record, and the shape is where GOVERNANCE 3 either holds or quietly fails:

  It verifies the region evidence.        The stage reads the bytes, checks their
                                          digest against the sealed reference, and
                                          decodes them to confirm the image is the
                                          size the transform claims. The fixture
                                          reader observes pixels only to prove a
                                          page-fallback act empty; ordinary text
                                          remains declared fixture text.
  It records its basis.                   The region it read, and every testimonium
                                          it saw, by reference.
  It never counts witnesses.              No branch anywhere in this file reads how
                                          many chairs agreed. The dissent record is
                                          computed *after* the reading is fixed,
                                          and cannot reach back into it.

Dissent is structural, not evaluative: it records where the reading departed from
each witness, which makes parroting measurable without new instrumentation. It is
not a quality signal — most lines in a register are easy and every witness agrees,
and zero dissent there is the correct output.

    python pipeline/4_perlector/run.py --run-root <dir> --run-id <id>
"""

import sys
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import annotations  # noqa: E402
import dossier as dossier_module  # noqa: E402
import nuda  # noqa: E402
import prompts  # noqa: E402
import regime  # noqa: E402
import truncation  # noqa: E402
from dissent import dissent_against, validate_dissent  # noqa: E402
from reader import FixtureReader  # noqa: E402

from common.chairs.models import AbsentChair, ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.canonical import digest_of  # noqa: E402
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal  # noqa: E402
from common.contracts.identities import attempt_id  # noqa: E402
from common.contracts.stages import ATTESTATORES, DESIGNATOR, PERLECTOR  # noqa: E402
from common.exemplar_boundary import verify_exemplar_crop_lineage  # noqa: E402
from common.stage import (  # noqa: E402
    ATTEMPTED_WITNESS_OUTCOMES,
    EXIT_COMPLETE,
    PERLECTOR_CHAIR,
    WITNESS_CONTEXT_REGIMES,
    WITNESS_READING_OUTCOMES,
    expected_acts,
    fixture_serving_details,
    latest_attempt,
    latest_per_chair,
    open_context,
    run_stage,
    stage_parser,
    validate_serving_provenance,
)


def regions_of(context, act_id: str) -> list[dict]:
    """Every Designator region for this act, its own provenance verified first.

    `pipeline/3_attestatores/run.py::proposed_regions` already validates the
    identical artifact kind before showing a region to a witness; reading it here
    unvalidated would let a tampered Designator provenance reach a real reading
    while the equivalent tamper on a Testimonium is refused. A region always
    carries a receipt-backed provenance -- `structure_provenance` refuses before
    any region is cut if the Designator chair is absent or unverifiable -- so
    every region validated here requires one.
    """
    records = []
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] == "region" and entry["subject_id"] == act_id:
            record = context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            validate_serving_provenance(
                context,
                record.get("payload", {}).get("provenance"),
                producer_stage=DESIGNATOR,
                require_receipt=True,
            )
            records.append(record)
    return sorted(records, key=_region_ordinal)


def _region_ordinal(record: dict) -> int:
    """The sort key, refused by name rather than escaping as a raw `KeyError`.

    Ordering happens before `verify_region` validates the region, so this is
    the one place a resealed record whose payload lost `attempt_ordinal` is
    read — and a stage boundary that answers untrusted input with a traceback
    is the one thing the Designator's own tests assert never appears in stderr.
    """
    ordinal = record.get("payload", {}).get("attempt_ordinal")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool):
        raise SchemaRefusal("a Designator region carries no integer attempt ordinal to order by")
    return ordinal


def act_regions(context, act_id: str) -> tuple[list[dict], list[dict]]:
    """Every region for this act, and the original-proposal subset of it.

    One spelling, because the preflight and the reading loop both need the pair
    and both refuse the same way. A second copy of the filter is how the two
    come to disagree about what counts as a proposal region, which is the fact
    the witness-coverage record is built on.
    """
    regions = regions_of(context, act_id)
    proposals = [region for region in regions if region["payload"].get("origin") == "proposal"]
    if not proposals:
        raise ContractError(f"act {act_id} reached the Perlector with no original proposal region")
    return regions, proposals


def _region_reference(region: dict) -> dict[str, str]:
    """The exact public region facts a Testimonium may claim it saw."""
    payload = region["payload"]
    return {
        "region_id": payload["region_id"],
        "image_path": payload["image_path"],
        "image_sha256": payload["image_sha256"],
    }


def _testimonium_inputs(context, regions: list[dict]) -> list[dict[str, str]]:
    """The crop blobs that a witness attempt must directly bind."""
    inputs = {}
    for region in regions:
        reference = context.input_ref(region["payload"]["image_path"])
        inputs[reference["relative_path"]] = reference
    return sorted(inputs.values(), key=lambda item: (item["relative_path"], item["sha256"]))


def validate_testimonium_regions(context, record: dict, proposal_regions: list[dict]) -> None:
    """Bind a witness's claimed regions to the actual original proposal.

    A Testimonium's direct blob references prove its pixels have not changed, but
    not by themselves that the `regions` payload names the proposal it was shown.
    Without this comparison, a resealed record could add a recovery crop and make
    Perlector mark newly uncovered ink as witnessed.  Recovery must stay visible
    as recovery, not become retrospective witness coverage.
    """
    payload = record["payload"]
    attempted = record["outcome"] in ATTEMPTED_WITNESS_OUTCOMES
    expected_regions = (
        [_region_reference(region) for region in proposal_regions] if attempted else []
    )
    expected_inputs = _testimonium_inputs(context, proposal_regions) if attempted else []
    if payload.get("regions") != expected_regions or record.get("inputs") != expected_inputs:
        raise SchemaRefusal(
            "a Testimonium does not bind exactly the original proposal regions and pixel "
            "inputs it claims its witness saw"
        )


def testimonia_of(context, act_id: str, proposal_regions: list[dict]) -> list[dict]:
    """Every chair's current testimonium for this act — the latest attempt only.

    Attempts are append-only (GOVERNANCE 4): a failed re-read is recorded beside
    the earlier success, never over it. Every record is still read and its
    provenance still validated, but only each chair's latest attempt is returned
    as evidence — the same collapsing `pipeline/5_recensor/run.py::chair_outcomes`
    already does for the identical artifacts, so dissent, witness-coverage, and the
    Perlectio's own recorded basis cannot see a superseded attempt as though it
    were still live.
    """
    records = []
    for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] == "testimonium" and entry["subject_id"] == act_id:
            record = context.tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
            validate_serving_provenance(
                context,
                record.get("payload", {}).get("provenance"),
                producer_stage=ATTESTATORES,
                require_receipt=record["outcome"] in ATTEMPTED_WITNESS_OUTCOMES,
            )
            validate_testimonium_regions(context, record, proposal_regions)
            records.append(record)
    current = latest_per_chair(records, f"testimonium for {act_id}")
    chairs = {record["payload"]["chair"] for record in current}
    configured = set(context.witness_chairs)
    missing = configured - chairs
    if missing:
        raise FatalAccounting(
            f"act {act_id} has no current Testimonium for configured chair(s) {sorted(missing)}; "
            "the Perlector may not seal a reading over a shortened witness denominator"
        )
    unsealed = chairs - configured
    if unsealed:
        raise FatalAccounting(
            f"act {act_id} carries Testimonium from chair(s) {sorted(unsealed)}, which this "
            "run was not sealed with"
        )
    return current


def act_attachment_view(context, act: dict[str, Any]) -> dict[str, Any]:
    """Validate the R0 attachment that makes a page witness act-addressable.

    R4 owns alignment; until then this is a declared span view, retained beside
    the page Testimonium and surfaced in the dossier rather than silently
    treating page completion as an act-level read.
    """
    act_id = act["act_id"]
    entries = [
        entry
        for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "act-attachment" and entry["subject_id"] == act_id
    ]
    if not entries:
        raise FatalAccounting(f"act {act_id} has no act-attachment record")
    records = [
        context.tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"])
        for entry in entries
    ]
    record = latest_attempt(records, f"act-attachment for {act_id}", operation="act-attachment")
    payload = record.get("payload")
    attachments = payload.get("attachments") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"act_key", "attempt_ordinal", "attachments"}
        or payload.get("act_key") != act["act_key"]
        or not isinstance(attachments, list)
    ):
        raise SchemaRefusal("an act-attachment record has no attachment list")
    configured = set(context.witness_chairs)
    chairs = [
        attachment.get("chair") if isinstance(attachment, dict) else None
        for attachment in attachments
    ]
    if len(chairs) != len(set(chairs)) or set(chairs) != configured:
        raise FatalAccounting(
            f"act {act_id} attachment chairs do not equal this run's configured witnesses"
        )
    page_witness_count = 0
    for attachment in attachments:
        if (
            not isinstance(attachment, dict)
            or set(attachment)
            != {
                "chair",
                "page_witness",
                "testimonium_ref",
                "attached",
                "content_health",
                "span",
            }
            or not isinstance(attachment.get("chair"), str)
            or not isinstance(attachment.get("page_witness"), bool)
            or not isinstance(attachment.get("attached"), bool)
            or not isinstance(attachment.get("content_health"), dict)
            or not isinstance(attachment.get("span"), dict)
        ):
            raise SchemaRefusal("an act-attachment record has a malformed attachment")
        chair = attachment["chair"]
        expected_page_witness = chair in set(context.fixture.get("page_witness_chairs", []))
        if attachment["page_witness"] != expected_page_witness:
            raise SchemaRefusal(
                f"act {act_id} attachment changes page-witness scope for chair {chair!r}"
            )
        reference = attachment.get("testimonium_ref")
        if attachment["page_witness"]:
            testimonium = context.tree.read_artifact_reference(
                reference,
                stage=ATTESTATORES,
                kind="page-testimonium",
                subject_id=act["page_id"],
            )
            page_payload = testimonium.get("payload")
            if (
                not isinstance(page_payload, dict)
                or page_payload.get("chair") != chair
                or page_payload.get("scope") != "page"
                or page_payload.get("page_ordinal") != act["page_ordinal"]
            ):
                raise SchemaRefusal(f"act {act_id} attachment points to the wrong page Testimonium")
            page_witness_count += 1
        else:
            testimonium = context.tree.read_artifact_reference(
                reference,
                stage=ATTESTATORES,
                kind="testimonium",
                subject_id=act_id,
            )
            if testimonium.get("payload", {}).get("chair") != chair:
                raise SchemaRefusal(
                    f"act {act_id} attachment points to another chair's Testimonium"
                )
    return {
        "reference": context.artifact_ref(ATTESTATORES, "act-attachment", record["artifact_id"]),
        # A blinded dossier may show that page evidence exists, but not the
        # chair names embedded in its retained attachment artifact.
        "page_witness_count": page_witness_count,
    }


def verify_region(context, region: dict) -> dict:
    """Prove the region handed over is the region the reference describes.

    Three checks, because each catches a different lie: the digest catches bytes
    that changed under a sealed reference, decoding catches a reference pointing at
    something that is not an image, and the dimensions catch a crop that does not
    match the transform it claims to be.
    """
    try:
        return verify_exemplar_crop_lineage(context.tree, context.run, region)
    except ContractError as error:
        raise SchemaRefusal("a Designator region does not trace to its Exemplar page") from error


# Moved into dossier.py so the dossier derives witness coverage from the same
# testimonia it carries; re-exported here for its callers and tests.
witnessed_region_ids = dossier_module.witnessed_region_ids


def declared_reading_failure(context, act_key: str) -> str | None:
    """The non-completed outcome this scenario declares for an act, if any.

    The fixture is the authority on what a scenario does, exactly as it is for a
    witness failure. A reading that did not succeed still carries whatever text
    it managed, which is the shape that matters: it is what let a `truncated`
    reading be established as the one text.
    """
    for row in context.fixture.get("reading_failure", []):
        if row["scenario"] == context.scenario and row["act_key"] == act_key:
            return row["outcome"]
    return None


def perlector_chair(context) -> ChairIdentity | AbsentChair:
    """The Perlector chair, resolved by name. Never another chair, never a base."""
    resolved = context.registry.resolve(PERLECTOR_CHAIR)
    if not isinstance(resolved, (ChairIdentity, AbsentChair)):
        raise ContractError("Perlector resolution returned neither an identity nor an absence")
    return resolved


def preflight_testimonia_denominator(context, acts: list[dict]) -> None:
    """Validate every requested act's witness denominator before any Perlectio writes.

    A Perlectio is immutable, so one published over a short denominator cannot
    be corrected: restoring the missing witness changes the bytes under the same
    reading identity, and the run can no longer resume normally. Checking the
    whole requested set first is what stops a malformed act discovered late from
    leaving an unfixable reading behind it.
    """
    for act in acts:
        if act["outcome"] == "held":
            continue
        _, proposal_regions = act_regions(context, act["act_id"])
        testimonia_of(context, act["act_id"], proposal_regions)


def provenance_for(context, resolved: ChairIdentity | AbsentChair, *, attempted: bool) -> dict:
    """Project one Perlector outcome's immutable provenance.

    A record for a reading that never happened — a held act, or an absent chair —
    names what would have read and stops there. Manufacturing a receipt for it
    would be a serving moment nobody observed.

    A reading that *did* happen re-verifies the configured snapshot first, at the
    moment it is produced rather than once at run creation: GOVERNANCE 6 is about
    identity when the reading was made, and a receipt captured at serve time and
    copied forward is the weaker claim spec 02 names and refuses.
    """
    regime = {
        # Tyrel's 2026-07-30 ruling: witness identity travels under a run-level
        # toggle, and every Perlectio records the regime it ran under, because a
        # reading's provenance includes what its reader was shown.
        "witness_regime": context.witness_context,
        "adapter_revision": context.adapter_revision,
    }
    if isinstance(resolved, AbsentChair):
        return {
            "chair": resolved.role,
            "chair_state": "absent",
            "absence": resolved.to_record(),
            "resolved_identity": None,
            "resolved_revision": None,
            "receipt_ref": None,
            **regime,
        }
    return {
        "chair": resolved.role,
        "chair_state": "configured",
        "resolved_identity": resolved.to_record(),
        "resolved_revision": {
            "kind": resolved.receipt_revision_kind,
            "value": resolved.receipt_revision,
        },
        "receipt_ref": (
            context.write_serving_receipt(resolved, fixture_serving_details(resolved))
            if attempted
            else None
        ),
        **regime,
    }


def _page_renders_for(context, bases: list[dict]) -> list[dict]:
    """One downscaled page render per distinct page an act's regions touch.

    A continuation act spans two pages; nuda and the primed pass see both,
    because sight is never what nuda withholds.
    """
    by_page: dict[str, dict] = {}
    for basis in bases:
        page_id = basis["source_page_id"]
        if page_id not in by_page:
            by_page[page_id] = dossier_module.build_page_render(
                context,
                source_page_id=page_id,
                source_page_ordinal=basis["source_page_ordinal"],
            )
    return list(by_page.values())


def _whole_act_gap(testimonia: list[dict], references: dict[str, dict]) -> list[dict]:
    """The one gap an unreadable act carries: zero-width, evidence attached,
    never a character inside `text` (the establishment firewall,
    `annotations.py`).

    Each variant travels with the digest-checked reference to the Testimonium
    that reported it, so a displayed "⟨illegible — witnesses agree: …⟩" leads
    back to the sealed record rather than to a chair name somebody would then
    have to go looking for.
    """
    evidence = [
        {
            "chair": record["payload"]["chair"],
            "testimonium_id": record["artifact_id"],
            "reference": references[record["artifact_id"]],
            "variant": record["payload"]["reported"],
        }
        for record in testimonia
        # Presence and type, never truthiness: a genuinely-empty witness
        # reported "" and that report is the strongest corroboration a
        # whole-act gap can carry.
        if record["outcome"] in WITNESS_READING_OUTCOMES
        and isinstance(record["payload"].get("reported"), str)
    ]
    return [{"position": "whole-act", "start": 0, "end": 0, "witness_evidence": evidence}]


def _region_pixels(bases: list[dict]) -> int:
    return sum(
        basis["transform"]["bounds"]["w"] * basis["transform"]["bounds"]["h"] for basis in bases
    )


def _reading_image_inputs(context, bases: list[dict], page_renders: list[dict]) -> list[dict]:
    """Every image blob the reading actually saw, including page context.

    The dossier records these references as payload facts; the envelope input
    list independently binds them as direct evidence.  Omitting page context
    there would let a Perlectio claim it saw a render that its own provenance
    never retained as an input.
    """
    inputs = {
        reference["relative_path"]: reference
        for reference in (context.input_ref(basis["image_path"]) for basis in bases)
    }
    for render in page_renders:
        source = render.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("relative_path"), str):
            raise SchemaRefusal("a Perlector page render carries no sealed source-page reference")
        for reference in (
            context.input_ref(source["relative_path"]),
            context.input_ref(render["image_path"]),
        ):
            inputs[reference["relative_path"]] = reference
    return sorted(inputs.values(), key=lambda item: (item["relative_path"], item["sha256"]))


# Every field an established-reading Perlectio payload carries. Closed, and
# checked before publication rather than described in the handoff and hoped
# for: spec 08's schema test asks that a Perlectio "missing identity, missing
# dissent, missing regime record, or with annotation spans outside text bounds"
# be refused, and three of those four are fields that would simply be absent
# rather than wrong. An absent field is the failure mode a per-field type check
# never sees.
_PERLECTIO_FIELDS: Final = frozenset(
    {
        "act_key",
        "attempt_ordinal",
        "text",
        "basis",
        "dossier",
        "prompt",
        "dissent",
        "truncation",
        "uncertain_spans",
        "gaps",
        "provenance",
    }
)

# The same, for the instrument record. It carries no `basis` -- a nuda reading
# has no witness basis to record, which is the whole point of it -- and it does
# carry the sampling design it was drawn under.
_LECTIO_NUDA_FIELDS: Final = frozenset(
    {
        "act_key",
        "attempt_ordinal",
        "text",
        "dossier",
        "prompt",
        "sampling",
        "dissent",
        "truncation",
        "uncertain_spans",
        "gaps",
        "provenance",
    }
)

# The two shapes a Perlectio takes when nothing was read: a held act (the
# proposal never completed) and an explicitly absent chair. Both predate the
# closed-schema guard the other two record kinds already carry -- D-6: the
# record that says *why nothing was read* deserves the same guard as the one
# that says what was, so a future edit that quietly drops `provenance` from
# either shape is refused rather than published.
_NOT_RUN_HELD_FIELDS: Final = frozenset({"act_key", "attempt_ordinal", "reason", "provenance"})
_NOT_RUN_ABSENT_FIELDS: Final = frozenset(
    {"act_key", "attempt_ordinal", "reason", "basis", "dissent", "provenance"}
)


def validate_not_run_payload(payload: dict, *, fields: frozenset) -> None:
    """Refuse a not-run Perlectio missing part of the record it claims.

    Deliberately just the closed field set, not full record validation: a
    not-run payload has no text, no dossier and no completed reading to check
    against each other -- there is nothing else about it to validate.
    """
    missing = sorted(fields - set(payload))
    unexpected = sorted(set(payload) - fields)
    if missing or unexpected:
        raise SchemaRefusal(
            f"a Perlector not-run payload is not its closed schema: missing {missing}, "
            f"unexpected {unexpected}"
        )


def validate_reading_payload(
    payload: dict,
    *,
    outcome: str,
    fields: frozenset,
    run_id: str | None = None,
    config_digest: str | None = None,
) -> None:
    """Refuse a reading payload that is missing part of the record it claims.

    Producer-local and deliberately so: `validate_serving_provenance` already
    refuses a wrong-schema provenance wherever a Perlectio is *consumed*, and
    this is the matching check at the moment one is written, so a defect
    surfaces where it was introduced rather than one stage later.
    """
    missing = sorted(fields - set(payload))
    unexpected = sorted(set(payload) - fields)
    if missing or unexpected:
        raise SchemaRefusal(
            f"a Perlector reading payload is not its closed schema: missing {missing}, "
            f"unexpected {unexpected}"
        )
    if outcome == "read" and (not isinstance(payload["text"], str) or not payload["text"].strip()):
        raise SchemaRefusal("a completed reading cannot establish an empty text")
    # The caller's field set decides which record shape this is: a Perlectio
    # payload smuggling `basis: None` must refuse as a missing witness basis,
    # never slip down the nuda branch with every witness gone from the record.
    is_nuda = "basis" not in fields
    basis = payload.get("basis")
    if is_nuda:
        if payload["dissent"] != []:
            raise SchemaRefusal("a Lectio nuda cannot dissent from testimony it was not shown")
    elif not isinstance(basis, dict) or not isinstance(basis.get("testimonia"), list):
        raise SchemaRefusal("a Perlectio carries no Testimonium basis for its dissent record")
    else:
        validate_dissent(
            payload["dissent"], text=payload["text"], basis_testimonia=basis["testimonia"]
        )
    provenance = payload["provenance"]
    if (
        not isinstance(provenance, dict)
        or provenance.get("witness_regime") not in WITNESS_CONTEXT_REGIMES
    ):
        raise SchemaRefusal(
            "a Perlector reading records no witness regime; a reading's provenance includes "
            "what its reader was shown"
        )
    if provenance.get("chair_state") == "configured" and not provenance.get("resolved_identity"):
        raise SchemaRefusal(
            "a Perlector reading by a configured chair records no resolved identity"
        )
    reading_dossier = payload["dossier"]
    dossier_fields = {
        "act_id",
        "act_key",
        "witness_regime",
        "regions",
        "page_renders",
        "testimonia",
        "dossier_digest",
    }
    if not isinstance(reading_dossier, dict) or set(reading_dossier) not in (
        dossier_fields,
        dossier_fields | {"act_attachment"},
    ):
        raise SchemaRefusal("a Perlector reading carries no closed dossier record")
    if reading_dossier["act_key"] != payload["act_key"]:
        raise SchemaRefusal("a Perlector reading disagrees with its dossier's act key")
    if reading_dossier["witness_regime"] != provenance["witness_regime"]:
        raise SchemaRefusal("a Perlector reading disagrees with its dossier's witness regime")
    dossier_body = {key: value for key, value in reading_dossier.items() if key != "dossier_digest"}
    if reading_dossier["dossier_digest"] != digest_of(dossier_body):
        raise SchemaRefusal("a Perlector dossier digest does not match the dossier it seals")
    dossier_module.assert_no_order_bearing_field(dossier_body)
    if "act_attachment" in reading_dossier:
        attachment = reading_dossier["act_attachment"]
        if (
            not isinstance(attachment, dict)
            or set(attachment) != {"reference", "page_witness_count"}
            or not isinstance(attachment["reference"], dict)
            or not isinstance(attachment["page_witness_count"], int)
        ):
            raise SchemaRefusal("a Perlector dossier has malformed act-attachment evidence")
    dossier_testimonia = reading_dossier["testimonia"]
    if not isinstance(dossier_testimonia, list):
        raise SchemaRefusal("a Perlector dossier has no Testimonium list")
    if is_nuda:
        if dossier_testimonia:
            raise SchemaRefusal("a Lectio nuda dossier cannot carry Testimonia")
    elif len(dossier_testimonia) != len(basis["testimonia"]):
        raise SchemaRefusal(
            "a Perlector dossier does not account for exactly its Testimonium basis"
        )
    else:
        # Label-for-label, not merely count-for-count: a sealed reading must not
        # show one witness set in the prompt while its basis, dissent and export
        # record another. Named labels are the chairs themselves; blinded labels
        # are re-derived from the run's own identity when the caller supplies it
        # (the production publish path always does — the bare form exists for
        # schema tests that assert other refusals).
        basis_chairs = {row["chair"] for row in basis["testimonia"] if isinstance(row, dict)}
        dossier_labels = {
            row["witness_label"] for row in dossier_testimonia if isinstance(row, dict)
        }
        regime_name = reading_dossier["witness_regime"]
        if regime_name == regime.NAMED:
            expected_labels = basis_chairs
        elif run_id is not None and config_digest is not None:
            expected_labels = {
                regime.pseudonym_for(chair, run_id=run_id, config_digest=config_digest)
                for chair in basis_chairs
            }
        else:
            expected_labels = None
        if expected_labels is not None and dossier_labels != expected_labels:
            raise SchemaRefusal(
                "a Perlector dossier's witness labels do not match its Testimonium basis"
            )
    prompt_record = payload["prompt"]
    identity_record = provenance.get("resolved_identity")
    if not isinstance(identity_record, dict):
        raise SchemaRefusal("a Perlector prompt has no resolved chair identity")
    try:
        identity = ChairIdentity(**identity_record)
    except TypeError as error:
        raise SchemaRefusal("a Perlector prompt carries a malformed chair identity") from error
    if prompt_record != prompts.prompt_evidence(identity, reading_dossier):
        raise SchemaRefusal(
            "a Perlector prompt record does not reproduce from its resolved chair and dossier"
        )
    if (
        not isinstance(payload["truncation"], dict)
        or payload["truncation"].get("classification") not in truncation.CLASSIFICATIONS
    ):
        raise SchemaRefusal(
            "a Perlector reading carries no truncation classification; truncation is detected "
            "by an instrument, never assumed"
        )
    if outcome == "read" and payload["truncation"]["classification"] != truncation.COMPLETE:
        raise SchemaRefusal(
            "a truncated or unknown attempt cannot carry the completed outcome 'read'"
        )
    if outcome == "truncated" and payload["truncation"]["classification"] == truncation.COMPLETE:
        raise SchemaRefusal(
            "a Perlectio with outcome 'truncated' cannot carry a 'complete' truncation "
            "classification; outcome == 'truncated' means 'not established complete', and "
            "the truncation field is where that is confirmed or held unknown, never "
            "contradicted"
        )
    annotations.validate_annotations(payload, outcome=outcome)


def _resolve_outcome(*, declared_failure: str | None, truncation_record: dict, text: str) -> str:
    """One place the outcome is decided, so the precedence is stated once:
    a scenario's declared engine behaviour outranks the computed detector
    (it stands in for a real engine's own report), the detector outranks a
    default `read`, and an empty reading is never silently `read`."""
    if declared_failure is not None:
        return declared_failure
    if truncation.holds_as_failure(truncation_record["classification"]):
        return "truncated"
    if not text.strip():
        # The same emptiness rubric the publish-time schema uses: a reader
        # returning "\n" for one act is an unreadable act, not a reason to
        # abort the stage and lose the parish's other readings.
        return "no-readable-text"
    return "read"


def _reconciled_truncation(*, declared_failure: str | None, truncation_record: dict) -> dict:
    """Keep the published truncation record from contradicting a declared failure.

    A declared failure stands in for a real engine's own report that a reading
    did not complete, and nothing about *why* need show in the text's shape --
    so the three computed signals can land on `complete` under an outcome that
    means "not established complete" (HANDOFF.md, verbatim). The signals stay
    exactly as measured; only the classification is raised to `unknown`, because
    the instrument did not itself confirm a cutoff. Something outside it did.
    """
    if (
        declared_failure == "truncated"
        and truncation_record["classification"] == truncation.COMPLETE
    ):
        return {**truncation_record, "classification": truncation.UNKNOWN}
    return truncation_record


def _publish_lectio_nuda(
    context,
    *,
    act: dict,
    act_id: str,
    ordinal: int,
    chair: ChairIdentity,
    bases: list[dict],
    page_renders: list[dict],
    reader: FixtureReader,
    region_pixels: int,
    witness_context_table: dict,
) -> None:
    """Publish the unprimed instrument reading.

    Carries no testimonia at all (spec 08). Written under its own artifact
    `kind` (`lectio-nuda`, never `perlectio`) and its own attempt operation
    (`lectio-nuda`, never `perlegere`) -- structurally outside every consumer
    that queries `kind == "perlectio"`, and outside the identity space
    `latest_attempt`'s `attempt_id` derivation binds to `perlegere` readings,
    so nothing can ever conflate a nuda attempt with an establishing one.
    """
    nuda_dossier, delivered_pixels = dossier_module.build_reader_dossier(
        context,
        act_id=act_id,
        act_key=act["act_key"],
        regions=bases,
        testimonia=[],
        regime=context.witness_context,
        page_renders=page_renders,
        witness_context=witness_context_table,
    )
    prompt = prompts.prompt_evidence(chair, nuda_dossier)
    result = reader.read(nuda_dossier, primed=False, delivered_pixels=delivered_pixels)
    truncation_record = truncation.classify(
        result["text"], region_pixels=region_pixels, stop_reason=result["stop_reason"]
    )
    outcome = _resolve_outcome(
        declared_failure=None, truncation_record=truncation_record, text=result["text"]
    )
    # One emptiness rubric everywhere: a whitespace-only reading resolved to
    # `no-readable-text` publishes the empty text that outcome's schema
    # requires — the whitespace was never established ink.
    nuda_text = "" if outcome == "no-readable-text" else result["text"]
    payload = {
        "act_key": act["act_key"],
        "attempt_ordinal": ordinal,
        "text": nuda_text,
        "dossier": nuda_dossier,
        "prompt": prompt,
        "sampling": nuda.sampling_design(
            nuda_per_mille=context.nuda_per_mille,
            approval_ref=context.nuda_approval_ref,
        ),
        "dissent": [],
        "truncation": truncation_record,
        "uncertain_spans": [],
        "gaps": _whole_act_gap([], {}) if outcome == "no-readable-text" else [],
        "provenance": provenance_for(context, chair, attempted=True),
    }
    validate_reading_payload(payload, outcome=outcome, fields=_LECTIO_NUDA_FIELDS)
    context.publish(
        kind=nuda.LECTIO_NUDA_KIND,
        subject_id=act_id,
        outcome=outcome,
        attempt=attempt_id(act_id, "lectio-nuda", ordinal),
        inputs=_reading_image_inputs(context, bases, page_renders),
        payload=payload,
    )


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run through the explicitly supplied chair implementation.

    Production passes the default registry. The test-only injection is a
    dependency seam, not a runtime choice among models or chairs.
    """
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = open_context(args, PERLECTOR, registry_factory=registry_factory)
    reader = FixtureReader(context.fixture, context.scenario)
    witness_context_table = dossier_module.load_witness_context(
        Path(context.witness_context_config_path)
    )

    # A recovery re-reads only the acts that were recovered. Re-reading the rest
    # would add an attempt nobody requested to an act nothing happened to, and an
    # attempt tally that counts work no stage asked for stops meaning anything.
    wanted = [act for act in expected_acts(context) if args.act in (None, act["act_id"])]
    if args.act and not wanted:
        raise ContractError(f"asked to read {args.act}, which the proposal seal does not name")
    preflight_testimonia_denominator(context, wanted)

    read = 0
    acknowledged = 0
    chair = perlector_chair(context)
    for act in wanted:
        act_id = act["act_id"]
        if act["outcome"] == "held":
            # A held act's proposal is incomplete — its page or its continuation
            # never sealed. Reading whatever regions exist would produce a
            # reading of part of an act, and it reads through to the end:
            # truncation is a failure, not an output. The act is acknowledged
            # with an explicit unresolved outcome rather than skipped, because
            # a unit this stage never mentions is invariant #10's imbalance.
            payload = {
                "act_key": act["act_key"],
                "attempt_ordinal": 1,
                "reason": (
                    "the Designator held this act; an incomplete proposal is "
                    "not read, because a reading of part of an act would be a "
                    "truncation delivered as an output"
                ),
                "provenance": provenance_for(context, chair, attempted=False),
            }
            validate_not_run_payload(payload, fields=_NOT_RUN_HELD_FIELDS)
            context.publish(
                kind="perlectio",
                subject_id=act_id,
                outcome="not-run",
                attempt=attempt_id(act_id, "perlegere", 1),
                payload=payload,
            )
            acknowledged += 1
            continue

        ordinal = _next_attempt(context, act_id)
        if isinstance(chair, AbsentChair):
            # No chair to read with. Every act still gets an explicit record
            # naming the absence: a stage that simply produced nothing would
            # leave the Recensor to infer a gap it cannot see.
            payload = {
                "act_key": act["act_key"],
                "attempt_ordinal": ordinal,
                "reason": f"the Perlector chair is explicitly absent: {chair.reason}",
                "basis": {"regions": [], "testimonia": []},
                "dissent": [],
                "provenance": provenance_for(context, chair, attempted=False),
            }
            validate_not_run_payload(payload, fields=_NOT_RUN_ABSENT_FIELDS)
            context.publish(
                kind="perlectio",
                subject_id=act_id,
                outcome="not-run",
                attempt=attempt_id(act_id, "perlegere", ordinal),
                payload=payload,
            )
            acknowledged += 1
            continue

        regions, proposal_regions = act_regions(context, act_id)

        # Every region of the act is verified and read, including a continuation
        # on the next page: an act that ran over the page break and was read only
        # up to the fold would be truncated, which is a failure and not an output.
        bases = [verify_region(context, region) for region in regions]
        testimonia = testimonia_of(context, act_id, proposal_regions)
        attachment_view = act_attachment_view(context, act)

        # Which regions any witness actually saw. Ink uncovered by a recovery
        # recrop was never shown to a witness, and saying so is the difference
        # between a gap in the record and a gap nobody can see. It changes nothing
        # about the reading — the Perlector reads the ink either way.
        witnessed = witnessed_region_ids(testimonia)
        for basis in bases:
            basis["witness_covered"] = basis["region_id"] in witnessed

        region_pixels = _region_pixels(bases)
        page_renders = _page_renders_for(context, bases)

        # The unprimed instrument, sampled by the run's own predeclared design
        # (`nuda_per_mille`, fixed before the run). Ahead of the establishing
        # pass in this loop, but the two are independent artifacts; nothing
        # about the nuda reading feeds the primed one or vice versa.
        if nuda.is_nuda_sampled(
            act_id, run_id=context.tree.run_id, nuda_per_mille=context.nuda_per_mille
        ):
            _publish_lectio_nuda(
                context,
                act=act,
                act_id=act_id,
                ordinal=ordinal,
                chair=chair,
                bases=bases,
                page_renders=page_renders,
                reader=reader,
                region_pixels=region_pixels,
                witness_context_table=witness_context_table,
            )

        # The establishing read: every testimonium in the dossier, verbatim.
        primed_dossier, delivered_pixels = dossier_module.build_reader_dossier(
            context,
            act_id=act_id,
            act_key=act["act_key"],
            regions=bases,
            testimonia=testimonia,
            regime=context.witness_context,
            page_renders=page_renders,
            witness_context=witness_context_table,
            act_attachment=attachment_view,
        )
        # Built before the reader is called, from the dossier the reader is
        # about to be shown, so what is recorded is the prompt this reading was
        # produced through rather than one reconstructed afterwards.
        prompt = prompts.prompt_evidence(chair, primed_dossier)
        result = reader.read(primed_dossier, primed=True, delivered_pixels=delivered_pixels)

        # The scenario's declared engine behaviour stands in for a real
        # engine's own report and, when present, decides `reading` and
        # `outcome` together: a declared `no-readable-text` means nothing was
        # read, not that the fixture's normal act text happens to still apply.
        declared_failure = declared_reading_failure(context, act["act_key"])
        reading = "" if declared_failure == "no-readable-text" else result["text"]
        truncation_record = _reconciled_truncation(
            declared_failure=declared_failure,
            truncation_record=truncation.classify(
                reading, region_pixels=region_pixels, stop_reason=result["stop_reason"]
            ),
        )
        outcome = _resolve_outcome(
            declared_failure=declared_failure, truncation_record=truncation_record, text=reading
        )
        if outcome == "no-readable-text":
            # See the nuda publish path: whitespace resolved as unreadable is
            # published as the empty text its schema requires.
            reading = ""
        testimonium_references = {
            record["artifact_id"]: context.artifact_ref(
                ATTESTATORES, "testimonium", record["artifact_id"]
            )
            for record in testimonia
        }
        gaps = (
            _whole_act_gap(testimonia, testimonium_references)
            if outcome == "no-readable-text"
            else []
        )

        provenance = provenance_for(context, chair, attempted=True)
        payload = {
            "act_key": act["act_key"],
            "attempt_ordinal": ordinal,
            "text": reading,
            "basis": {
                "regions": bases,
                "testimonia": [
                    {
                        "chair": record["payload"]["chair"],
                        "artifact_id": record["artifact_id"],
                        "outcome": record["outcome"],
                        "reference": testimonium_references[record["artifact_id"]],
                    }
                    for record in testimonia
                ],
            },
            "dossier": primed_dossier,
            "prompt": prompt,
            "dissent": dissent_against(reading, testimonia),
            "truncation": truncation_record,
            "uncertain_spans": [],
            "gaps": gaps,
            "provenance": provenance,
        }
        validate_reading_payload(
            payload,
            outcome=outcome,
            fields=_PERLECTIO_FIELDS,
            run_id=context.tree.run_id,
            config_digest=context.config_digest,
        )
        context.publish(
            kind="perlectio",
            subject_id=act_id,
            outcome=outcome,
            attempt=attempt_id(act_id, "perlegere", ordinal),
            inputs=_reading_image_inputs(context, bases, page_renders)
            + list(testimonium_references.values())
            + [attachment_view["reference"]],
            payload=payload,
        )
        read += 1

    if read == 0 and acknowledged == 0:
        raise ContractError("the Perlector read no act and acknowledged no held act")

    context.finish()
    return EXIT_COMPLETE


def _next_attempt(context, act_id: str) -> int:
    """Which reading attempt this is, derived from the act rather than from history.

    Counting existing Perlectiones and adding one would make the answer depend on
    how many times the stage had been *invoked*, so a rerun of an unchanged run
    would append a reading nobody asked for and the Archetypus would then point at
    it. The reading attempt is instead a function of the act's own state: one
    reading of the proposal, and one more for each recovery region cut since. A
    rerun that changed nothing therefore recomputes the same ordinal, produces the
    same bytes, and is reused rather than rewritten.
    """
    recoveries = 0
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] == "region" and entry["subject_id"] == act_id:
            record = context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            if record["payload"]["origin"] == "recovery":
                recoveries += 1
    return recoveries + 1


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
