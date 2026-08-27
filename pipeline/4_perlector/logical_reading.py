"""Build the run partition and atomic presentation before any Perlector call.

The partition covers the complete proposal seal even when recovery selects one
act. This read loop has no capture-alignment input, so it refuses any resolved
physical-act group before publishing a reading rather than reading its local
members separately.
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
    """Exclude submitted captures without immutable Exemplar page lineage."""
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
    """Seal the complete readable local-to-logical denominator once per run.

    Held acts have hold evidence rather than a sealed source page, so they
    cannot supply the source digest required by a partition row.
    """
    held = sorted(act["act_id"] for act in expected if act["outcome"] == "held")
    if held:
        # The partition count is necessarily narrower than the proposal seal when
        # held acts have no source page; name the omitted rows instead of hiding it.
        print(
            "non-fatal finding: physical-act partition excludes "
            f"{len(held)} held act(s) from local_expected_count: {', '.join(held)}",
            file=sys.stderr,
        )
    local_acts = [_local_act_row(context, act) for act in expected if act["outcome"] != "held"]
    if not local_acts:
        # The partition schema refuses an empty denominator, and the read loop
        # short-circuits every held act before dereferencing these sentinels.
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
    """Stop before publication if the local-act loop cannot honor the denominator.

    Findings make the partition non-total. Physical-act groups would be visited
    once per local member, producing capture-local Perlectiones for one logical
    act even when the group currently has only one proposed member.
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

    A local act absent from ``local_to_logical`` cannot default to a singleton;
    doing so would bypass the partition's unresolved-cluster findings.
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
    """Group every touched page by source capture without asserting visibility.

    ``visibility_evidence_refs`` retains page-render provenance only; no
    downstream consumer treats it as a completed occlusion survey.
    """
    renders_by_page: dict[str, dict[str, Any]] = {}
    for render in page_renders:
        page_id = render["source_page_id"]
        if page_id in renders_by_page and renders_by_page[page_id] != render:
            raise SchemaRefusal(
                f"act {act['act_id']!r} has two different page renders for {page_id!r}"
            )
        renders_by_page[page_id] = render
    pages_by_capture: dict[str, list[str]] = {}
    for page_id in sorted({basis["source_page_id"] for basis in bases}):
        pages_by_capture.setdefault(_source_sha256_of_page(context, page_id), []).append(page_id)
    views = []
    required = sorted(pages_by_capture)
    for source_sha256 in required:
        capture_pages = pages_by_capture[source_sha256]
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
                # Preserve one slot per region even when different crops encode
                # identically, and retain the upstream digest for delivery recheck.
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
