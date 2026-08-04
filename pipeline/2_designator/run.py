"""Designator: marks out the acts and cuts the crops. It establishes no text.

Two things it owns that nothing else may touch. **Crops** — the Recensor may
*request* a replacement region, but only this stage cuts one, so a crop always has
one author. And the **proposal seal**: an immutable record of every act this run
expects, emitted once, which becomes the downstream expected-act authority. Without
it, a later stage could only ask "did I account for the acts I happen to have seen"
rather than "did I account for the acts that were found", and an act lost between
stages would leave no hole to notice.

Every seal entry carries this stage's outcome for the act: `proposed` when it was
fully marked out, `held` when it could not be — its page unsealed, or a declared
continuation whose page never sealed — with a `hold` artifact recording why. An
act this stage cannot mark out is a unit it still accounts for; before the hold
existed, such an act was skipped, sealed nowhere, and the run reported complete
over its absence.

Regions are append-only per act, and each carries an `origin` saying what kind of
region it is: a **proposal** region is part of what was originally marked out — the
first crop, and a continuation on the next page, both — while a **recovery** region
is a recrop cut later at the Recensor's request. The distinction is load-bearing:
witnesses read the proposal regions, so ink that only a recovery uncovered was
never shown to a witness, and the Perlectio records that rather than papering over
it. A bare sequence number cannot express this, and reading one as an attempt count
made the witnesses skip the far side of a page break.

Act identity is bound to the *original proposal* and so is unchanged by any recrop;
the region identity is bound to the transform and so must change. ARCHITECTURE's
first invariant therefore falls out of the derivation rather than being maintained
by hand.

    python pipeline/2_designator/run.py --run-root <dir> --run-id <id>
    python pipeline/2_designator/run.py ... --operation recover --act <act_id>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.chairs.models import AbsentChair, ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.approval import (  # noqa: E402
    APPROVAL_GATED_REAL_INGRESS,
    parse_data_gate_ingress_record,
)
from common.contracts.canonical import verify_self_hash  # noqa: E402
from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.identities import artifact_id, attempt_id, region_id  # noqa: E402
from common.contracts.stages import DESIGNATOR, EXEMPLAR  # noqa: E402
from common.exemplar_boundary import verify_sealed_page_pixels  # noqa: E402
from common.imaging import crop_png  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402
from common.stage import (  # noqa: E402
    DESIGNATOR_CHAIR,
    EXIT_COMPLETE,
    StageContext,
    act_bounds,
    act_identity,
    adapter_recipe_for,
    continuation_for,
    fixture_serving_details,
    open_context,
    page_identity,
    run_stage,
    stage_parser,
)


def structure_provenance(context) -> dict:
    """Verify and record the exact chair that produced structural proposals.

    The walking skeleton derives deterministic crops, but it still exercises the
    structure-chair seam. An absent or unverifiable Designator is a refusal, never
    a cue to synthesize structure through a different role.
    """
    resolved = context.registry.resolve(DESIGNATOR_CHAIR)
    if isinstance(resolved, AbsentChair):
        raise ContractError(
            f"the Designator chair is explicitly absent: {resolved.reason}; "
            "no other chair may mark out structure"
        )
    if not isinstance(resolved, ChairIdentity):
        raise ContractError("Designator resolution returned neither an identity nor an absence")
    receipt_ref = context.write_serving_receipt(resolved, fixture_serving_details(resolved))
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


def page_records(context) -> dict[int, dict]:
    """Every page outcome the Exemplar recorded — sealed and refused — by ordinal.

    Read from the Exemplar's artifacts rather than from the fixture, so a page the
    door refused is a page this stage genuinely does not see as ink. The refused
    records still matter here: they are the evidence a hold rests on.
    """
    manifest = context.tree.build_manifest(EXEMPLAR)
    source_rows = _source_rows(context.run)
    records = {}
    entries_by_ordinal = {}
    for entry in manifest["artifacts"]:
        if entry["kind"] != "page":
            continue
        record = context.tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        ordinal = record["payload"].get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise ContractError("an Exemplar page carries no integer ordinal")
        if ordinal in records:
            raise ContractError(f"the Exemplar carries more than one outcome for ordinal {ordinal}")
        records[ordinal] = {
            "record": record,
            "relative_path": entry["relative_path"],
        }
        entries_by_ordinal[ordinal] = entry
    _verify_exemplar_boundary(context, manifest, source_rows, records, entries_by_ordinal)
    return records


def _source_rows(run: dict) -> dict[int, dict]:
    """The submitted denominator, retaining each filename for a useful failure."""
    rows = run.get("source_manifest")
    if not isinstance(rows, list) or not rows:
        raise ContractError("run.json carries no source manifest for the Exemplar boundary")
    sources: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("run.json carries a source-manifest row that is not an object")
        ordinal = row.get("ordinal")
        path = row.get("relative_path")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise ContractError("run.json carries a source-manifest row without an integer ordinal")
        if ordinal in sources:
            raise ContractError(f"run.json repeats source ordinal {ordinal}")
        if not isinstance(path, str) or not path:
            raise ContractError(f"run.json source ordinal {ordinal} carries no filename")
        sources[ordinal] = row
    return sources


def _verify_exemplar_boundary(context, manifest, sources, records, entries_by_ordinal) -> None:
    """Reconcile the immutable Exemplar census before the Designator reads pixels."""
    expected_ordinals = set(sources)
    observed_ordinals = set(records)
    missing = sorted(expected_ordinals - observed_ordinals)
    if missing:
        names = [sources[ordinal]["relative_path"] for ordinal in missing]
        raise ContractError(
            "the Exemplar boundary lost submitted page(s) before the Designator: "
            f"{names}. Every filename ledger entry must have one Exemplar outcome"
        )
    extra = sorted(observed_ordinals - expected_ordinals)
    if extra:
        raise ContractError(
            f"the Exemplar boundary names page ordinal(s) {extra} absent from run.json's "
            "submitted source manifest"
        )

    seals = [entry for entry in manifest["artifacts"] if entry["kind"] == "seal"]
    expected_id = artifact_id(EXEMPLAR, "seal", "corpus-seal")
    if len(seals) != 1 or seals[0]["artifact_id"] != expected_id:
        raise ContractError("the Exemplar boundary carries no single derived corpus seal")
    seal = context.tree.read_artifact(EXEMPLAR, "seal", expected_id)
    if seal["run_id"] != context.tree.run_id or seal["stage"] != EXEMPLAR:
        raise ContractError("the Exemplar corpus seal belongs to a different run or stage")
    payload = seal["payload"]
    if set(payload) != {"page_count", "pages", "self_hash"} or not verify_self_hash(payload):
        raise ContractError("the Exemplar corpus seal does not carry a valid self-hashed census")
    if payload["page_count"] != len(sources) or not isinstance(payload["pages"], list):
        raise ContractError(
            "the Exemplar corpus seal count does not reconcile with submitted sources"
        )

    census: dict[int, dict] = {}
    for row in payload["pages"]:
        if not isinstance(row, dict) or not isinstance(row.get("ordinal"), int):
            raise ContractError("the Exemplar corpus seal carries a page row without an ordinal")
        ordinal = row["ordinal"]
        if ordinal in census:
            raise ContractError(f"the Exemplar corpus seal names ordinal {ordinal} more than once")
        census[ordinal] = row
    if set(census) != expected_ordinals:
        missing = sorted(expected_ordinals - set(census))
        names = [sources[ordinal]["relative_path"] for ordinal in missing]
        raise ContractError(
            "the Exemplar corpus seal dropped filename-ledger page(s) before the Designator: "
            f"{names}"
        )

    expected_refs = {
        (entry["relative_path"], entry["sha256"]) for entry in entries_by_ordinal.values()
    }
    actual_refs = {
        (reference.get("relative_path"), reference.get("sha256")) for reference in seal["inputs"]
    }
    if actual_refs != expected_refs or len(seal["inputs"]) != len(expected_refs):
        raise ContractError(
            "the Exemplar corpus seal inputs do not name every page outcome exactly once"
        )

    for ordinal, source in sources.items():
        record = records[ordinal]["record"]
        page = census[ordinal]
        outcome = record["outcome"]
        expected = {
            "ordinal": ordinal,
            "declared_path": source["relative_path"],
            "declared_sha256": source["sha256"],
            "page_id": record["subject_id"] if outcome == "sealed" else None,
            "outcome": outcome,
            "source_sha256": record["payload"].get("source_sha256")
            if outcome == "sealed"
            else None,
        }
        for field in ("bytes", "ledger_sha256", "container_page_index"):
            if field in source and source[field] is not None:
                expected[{"bytes": "declared_bytes"}.get(field, field)] = source[field]
        if page != expected:
            raise ContractError(
                f"the Exemplar corpus seal row for {source['relative_path']!r} does not "
                "match the page outcome and submitted filename ledger"
            )
        if outcome == "sealed":
            # The census proves the page is accounted for; this additionally
            # proves the actual pixels we are about to crop are the immutable
            # Door blob the Exemplar sealed.  Never read and re-hash a changed
            # blob into a fresh region record.
            verify_sealed_page_pixels(context.tree, context.run, source, record)


def sealed_pages(records: dict[int, dict]) -> dict[int, dict]:
    """The sealed subset, by ordinal, each value the page artifact itself."""
    return {
        ordinal: entry["record"]
        for ordinal, entry in records.items()
        if entry["record"]["outcome"] == "sealed"
    }


def cut_region(context, act, page_record, bounds, ordinal, page_ordinal, origin):
    """Cut one region of one act and publish it.

    `origin` separates two things that a bare sequence number runs together. A
    **proposal** region is part of what the Designator originally marked out —
    including a continuation on the next page, which is a second region of the
    same act rather than a later attempt at it. A **recovery** region is a recrop
    cut later at the Recensor's request. Witnesses read the proposal regions;
    ink a recovery uncovers was never shown to them. Numbering alone cannot say
    which is which, and reading it as an attempt count made this stage skip the
    far side of a page break.
    """
    act_id = act_identity(context.fixture, act)
    provenance = structure_provenance(context)
    image_path = page_record["payload"]["image_path"]
    page_bytes = context.tree.read_bytes(image_path)

    transform = {
        "operation": "crop",
        "source_page_ordinal": page_ordinal,
        "source_page_id": page_record["subject_id"],
        "bounds": bounds,
    }
    crop_bytes = crop_png(page_bytes, bounds)
    digest, stored = context.tree.put_blob(DESIGNATOR, crop_bytes)

    context.publish(
        kind="region",
        subject_id=act_id,
        outcome="proposed",
        attempt=attempt_id(act_id, "crop", ordinal),
        inputs=[context.input_ref(image_path)],
        payload={
            "region_id": region_id(act_id, transform),
            "act_key": act["key"],
            "attempt_ordinal": ordinal,
            "origin": origin,
            "transform": transform,
            "image_path": stored.relative_path,
            "image_sha256": digest,
            "provenance": provenance,
        },
    )
    return act_id


def hold_act(context, act, act_id: str, unsealed_ordinal: int, records, reason: str) -> None:
    """Publish the artifact that says why this act could not be marked out.

    The hold is a real record, never a skipped loop iteration: before it existed,
    an act on an unsealed page was written nowhere at all, the proposal seal came
    up short, and the Armarium's conservation check reconciled perfectly against
    a record of the loss's absence. The hold references the Exemplar's own page
    outcome as its evidence, so the refusal it rests on is one digest-checked
    hop away.
    """
    entry = records.get(unsealed_ordinal)
    if entry is None:
        raise ContractError(
            f"act {act['key']} needs page {unsealed_ordinal}, and the Exemplar "
            "recorded no outcome for it at all — a page in neither the sealed nor "
            "the refused set is invariant #10's imbalance, not a page to skip"
        )
    context.publish(
        kind="hold",
        subject_id=act_id,
        outcome="held",
        inputs=[context.input_ref(entry["relative_path"])],
        payload={
            "act_key": act["key"],
            "unsealed_page_ordinal": unsealed_ordinal,
            "reason": reason,
        },
    )


def initial_pass(context) -> int:
    records = page_records(context)
    pages = sealed_pages(records)
    if not pages:
        raise ContractError("the Designator found no sealed page to mark out")

    expected = []
    for act in context.fixture["act"]:
        page_ordinal = act["page_ordinal"]
        act_id = act_identity(context.fixture, act)
        continuation = continuation_for(context.fixture, act["key"])
        continuation_cut = False

        if page_ordinal not in pages:
            # The act's own page never sealed. It cannot be marked out, and it
            # may not disappear either: it is held, with the reason on record,
            # and no region of it — not even a sealed continuation — is cut. An
            # orphan far-side crop would be evidence of an act nothing accounts
            # for.
            outcome = "held"
            hold_act(
                context,
                act,
                act_id,
                page_ordinal,
                records,
                f"page {page_ordinal} was not sealed, so the act could not be marked out",
            )
        else:
            cut_region(
                context, act, pages[page_ordinal], act_bounds(act), 1, page_ordinal, "proposal"
            )

            # An act that runs over the page break gets a second region of the
            # SAME act. A continuation that became its own act would quietly turn
            # one entry into two and break identity where it is hardest to see.
            if continuation and continuation["page_ordinal"] in pages:
                cut_region(
                    context,
                    act,
                    pages[continuation["page_ordinal"]],
                    {key: continuation[key] for key in ("x", "y", "w", "h")},
                    2,
                    continuation["page_ordinal"],
                    "proposal",
                )
                continuation_cut = True

            if continuation and not continuation_cut:
                # The near side is sealed ink and stays cut as evidence for the
                # reviewer, but the act as marked out is incomplete: delivering
                # a reading of the near side alone would be a truncation wearing
                # a complete act's name.
                outcome = "held"
                hold_act(
                    context,
                    act,
                    act_id,
                    continuation["page_ordinal"],
                    records,
                    f"the act continues onto page {continuation['page_ordinal']}, "
                    "which was not sealed, so its continuation could not be cut",
                )
            else:
                outcome = "proposed"

        expected.append(
            {
                "act_id": act_id,
                "act_key": act["key"],
                "page_id": page_identity(context.fixture, page_ordinal),
                "page_ordinal": page_ordinal,
                # Derived from the regions actually cut, never from the fixture
                # declaration: a seal that claims a continuation nothing holds is
                # how an act gets read on one side of a page break and delivered
                # as a complete reading.
                "has_continuation": continuation_cut,
                "outcome": outcome,
            }
        )

    if not expected:
        raise ContractError("no act was marked out on any sealed page")

    # The seal, emitted once and never rewritten: this is what downstream stages
    # reconcile against, so "every expected act has exactly one outcome" is a
    # question with an answer.
    context.publish(
        kind="proposal-seal",
        subject_id="proposal-seal",
        outcome="proposed",
        payload={
            "expected_acts": expected,
            "count": len(expected),
            "provenance": structure_provenance(context),
        },
    )
    return len(expected)


def recovery_pass(context, act_id: str) -> int:
    """Cut one replacement region for one act, at the Recensor's request.

    The Recensor asked; the Designator cuts. Keeping the ownership straight is
    what stops the recovery loop from growing a second author for crops.
    """
    seal = context.tree.read_artifact(DESIGNATOR, "proposal-seal", _seal_artifact_id(context))
    match = [item for item in seal["payload"]["expected_acts"] if item["act_id"] == act_id]
    if not match:
        raise ContractError(f"recovery asked for {act_id}, which the proposal seal does not name")
    if match[0].get("outcome") != "proposed":
        raise ContractError(
            f"recovery asked for {act_id}, which the seal holds as "
            f"{match[0].get('outcome')!r}; a held act is terminal and may not be "
            "recropped back to life"
        )

    act = next(item for item in context.fixture["act"] if item["key"] == match[0]["act_key"])
    recovery = [row for row in context.fixture.get("recovery", []) if row["act_key"] == act["key"]]
    if not recovery:
        raise ContractError(f"the fixture declares no recovery region for act {act['key']}")

    pages = sealed_pages(page_records(context))
    bounds = {key: recovery[0][key] for key in ("x", "y", "w", "h")}
    ordinal = _next_region_ordinal(context, act_id)
    cut_region(
        context, act, pages[act["page_ordinal"]], bounds, ordinal, act["page_ordinal"], "recovery"
    )
    return 1


def _seal_artifact_id(context) -> str:
    from common.contracts.identities import artifact_id

    return artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None)


def _next_region_ordinal(context, act_id: str) -> int:
    ordinals = [record["payload"]["attempt_ordinal"] for record in _regions_of(context, act_id)]
    return max(ordinals, default=0) + 1


def _regions_of(context, act_id: str) -> list[dict]:
    records = []
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] == "region" and entry["subject_id"] == act_id:
            records.append(context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"]))
    return records


def _open(args, registry_factory) -> tuple[object, bool]:
    """Open either a fixture stage context or the honest real-input boundary.

    System 03 owns the Exemplar-to-Designator reconciliation, but it does not own
    a real structural-proposal model.  A real run therefore reaches that check and
    then stops; it must not fabricate fixture acts, successful no-op work, or a
    synthetic hold that could make an unproposed corpus look exported.
    """
    tree = RunTree(Path(args.run_root), args.run_id)
    run = tree.read_run()
    mode, _policy_hash, _reference = parse_data_gate_ingress_record(run.get("ingress"))
    if mode != APPROVAL_GATED_REAL_INGRESS:
        return open_context(args, DESIGNATOR, registry_factory=registry_factory), False
    return (
        StageContext(
            tree=tree,
            run=run,
            fixture={},
            scenario="real-submission",
            stage=DESIGNATOR,
            adapter_revision=adapter_recipe_for(run, DESIGNATOR),
            args=args,
            registry=None,
        ),
        True,
    )


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run through the explicitly supplied structure-chair implementation."""
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context, real_input = _open(args, registry_factory)

    if real_input:
        page_records(context)
        raise ContractError(
            "the Exemplar-to-Designator filename-ledger boundary reconciled, but real "
            "structural proposal/model work is outside System 03; no proposals or holds "
            "were fabricated"
        )

    if args.operation == "recover":
        if not args.act:
            raise ContractError("a recovery operation must name the act it is recovering")
        recovery_pass(context, args.act)
    else:
        initial_pass(context)

    context.finish()
    return EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
