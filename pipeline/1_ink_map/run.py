"""Ink map: measure every sealed page before the Designator proposes acts.

One ``ink-map`` record is written for every sealed Exemplar page, including a
page on which this measure finds no ink; proposals do not exist yet. The record
is bounded evidence: ``unclaimed-edge-ink`` names an edge signal without holding
anything; the Armarium decides the hold by re-measuring the retained runs
against the Designator's verified crops.

This stage infers each page's paper value through
``common.background``, the same inference and the same sealed
``[grouping.background]`` policy the Designator's structure pass runs under, and
proves the bytes it read against the run's own ``designator-grouping`` seal. The
page's raw histogram mode is not used: on a photographed opening that mode is
the bezel, which would map every such page as carrying approximately no ink at
all. A page is published as ``ink-not-measurable`` -- present in the census,
with its refusal named and no counts -- rather than as a page that measured
clean, for either of two reasons: the shared inference refuses its paper, or
the paper it infers is too dark for this audit's own contrast
(``MINIMUM_CONTRAST_BELOW_BACKGROUND``) to leave any level to count as ink. The
second happens on pages the Designator measures, since its own floor margin is
smaller.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from common.background import (  # noqa: E402
    BackgroundInferenceRefusal,
    load_background_config,
    resolve_background_policy,
)
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.approval import parse_ingress_record  # noqa: E402
from common.contracts.canonical import is_plain_int  # noqa: E402
from common.contracts.errors import FatalAccounting  # noqa: E402
from common.contracts.stages import EXEMPLAR, INK_MAP  # noqa: E402
from common.exemplar_boundary import sealed_page_bytes, verify_sealed_page_pixels  # noqa: E402
from common.imaging import UnsettledReadingPolicy, grayscale_rows  # noqa: E402
from common.residual_ink import (  # noqa: E402
    INK_NOT_MEASURABLE,
    ink_map_page,
    load_coverage_audit_config,
    resolve_coverage_audit_policy,
)
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    open_stage_context,
    run_stage,
    stage_parser,
)

DESCRIPTION = "Ink map: measure every sealed page before the Designator proposes acts."


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
        if not is_plain_int(ordinal):
            raise FatalAccounting(
                "the ink map refuses the Exemplar census: a sealed page has no integer "
                "ordinal, so it cannot be matched to one submitted source; no ink-map "
                "record was written"
            )
        sources = [row for row in context.run["source_manifest"] if row.get("ordinal") == ordinal]
        if len(sources) != 1:
            raise FatalAccounting(
                f"the ink map refuses the Exemplar census: sealed page ordinal {ordinal} "
                f"matches {len(sources)} submitted source rows, not exactly one; no "
                "ink-map record was written"
            )
        if ordinal in seen_ordinals:
            raise FatalAccounting(
                f"the ink map refuses the Exemplar census: more than one sealed page names "
                f"ordinal {ordinal}; one source cannot receive two ink-map records, and no "
                "record was written"
            )
        seen_ordinals.add(ordinal)
        verify_sealed_page_pixels(context.tree, context.run, sources[0], page)
        pages.append((ordinal, page, entry["relative_path"]))
    if not pages:
        raise FatalAccounting(
            "the ink map refuses the Exemplar census: it contains no sealed pages; "
            "a completed map cannot be published over an empty measured denominator"
        )
    return sorted(pages, key=lambda row: row[0])


def measured_page_bytes(tree, ordinal: int, page: dict) -> bytes:
    """The page pixels this stage measures, digested as the bytes it measures.

    Read one page at a time rather than accumulated with the census, because
    this stage measures EVERY sealed page of a shard and a shard runs to 1,000
    of them (`config/corpus_frame.toml`): holding every page's bytes at once
    would make peak memory the size of the shard's pixels. The Recensor reads
    inside its own loop for the same reason.
    """
    return sealed_page_bytes(
        tree, page, what=f"the ink map (page {ordinal})", refusal=FatalAccounting
    )


def artifact_finding(finding: dict) -> dict:
    """Make the shared measure's ratio canonical without changing its measure."""
    recorded = dict(finding)
    # Defaulted rather than indexed: a measure that stops emitting the key at
    # all is a worse contract break than one emitting a string, so both cases
    # arrive here as the same named refusal below rather than one raising a
    # bare KeyError.
    fraction = recorded.pop("fraction_outside", None)
    if not isinstance(fraction, float):
        raise FatalAccounting(
            "residual-ink measure returned no float `fraction_outside`; the ink map "
            "cannot record a ratio it was not given"
        )
    recorded["fraction_outside_per_million"] = int(round(fraction * 1_000_000))
    return recorded


def main(registry_factory=ChairRegistry.from_toml) -> int:
    args = stage_parser(DESCRIPTION).parse_args()
    # Both ingress routes open through the shared constructor, which keeps every
    # direct-entry guard -- register drift, the sealed snapshot, the Exemplar's
    # completion seal, the run-level cap -- on the real route as well.
    context = open_stage_context(args, INK_MAP, registry_factory=registry_factory)
    # The shared constructor's route test treats an absent `ingress` key as
    # synthetic by design, so it decides the route without refusing a run that
    # simply never recorded one. This stage does not branch on the route -- it
    # maps every sealed page the same way either way -- so nothing else here
    # would catch that gap. Re-parse for the refusal effect alone: a run whose
    # ingress evidence is not a closed fixture-or-real record must still stop
    # here.
    parse_ingress_record(context.run.get("ingress"))
    # The same file the Designator loads and the same digest the run sealed at
    # binding time. Read once for the whole run: the policy is per-page only in
    # its two band widths, which `resolve_background_policy` derives from each
    # page's own dimensions below.
    background_config = load_background_config(context.args.designator_grouping_config)
    context.require_sealed_config("designator-grouping", background_config["config_sha256"])
    # `[coverage_audit]` out of the same file and under the same seal: the two
    # gates this stage's finding is decided by, and the page-spanning bound it
    # splits its counts on. Read once for the run for the same reason.
    coverage_config = load_coverage_audit_config(context.args.designator_grouping_config)
    context.require_sealed_config("designator-grouping", coverage_config["config_sha256"])
    for ordinal, page, page_path in sealed_pages(context):
        image_bytes = measured_page_bytes(context.tree, ordinal, page)
        try:
            # `measured_page_bytes` proves these bytes match the digest the
            # Exemplar sealed, not that the decoder can read them. Earlier pages
            # are already published, so either refusal says the map is
            # incomplete and names its own cause.
            width, height, rows = grayscale_rows(image_bytes)
        except UnsettledReadingPolicy as error:
            raise FatalAccounting(
                f"the ink map cannot measure sealed Exemplar page {ordinal}: the pipeline has "
                f"no settled policy for reading its pixels as grey ({error}); the bytes are "
                "intact, no boundary was sealed, and the records already published for "
                "earlier pages of this run are an incomplete map"
            ) from error
        except ValueError as error:
            raise FatalAccounting(
                f"the ink map cannot measure sealed Exemplar page {ordinal}: its own "
                f"digest-verified pixels do not decode ({error}); no boundary was "
                "sealed, and the records already published for earlier pages of this "
                "run are an incomplete map"
            ) from error
        policy = resolve_background_policy(background_config, width, height)
        audit_policy = resolve_coverage_audit_policy(coverage_config, width, height)
        try:
            measured = ink_map_page(
                width, height, rows, background_policy=policy, coverage_policy=audit_policy
            )
        except BackgroundInferenceRefusal as error:
            # Named, and still in the census: the Designator still cuts this
            # page, and the Armarium reconciles its page denominator against
            # the census. `mapped` with zero counts would be an audit passing by
            # construction.
            context.publish(
                kind="ink-map",
                subject_id=page["subject_id"],
                outcome=INK_NOT_MEASURABLE,
                inputs=[context.input_ref(page_path)],
                payload={
                    "page_ordinal": ordinal,
                    "ink_measurable": False,
                    "background_refusal": str(error),
                    "background_config_sha256": background_config["config_sha256"],
                },
            )
            continue
        edge = measured["edge"]
        context.publish(
            kind="ink-map",
            subject_id=page["subject_id"],
            outcome="unclaimed-edge-ink" if edge["flagged"] else "mapped",
            inputs=[context.input_ref(page_path)],
            payload={
                "page_ordinal": ordinal,
                "ink_measurable": True,
                # The paper value every count was taken under, where it came
                # from, this audit's contrast below it, and the digest of the
                # sealed policy that decided the inference.
                "background": {
                    **measured["background"],
                    "config_sha256": background_config["config_sha256"],
                },
                # `edge.total_ink_pixels` is the page's whole audited ink: the
                # pre-proposal denominator.
                "edge": artifact_finding(edge),
                # The page's audited runs, so later coverage decisions do not
                # re-measure it under another pixel predicate or spanning split.
                "edge_findings": measured["edge_findings"],
            },
        )
    context.seal_boundary()
    context.finish()
    return EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
