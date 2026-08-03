"""Perlector: reads the ink, with the testimonia as fallible clues.

The fake here proves wiring and nothing else — its text comes from the fixture, so
it demonstrates exactly zero about reading. What it *does* prove is the shape of
the record, and the shape is where GOVERNANCE 3 either holds or quietly fails:

  It verifies the region it was handed.   The bytes are read, their digest checked
                                          against the sealed reference, and decoded
                                          to confirm the image is the size the
                                          transform claims. A reader that never
                                          looked at the image cannot be said to
                                          have read the ink.
  It records its basis.                   The region it read, and every testimonium
                                          it saw, by reference.
  It never counts witnesses.              No branch anywhere in this file reads how
                                          many seats agreed. The dissent record is
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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.chairs.models import AbsentChair, ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.canonical import digest_bytes  # noqa: E402
from common.contracts.errors import ContractError, SchemaRefusal  # noqa: E402
from common.contracts.identities import attempt_id  # noqa: E402
from common.contracts.stages import ATTESTATORES, DESIGNATOR, PERLECTOR  # noqa: E402
from common.imaging import dimensions  # noqa: E402
from common.stage import (  # noqa: E402
    ATTEMPTED_WITNESS_OUTCOMES,
    EXIT_COMPLETE,
    expected_acts,
    fixture_serving_details,
    open_context,
    run_stage,
    stage_parser,
    validate_serving_provenance,
)


def regions_of(context, act_id: str) -> list[dict]:
    records = []
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] == "region" and entry["subject_id"] == act_id:
            records.append(context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"]))
    return sorted(records, key=lambda record: record["payload"]["attempt_ordinal"])


def testimonia_of(context, act_id: str) -> list[dict]:
    records = []
    for entry in context.tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] == "testimonium" and entry["subject_id"] == act_id:
            record = context.tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
            validate_serving_provenance(
                context,
                record["payload"]["provenance"],
                producer_stage=ATTESTATORES,
                require_receipt=record["outcome"] in ATTEMPTED_WITNESS_OUTCOMES,
            )
            records.append(record)
    return sorted(records, key=lambda record: record["payload"]["seat"])


def verify_region(context, region: dict) -> dict:
    """Prove the region handed over is the region the reference describes.

    Three checks, because each catches a different lie: the digest catches bytes
    that changed under a sealed reference, decoding catches a reference pointing at
    something that is not an image, and the dimensions catch a crop that does not
    match the transform it claims to be.
    """
    payload = region["payload"]
    data = context.tree.read_bytes(payload["image_path"])

    actual = digest_bytes(data)
    if actual != payload["image_sha256"]:
        raise SchemaRefusal(
            f"region {payload['region_id']} has digest {actual}, not the "
            f"{payload['image_sha256']} its reference recorded"
        )

    width, height = dimensions(data)
    bounds = payload["transform"]["bounds"]
    if (width, height) != (bounds["w"], bounds["h"]):
        raise SchemaRefusal(
            f"region {payload['region_id']} is {width}x{height}, but its transform "
            f"claims {bounds['w']}x{bounds['h']}"
        )
    return {
        "region_id": payload["region_id"],
        "image_path": payload["image_path"],
        "image_sha256": actual,
        "verified_dimensions": {"w": width, "h": height},
    }


def dissent_against(reading: str, testimonia: list[dict]) -> list[dict]:
    """Where the reading departed from each witness that actually reported.

    Computed after the reading is fixed. A seat that failed or never ran has no
    opinion to depart from, and is recorded as having none rather than as agreeing
    — silence is not assent.
    """
    rows = []
    for record in testimonia:
        chair = record["payload"]["seat"]
        if record["outcome"] != "read":
            rows.append({"seat": chair, "compared": False, "reason": record["outcome"]})
            continue
        reported = record["payload"]["reported"]
        rows.append({"seat": chair, "compared": True, "departed": reported != reading})
    return rows


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
    """The Perlector seat, resolved by name. Never another seat, never a base."""
    resolved = context.registry.resolve(PERLECTOR)
    if not isinstance(resolved, (ChairIdentity, AbsentChair)):
        raise ContractError("Perlector resolution returned neither an identity nor an absence")
    return resolved


def provenance_for(context, resolved: ChairIdentity | AbsentChair, *, attempted: bool) -> dict:
    """Project one Perlector outcome's immutable provenance.

    A record for a reading that never happened — a held act, or an absent seat —
    names what would have read and stops there. Manufacturing a receipt for it
    would be a serving moment nobody observed.

    A reading that *did* happen re-verifies the configured snapshot first, at the
    moment it is produced rather than once at run creation: GOVERNANCE 6 is about
    identity when the reading was made, and a receipt captured at serve time and
    copied forward is the weaker claim spec 02 names and refuses.
    """
    regime = {
        # Tyrel's 2026-07-30 ruling: witness identity travels under a run-level
        # toggle, and every Perlectio records the regime it ran under so a later
        # reader knows what it was shown.
        "witness_regime": "named",
        "adapter_revision": context.adapter_revision,
    }
    if isinstance(resolved, AbsentChair):
        return {
            "seat": resolved.role,
            "seat_state": "absent",
            "absence": resolved.to_record(),
            "resolved_identity": None,
            "resolved_revision": None,
            "receipt_ref": None,
            **regime,
        }
    return {
        "seat": resolved.role,
        "seat_state": "configured",
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


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run through the explicitly supplied seat implementation.

    Production passes the default registry. The test-only injection is a
    dependency seam, not a runtime choice among models or seats.
    """
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = open_context(args, PERLECTOR, registry_factory=registry_factory)
    texts = {act["key"]: act["text"] for act in context.fixture["act"]}

    # A recovery re-reads only the acts that were recovered. Re-reading the rest
    # would add an attempt nobody requested to an act nothing happened to, and an
    # attempt tally that counts work no stage asked for stops meaning anything.
    wanted = [act for act in expected_acts(context) if args.act in (None, act["act_id"])]
    if args.act and not wanted:
        raise ContractError(f"asked to read {args.act}, which the proposal seal does not name")

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
            context.publish(
                kind="perlectio",
                subject_id=act_id,
                outcome="not-run",
                attempt=attempt_id(act_id, "perlegere", 1),
                payload={
                    "act_key": act["act_key"],
                    "attempt_ordinal": 1,
                    "reason": (
                        "the Designator held this act; an incomplete proposal is "
                        "not read, because a reading of part of an act would be a "
                        "truncation delivered as an output"
                    ),
                    "provenance": provenance_for(context, chair, attempted=False),
                },
            )
            acknowledged += 1
            continue

        ordinal = _next_attempt(context, act_id)
        if isinstance(chair, AbsentChair):
            # No seat to read with. Every act still gets an explicit record
            # naming the absence: a stage that simply produced nothing would
            # leave the Recensor to infer a gap it cannot see.
            context.publish(
                kind="perlectio",
                subject_id=act_id,
                outcome="not-run",
                attempt=attempt_id(act_id, "perlegere", ordinal),
                payload={
                    "act_key": act["act_key"],
                    "attempt_ordinal": ordinal,
                    "reason": f"the Perlector seat is explicitly absent: {chair.reason}",
                    "basis": {"regions": [], "testimonia": []},
                    "dissent": [],
                    "provenance": provenance_for(context, chair, attempted=False),
                },
            )
            acknowledged += 1
            continue

        regions = regions_of(context, act_id)
        if not regions:
            raise ContractError(f"act {act_id} reached the Perlector with no region")

        # Every region of the act is verified and read, including a continuation
        # on the next page: an act that ran over the page break and was read only
        # up to the fold would be truncated, which is a failure and not an output.
        bases = [verify_region(context, region) for region in regions]
        testimonia = testimonia_of(context, act_id)
        reading = texts[act["act_key"]]

        # Which regions any witness actually saw. Ink uncovered by a recovery
        # recrop was never shown to a witness, and saying so is the difference
        # between a gap in the record and a gap nobody can see. It changes nothing
        # about the reading — the Perlector reads the ink either way.
        witnessed = {
            reference["region_id"]
            for record in testimonia
            if record["outcome"] == "read"
            for reference in record["payload"]["regions"]
        }
        for basis in bases:
            basis["witness_covered"] = basis["region_id"] in witnessed

        # A real reading attempt, so the pinned snapshot is re-verified here, at
        # the moment this reading is produced, and its receipt written.
        provenance = provenance_for(context, chair, attempted=True)
        context.publish(
            kind="perlectio",
            subject_id=act_id,
            outcome=declared_reading_failure(context, act["act_key"]) or "read",
            attempt=attempt_id(act_id, "perlegere", ordinal),
            inputs=[context.input_ref(basis["image_path"]) for basis in bases],
            payload={
                "act_key": act["act_key"],
                "attempt_ordinal": ordinal,
                "text": reading,
                "basis": {
                    "regions": bases,
                    "testimonia": [
                        {
                            "seat": record["payload"]["seat"],
                            "artifact_id": record["artifact_id"],
                            "outcome": record["outcome"],
                        }
                        for record in testimonia
                    ],
                },
                "dissent": dissent_against(reading, testimonia),
                "provenance": provenance,
            },
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
