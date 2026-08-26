"""Unit 19B's first production caller of the Unit 19A partition builder.

``common/physical_act_partition.py``'s own module docstring records that no
production stage calls it: 19A left ``build_physical_act_partition`` and
``source_ledger_from_run`` with no run-tree producer, and a missing active
member was loud only in theory. This module is that caller for the Perlector.

No run this repository's fixtures produce registers a physical page yet, so
``capture_alignments`` is always empty here and every local act resolves as
``image-local-singleton`` (``logical_act_id == act_id``, consult §2.1). Nothing
here invents multi-capture correspondence; it wires the real 19A builder with
real run data so a genuinely clustered register -- once a discovery run has
appended one -- resolves through this exact call, unmodified. Consult §9.1
assigns the production partition artifact to the Designator; it is built here,
under the Perlector's own stage, because 19A did not wire a Designator-side
producer and that wiring is outside this slice's charge. This is recorded as a
deviation from the consult's final ownership, not a silent relocation: the
artifact this module publishes is self-contained (register digest, proposal
seal reference, and every local act, all independently re-verifiable) and
moving its producer to the Designator later changes nothing it asserts.
"""

from __future__ import annotations

import sys
from typing import Any

from common.contracts.canonical import canonical_bytes
from common.contracts.errors import SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.stages import DESIGNATOR, EXEMPLAR, PERLECTOR
from common.corpus_register import read_snapshot
from common.cross_capture_autopsia import build_autopsia_from_run
from common.physical_act_partition import build_physical_act_partition, source_ledger_from_run


def _source_sha256_of_page(context, page_id: str) -> str:
    """The submitted capture digest a sealed Exemplar page derives from."""
    page = context.tree.read_artifact(EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", page_id))
    digest = page.get("payload", {}).get("source_sha256")
    if not isinstance(digest, str) or not digest:
        raise SchemaRefusal(f"Exemplar page {page_id!r} carries no source_sha256")
    return digest


def _verified_source_ledger(context) -> set[str]:
    """Every submitted capture that has immutable Exemplar page lineage.

    ``run.json`` is the complete submission denominator, not proof that the
    Exemplar admitted a page from every row: a door refusal remains in that
    denominator deliberately. The physical-act partition needs the narrower
    fact "this run can present this capture". Each digest returned here comes
    from a page artifact re-read through ``RunTree.read_artifact``; a registered
    cluster member that was submitted but never admitted therefore remains
    absent and becomes ``cluster-member-absent`` before any Perlectio.
    """
    submitted = source_ledger_from_run(context.run)
    verified: set[str] = set()
    for entry in context.tree.build_manifest(EXEMPLAR)["artifacts"]:
        if entry["kind"] != "page":
            continue
        page = context.tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        digest = page.get("payload", {}).get("source_sha256")
        if not isinstance(digest, str) or not digest:
            raise SchemaRefusal(f"Exemplar page {entry['artifact_id']!r} carries no source_sha256")
        if digest not in submitted:
            raise SchemaRefusal(
                "physical-act partition: an Exemplar page names a capture absent from "
                "this run's sealed source manifest"
            )
        verified.add(digest)
    return verified


def _local_act_row(context, act: dict[str, Any]) -> dict[str, Any]:
    evidence = act.get("evidence") or []
    proposal_refs = sorted(
        {
            row["relative_path"]
            for row in evidence
            if isinstance(row, dict) and row.get("relative_path")
        }
    )
    if not proposal_refs:
        raise SchemaRefusal(
            f"expected-act {act['act_id']!r} carries no proposal evidence to derive a "
            "partition row from"
        )
    return {
        "act_id": act["act_id"],
        "act_key": act["act_key"],
        "page_id": act["page_id"],
        "page_ordinal": act["page_ordinal"],
        "source_sha256": _source_sha256_of_page(context, act["page_id"]),
        "proposal_refs": proposal_refs,
    }


def build_run_partition(
    context, expected: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """The run's total local-to-logical denominator, sealed as a content-addressed blob.

    Built once, from every expected act the proposal seal names with a region
    to partition -- never only the acts one recovery invocation asked to
    reread -- because the partition's own conservation check
    (``local_expected_count``) is over that complete set (consult §2.1.7). A
    ``held`` act's ``evidence`` names its Designator hold record, not a
    proposal region: its page may never have sealed at all (a door refusal
    upstream), and the main read loop already acknowledges it as ``not-run``
    before ever asking this partition for its logical identity (consult
    §4.7's held-act short circuit). Feeding it in here would ask
    ``_source_sha256_of_page`` to read a page that was never sealed, over an
    act nothing downstream needs a logical identity for. The register has no
    active physical-page members in any run this repository's fixtures
    produce, so ``capture_alignments`` is empty and every act resolves as a
    singleton; that path is exercised by 19A's own suite
    (``common/test_unit19_physical_act_partition.py``) and is not reproduced
    here.
    """
    held = sorted(act["act_id"] for act in expected if act["outcome"] == "held")
    if held:
        # Consult §2.1.7 defines `local_expected_count` as the number of local
        # proposal-seal rows, and this partition's is the number of *readable*
        # ones. The gap is forced -- a held act's page may never have sealed,
        # so it has no `source_sha256` to give -- but a denominator that is
        # quietly narrower than the one its contract names is exactly the
        # silence GOVERNANCE 2 forbids, so the run says which rows it left out
        # and how many. The consumer that turns this count into an act census
        # is Unit 19D's Armarium; it reconciles against the proposal seal, and
        # this line is what tells a reader of the run why the two differ.
        print(
            "non-fatal finding: physical-act partition excludes "
            f"{len(held)} held act(s) from local_expected_count: {', '.join(held)}",
            file=sys.stderr,
        )
    local_acts = [_local_act_row(context, act) for act in expected if act["outcome"] != "held"]
    if not local_acts:
        # Every expected act is held (a whole page refused at the door holds
        # every act it carried, consult §4.7). Nothing reaches
        # `logical_act_id_for`/`act_autopsia` in that run -- the read loop's
        # held branch continues before either is called -- so there is no
        # logical act here for a partition to denominate, and
        # `build_physical_act_partition` refuses an empty `local_acts` by
        # design (consult §2.1.7's "no local expected acts are not a
        # denominator"). Returning nothing is that same rule, not a bypass of
        # it: a caller that dereferences either return value with no acts to
        # read would fail loudly, exactly as it should.
        return None, None
    register_bytes = read_snapshot(context.tree, context.run)
    proposal_seal_ref = context.artifact_ref(
        DESIGNATOR,
        "proposal-seal",
        artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None),
    )
    partition = build_physical_act_partition(
        register=register_bytes,
        register_digest=context.run["register_digest"],
        proposal_seal_ref=proposal_seal_ref,
        local_acts=local_acts,
        capture_alignments=[],
        source_ledger=_verified_source_ledger(context),
    )
    _refuse_a_partition_this_loop_cannot_read(partition)
    digest, published = context.tree.put_blob(PERLECTOR, canonical_bytes(partition))
    return partition, {"relative_path": published.relative_path, "sha256": digest}


def _refuse_a_partition_this_loop_cannot_read(partition: dict[str, Any]) -> None:
    """Stop before any Perlectio when the read loop cannot honour the partition.

    Two distinct stops, both required before publication rather than at the act
    that trips over them, because ``run.py``'s loop publishes each act as it
    reads it: by the time an unresolvable act is reached, earlier acts already
    have Perlectiones, and consult §2.1 requires the run to stop *before*
    Perlector publication, not partway through it.

    1. Any finding at all. §2.1 gives the partition builder ``unresolved-``,
       ``retracted-`` and ``ambiguous-physical-act`` plus the two lineage codes
       precisely so an unresolved equivalence relation holds the run; a
       partition that names one and is then read anyway would publish acts over
       a denominator it has already said is not total.

    2. Any logical act this loop would read once per member. ``run.py`` walks
       *local* acts and builds one presentation from one local act's own
       regions, so a logical act with several members would get one
       establishing call and one Perlectio per member -- capture-local
       Perlectiones over one logical act, forbidden shapes §7.9 and §7.15 --
       and a single-member ``physical-act`` group would present only the
       captures that member happens to have proposals on rather than every
       capture the register declares for the component. Neither is reachable
       today (``capture_alignments`` is empty, so every act resolves as an
       ``image-local-singleton``), and 19B's charge is the one combined
       autopsia, not the cross-capture read loop 19C/19D restructure. What this
       refuses is the *silent* version of that gap: the first run whose
       register actually clusters would otherwise duplicate the act instead of
       saying it cannot yet read it.
    """
    if partition["findings"]:
        codes = sorted({f"{row['code']}:{row['act_id']}" for row in partition["findings"]})
        raise SchemaRefusal(
            "physical-act partition: this run's local-to-logical correspondence is not total, "
            f"so no act is read: {', '.join(codes)}"
        )
    unreadable = sorted(
        group["logical_act_id"]
        for group in partition["logical_acts"]
        if group["identity_scope"] != "image-local-singleton"
        or len(group["member_local_acts"]) != 1
    )
    if unreadable:
        raise SchemaRefusal(
            "physical-act partition: this run resolves a clustered logical act "
            f"({', '.join(unreadable)}), and the Perlector read loop still presents one local "
            "act's own regions at a time. Reading it here would publish one capture-local "
            "Perlectio per member instead of one combined autopsia (consult §7.9, §7.15); the "
            "cross-capture read loop is Unit 19C/19D's."
        )


def logical_act_id_for(partition: dict[str, Any], act_id: str) -> str:
    """This local act's resolved logical identity, or a named refusal.

    A local act absent from ``local_to_logical`` is not a singleton by
    default (consult §2.1.4): the partition builder already turned every
    unresolved clustered row into a finding and held, so reaching this call
    with a genuinely missing row means the caller skipped that hold.
    """
    for row in partition["local_to_logical"]:
        if row["act_id"] == act_id:
            return row["logical_act_id"]
    raise SchemaRefusal(
        f"{act_id!r} does not resolve to a logical act in this run's physical-act partition"
    )


def act_autopsia(
    context,
    *,
    logical_act_id: str,
    partition_ref: dict[str, str],
    act: dict[str, Any],
    bases: list[dict[str, Any]],
    page_renders: list[dict[str, Any]],
) -> dict[str, Any]:
    """The complete cross-capture presentation for one logical act's captures.

    One view per distinct capture the act's own regions touch -- exactly one
    for an ordinary act, and one per page for a far-page continuation, since a
    continuation's second page is its own submitted capture even though it
    shares the one local ``act_id``. ``visibility_evidence_refs`` names the
    same page render every view already presents: the complete act-level
    occlusion survey producer is Unit 19C's (consult §10.7), so this is
    provenance a later audit can look at, never a completed visibility claim
    read by anything downstream today.
    """
    renders_by_page: dict[str, dict[str, Any]] = {}
    for render in page_renders:
        page_id = render["source_page_id"]
        if page_id in renders_by_page and renders_by_page[page_id] != render:
            raise SchemaRefusal(
                f"act {act['act_id']!r} has two different page renders for {page_id!r}"
            )
        renders_by_page[page_id] = render
    pages = sorted({basis["source_page_id"] for basis in bases})
    pages_by_capture: dict[str, list[str]] = {}
    for page_id in pages:
        pages_by_capture.setdefault(_source_sha256_of_page(context, page_id), []).append(page_id)
    views = []
    required: list[str] = []
    for source_sha256 in sorted(pages_by_capture):
        capture_pages = pages_by_capture[source_sha256]
        required.append(source_sha256)
        capture_renders = []
        for page_id in capture_pages:
            render = renders_by_page.get(page_id)
            if render is None:
                raise SchemaRefusal(
                    f"act {act['act_id']!r} has a region on page {page_id!r} with no page render"
                )
            capture_renders.append(
                {"relative_path": render["image_path"], "sha256": render["image_sha256"]}
            )
        views.append(
            {
                "view_id": f"view_capture_{source_sha256}",
                "physical_page_id": f"ppg_local_capture_{source_sha256}",
                "source_sha256": source_sha256,
                "page_ids": capture_pages,
                "local_act_ids": [act["act_id"]],
                # Not deduplicated by content: a recovery crop and its
                # original proposal can cut different rectangles of an
                # ink-free page that encode to byte-identical PNGs, and
                # `dossier.regions`/the delivered pixel list stay one row per
                # region regardless (consult §3.1) -- collapsing the pair
                # here would under-deliver a region the reader is still owed
                # its own image slot for.
                # The digest the Designator sealed on the region, not one
                # re-derived from whatever is on disk now: `input_ref` hashes
                # the bytes it finds, so a reference built that way agrees with
                # the file by construction and can never disagree with it.
                # `atomic_delivered_pixels` checks a delivered image against
                # this reference, and that check is only worth making against
                # the upstream claim (`verify_region` has already proved the
                # two agree at this point in the loop).
                "region_refs": [
                    {"relative_path": basis["image_path"], "sha256": basis["image_sha256"]}
                    for basis in bases
                    if basis["source_page_id"] in capture_pages
                ],
                "page_render_refs": capture_renders,
                "alignment_ref": f"identity-alignment:{source_sha256}",
                "visibility_evidence_refs": capture_renders,
            }
        )
    return build_autopsia_from_run(
        run=context.run,
        logical_act_id=logical_act_id,
        partition_ref=partition_ref,
        required_capture_sha256s=required,
        views=views,
    )
