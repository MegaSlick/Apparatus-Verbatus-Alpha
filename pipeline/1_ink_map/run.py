"""Ink map: measure every sealed page before the Designator proposes acts.

One ``ink-map`` record is written for every sealed Exemplar page, including a
page on which this measure finds no ink; proposals do not exist yet. The record
is bounded evidence: ``unclaimed-edge-ink`` names an edge signal without holding
anything; Unit 14 owns that explicit hold outcome.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.canonical import digest_bytes  # noqa: E402
from common.contracts.errors import FatalAccounting  # noqa: E402
from common.contracts.stages import EXEMPLAR, INK_MAP  # noqa: E402
from common.exemplar_boundary import verify_sealed_page_pixels  # noqa: E402
from common.residual_ink import ink_runs, page_edge_ink, page_residual_ink  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    open_stage_context,
    run_stage,
    stage_parser,
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
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
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
        if not isinstance(page["payload"].get("image_path"), str):
            raise FatalAccounting(
                f"the ink map refuses sealed Exemplar page {ordinal}: it has no image path "
                "to measure; no ink-map record was written"
            )
        pages.append((ordinal, page, entry["relative_path"]))
    if not pages:
        raise FatalAccounting(
            "the ink map refuses the Exemplar census: it contains no sealed pages; "
            "a completed map cannot be published over an empty measured denominator"
        )
    return sorted(pages, key=lambda row: row[0])


def measured_page_bytes(tree, ordinal: int, page: dict) -> bytes:
    """The page pixels this stage measures, digested as the bytes it measures.

    `verify_sealed_page_pixels` proves a separate read of the sealed blob. This
    read must therefore verify its own digest or the measurement would describe
    unchecked pixels (GOVERNANCE 10).

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
    # Defaulted rather than indexed: a measure that stops emitting the key at
    # all is a worse contract break than one emitting a string, and it was the
    # one getting the worse report -- a bare KeyError where the string got a
    # named refusal. Both arrive here as the same statement now.
    fraction = recorded.pop("fraction_outside", None)
    if not isinstance(fraction, float):
        raise FatalAccounting(
            "residual-ink measure returned no float `fraction_outside`; the ink map "
            "cannot record a ratio it was not given"
        )
    recorded["fraction_outside_per_million"] = int(round(fraction * 1_000_000))
    return recorded


def main(registry_factory=ChairRegistry.from_toml) -> int:
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    # Both ingress routes open through the shared constructor, which keeps every
    # direct-entry guard -- register drift, the sealed snapshot, the Exemplar's
    # completion seal, the run-level cap -- on the real route as well.
    context = open_stage_context(args, INK_MAP, registry_factory=registry_factory)
    for ordinal, page, page_path in sealed_pages(context):
        image_bytes = measured_page_bytes(context.tree, ordinal, page)
        try:
            # Empty coverage is intentional: this is the pre-proposal denominator.
            ink_map = page_residual_ink(image_bytes, covered=[])
            edge = page_edge_ink(image_bytes)
            # Retain lossless runs so later coverage decisions cannot
            # re-measure the page under a different pixel predicate. Decoded
            # inside this refusal boundary: it is this module's third decode of
            # the same digest-verified bytes, and an undecodable page must be
            # the named census failure, never a bare traceback mid-publish.
            edge_findings = ink_runs(image_bytes)
        except ValueError as error:
            # `measured_page_bytes` proves these bytes match the digest the
            # Exemplar sealed; it proves nothing about whether this module's own
            # independent decoder can read them. An uncaught decoder ValueError
            # here would escape `run_stage`'s refusal handling as a bare
            # traceback, with `seal_boundary`/`finish` never reached and earlier
            # pages already published -- GOVERNANCE 2's silent loss with extra
            # steps. Named and stopped instead, like every other census failure
            # this stage refuses.
            raise FatalAccounting(
                f"the ink map cannot measure sealed Exemplar page {ordinal}: its own "
                f"digest-verified pixels do not decode ({error}); no boundary was "
                "sealed, and the records already published for earlier pages of this "
                "run are an incomplete map"
            ) from error
        context.publish(
            kind="ink-map",
            subject_id=page["subject_id"],
            outcome="unclaimed-edge-ink" if edge["flagged"] else "mapped",
            inputs=[context.input_ref(page_path)],
            payload={
                "page_ordinal": ordinal,
                "ink": artifact_finding(ink_map),
                "edge": artifact_finding(edge),
                "edge_findings": edge_findings,
            },
        )
    context.seal_boundary()
    context.finish()
    return EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
