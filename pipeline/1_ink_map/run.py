"""Ink map: measure every sealed page before the Designator proposes acts.

One ``ink-map`` record is written for every sealed Exemplar page, including a
page with no ink and therefore no possible proposal.  The record is bounded
evidence: ``unclaimed-edge-ink`` names an edge signal without holding anything;
Unit 14 owns that explicit hold outcome.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.approval import REAL_INGRESS, parse_ingress_record  # noqa: E402
from common.contracts.canonical import digest_bytes  # noqa: E402
from common.contracts.errors import FatalAccounting  # noqa: E402
from common.contracts.stages import EXEMPLAR, INK_MAP  # noqa: E402
from common.exemplar_boundary import verify_sealed_page_pixels  # noqa: E402
from common.residual_ink import page_edge_ink, page_residual_ink  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    StageContext,
    adapter_recipe_for,
    open_context,
    run_stage,
    stage_parser,
    verify_predecessor_seal,
)


def sealed_pages(context):
    """Every sealed page once, with its Exemplar boundary proved.

    The census is structural and is completed before a single record is
    published, so a run whose Exemplar cannot be reconciled writes nothing at
    all. The page pixels themselves are read one at a time in `main`, through
    `measured_page_bytes`.
    """
    pages = []
    seen_ordinals = set()
    for entry in context.tree.build_manifest(EXEMPLAR)["artifacts"]:
        if entry["kind"] != "page":
            continue
        page = context.tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        if page["outcome"] != "sealed":
            continue
        ordinal = page.get("payload", {}).get("ordinal")
        sources = [row for row in context.run["source_manifest"] if row.get("ordinal") == ordinal]
        if not isinstance(ordinal, int) or len(sources) != 1 or ordinal in seen_ordinals:
            raise FatalAccounting(
                "the Exemplar does not name exactly one sealed source per ink map"
            )
        seen_ordinals.add(ordinal)
        verify_sealed_page_pixels(context.tree, context.run, sources[0], page)
        if not isinstance(page["payload"].get("image_path"), str):
            raise FatalAccounting(f"sealed page {ordinal} has no image bytes for the ink map")
        pages.append((ordinal, page, entry["relative_path"]))
    if not pages:
        raise FatalAccounting("the ink map received no sealed Exemplar pages")
    return sorted(pages, key=lambda row: row[0])


def measured_page_bytes(tree, ordinal: int, page: dict) -> bytes:
    """The page pixels this stage measures, digested as the bytes it measures.

    `verify_sealed_page_pixels` proves the sealed blob against a read of its
    own. This is a second read, and measuring a second read on the strength of
    the first one's proof records a finding derived from pixels nobody checked
    -- a metric that was not measured passing as one (GOVERNANCE 10). The
    Recensor already states exactly this guard for exactly this measure at the
    late boundary (`pipeline/5_recensor/run.py::page_coverage_findings`); the
    early map is the pre-proposal evidence baseline and is the last place in
    the pipeline that may be the weaker of the two.

    Read one page at a time rather than accumulated with the census, because
    this stage measures EVERY sealed page of a shard and a shard runs to 1,000
    of them (`config/corpus_frame.toml`). Holding every page's bytes at once
    made peak memory the size of the shard's pixels; the Recensor reads inside
    its own loop for the same reason.
    """
    payload = page["payload"]
    image_bytes = tree.read_bytes(payload["image_path"])
    if digest_bytes(image_bytes) != payload.get("source_sha256"):
        raise FatalAccounting(
            f"the sealed Exemplar page {ordinal} the ink map read does not match the "
            "pixel digest its own page record verified"
        )
    return image_bytes


def artifact_finding(finding: dict) -> dict:
    """Make the shared measure's ratio canonical without changing its measure."""
    recorded = dict(finding)
    fraction = recorded.pop("fraction_outside")
    if not isinstance(fraction, float):
        raise FatalAccounting("residual-ink measure returned a non-float fraction")
    recorded["fraction_outside_per_million"] = int(round(fraction * 1_000_000))
    return recorded


def _open(args, registry_factory) -> StageContext:
    """Keep real ingress on its sealed authority, as the Exemplar does."""
    tree = RunTree(Path(args.run_root), args.run_id)
    run = tree.read_run()
    if parse_ingress_record(run.get("ingress")) != REAL_INGRESS:
        return open_context(args, INK_MAP, registry_factory=registry_factory)
    verify_predecessor_seal(tree, INK_MAP)
    return StageContext(
        tree=tree,
        run=run,
        fixture={},
        scenario="real-submission",
        stage=INK_MAP,
        adapter_revision=adapter_recipe_for(run, INK_MAP),
        args=args,
        registry=None,
    )


def main(registry_factory=ChairRegistry.from_toml) -> int:
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = _open(args, registry_factory)
    for ordinal, page, page_path in sealed_pages(context):
        image_bytes = measured_page_bytes(context.tree, ordinal, page)
        # Empty coverage is intentional: this is the pre-proposal denominator.
        ink_map = page_residual_ink(image_bytes, covered=[])
        edge = page_edge_ink(image_bytes)
        context.publish(
            kind="ink-map",
            subject_id=page["subject_id"],
            outcome="unclaimed-edge-ink" if edge["flagged"] else "mapped",
            inputs=[context.input_ref(page_path)],
            payload={
                "page_ordinal": ordinal,
                "ink": artifact_finding(ink_map),
                "edge": artifact_finding(edge),
            },
        )
    context.seal_boundary()
    context.finish()
    return EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
