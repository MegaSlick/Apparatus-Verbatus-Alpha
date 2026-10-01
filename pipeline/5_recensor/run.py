"""Recensor: establishes that the text is complete. It establishes no text.

Every unit a page-read run counts gets exactly one review (`page_review.py`):
its witness floor, the residual ink on its page and, for a page said to hold no
act, whether that is confirmed. Every page break a reading flags becomes a
`continuation-link`. The stage asks for no recovery and never touches a
reading; a unit that cannot be accepted is held for review with every reason
named.

**It does not select among witnesses.** Witness outcomes form a coverage record
that can mark a unit under-witnessed and the run visibly partial.

    python pipeline/5_recensor/run.py --run-root <dir> --run-id <id>
"""

import sys
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import page_review  # noqa: E402

from common.background import (  # noqa: E402
    BackgroundInferenceRefusal,
    load_background_config,
    resolve_background_policy,
)
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.canonical import is_plain_int  # noqa: E402
from common.contracts.errors import (  # noqa: E402
    ContractError,
    FatalAccounting,
    IncompatibleReuse,
)
from common.contracts.identities import attempt_id  # noqa: E402
from common.contracts.stages import EXEMPLAR, RECENSOR  # noqa: E402
from common.corpus_register import refuse_capture_preference  # noqa: E402
from common.exemplar_boundary import sealed_page_bytes, verify_sealed_page_pixels  # noqa: E402
from common.imaging import grayscale_rows  # noqa: E402
from common.residual_ink import (  # noqa: E402
    INK_NOT_MEASURABLE,
    load_coverage_audit_config,
    residual_ink,
    resolve_coverage_audit_policy,
)
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    EXIT_HELD,
    latest_attempt,
    open_stage_context,
    reading_denominator,
    run_stage,
    stage_manifest,
    stage_parser,
)
from operations.serving.assembly import SERVING_READER  # noqa: E402

DESCRIPTION = "Recensor: establishes that the text is complete. It establishes no text."


def artifacts_for(context, stage: str, kind: str, subject: str) -> list[dict]:
    records = []
    for entry in stage_manifest(context, stage)["artifacts"]:
        if entry["kind"] == kind and entry["subject_id"] == subject:
            records.append(context.tree.read_artifact(stage, kind, entry["artifact_id"]))
    return records


def _records_of_kind(context, stage: str, kind: str) -> Iterator[dict]:
    """Read every artifact of one kind from a stage's manifest, in manifest order."""
    for entry in stage_manifest(context, stage)["artifacts"]:
        if entry["kind"] == kind:
            yield context.tree.read_artifact(stage, kind, entry["artifact_id"])


def _source_rows(run: dict) -> dict[int, dict]:
    """The submitted source-manifest row for each ordinal.

    Every stage that reads sealed Exemplar pixels rebuilds the submitted denominator
    before trusting a page's claim; the Designator and Armarium carry their own copies.
    """
    rows = run.get("source_manifest")
    if not isinstance(rows, list) or not rows:
        raise FatalAccounting("run.json carries no source manifest for the Exemplar boundary")
    sources: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise FatalAccounting("run.json carries a source-manifest row that is not an object")
        ordinal = row.get("ordinal")
        if not is_plain_int(ordinal):
            raise FatalAccounting(
                "run.json carries a source-manifest row without an integer ordinal"
            )
        if ordinal in sources:
            raise FatalAccounting(f"run.json repeats source ordinal {ordinal}")
        sources[ordinal] = row
    return sources


def sealed_page_images(context) -> dict[int, dict]:
    """Every sealed Exemplar page's own artifact, by ordinal, checked against the manifest.

    The residual-ink check reads raw bytes off `payload["image_path"]`, a self-declared
    field the envelope never relates to the page's digest-checked inputs, so it is
    verified first, as every stage that reads page pixels does.
    """
    pages: dict[int, dict] = {}
    for record in _records_of_kind(context, EXEMPLAR, "page"):
        if record["outcome"] != "sealed":
            continue
        ordinal = record["payload"].get("ordinal")
        if not is_plain_int(ordinal):
            raise FatalAccounting(
                f"Exemplar page {record.get('artifact_id')} carries no integer ordinal"
            )
        if ordinal in pages:
            raise FatalAccounting(
                f"the Exemplar carries more than one sealed page for ordinal {ordinal}; "
                "the Recensor has no rule for selecting one page image"
            )
        pages[ordinal] = record

    # Settle the structural denominator before reading pixels.
    sources = _source_rows(context.run)
    for ordinal, record in pages.items():
        source = sources.get(ordinal)
        if source is None:
            raise FatalAccounting(
                f"a sealed Exemplar page names ordinal {ordinal}, which run.json never submitted"
            )
        try:
            verify_sealed_page_pixels(context.tree, context.run, source, record)
        except ContractError as error:
            raise FatalAccounting(
                f"sealed Exemplar page {ordinal} failed pixel verification; the residual-ink "
                "check may not read bytes over an unverified image_path"
            ) from error
    return pages


def page_coverage_findings(context, *, regions: dict[int, list[dict]]) -> dict[int, dict]:
    """Residual-ink findings for every page in `regions`, once per run.

    `regions` maps a sealed page ordinal to the bounds counted as covering its
    ink: every reading region cut on it, and every sealed page, one with no
    region included. The input is the page image itself, never the proposal
    set, a witness or a reading.
    The paper value is the Designator's shared inference under the sealed background
    policy, never the page's own histogram mode, which on a photographed opening is the
    bezel and hides all residual ink. A page whose paper the inference refuses gets a
    finding carrying the refusal and no counts.
    """
    if not regions:
        return {}
    background_config = load_background_config(context.args.ink_map_config)
    context.require_sealed_config("ink-map", background_config["config_sha256"])
    # Set aside the page-spanning component already accounted for by Designator.
    coverage_config = load_coverage_audit_config(context.args.ink_map_config)
    context.require_sealed_config("ink-map", coverage_config["config_sha256"])
    pages = sealed_page_images(context)
    findings: dict[int, dict] = {}
    for ordinal, bounds in regions.items():
        page = pages.get(ordinal)
        if page is None:
            raise FatalAccounting(
                f"a region names source page {ordinal}, which the Exemplar did not seal; "
                "a crop of unsealed pixels is invariant #10's imbalance"
            )
        # Digest the bytes actually measured, not an earlier read.
        image_bytes = sealed_page_bytes(
            context.tree, page, what="the residual-ink check", refusal=FatalAccounting
        )
        width, height, rows = grayscale_rows(image_bytes)
        try:
            findings[ordinal] = {
                **residual_ink(
                    width,
                    height,
                    rows,
                    bounds,
                    background_policy=resolve_background_policy(background_config, width, height),
                    coverage_policy=resolve_coverage_audit_policy(coverage_config, width, height),
                ),
                "background_config_sha256": background_config["config_sha256"],
            }
        except BackgroundInferenceRefusal as error:
            # Without a paper value zero ink would be a false clean page.
            findings[ordinal] = {
                "ink_measurable": False,
                "named_finding": INK_NOT_MEASURABLE,
                "background_refusal": str(error),
                "background_config_sha256": background_config["config_sha256"],
            }
    return findings


def current_review(context, act_id: str) -> dict | None:
    """This act's current Recensor review, or `None` before its first.

    `stage_manifest` rebuilds fresh from the tree on every call, so this also
    sees a review this same pass already published for the act -- not only
    ones from an earlier invocation.
    """
    reviews = artifacts_for(context, RECENSOR, "review", act_id)
    if not reviews:
        return None
    return latest_attempt(reviews, f"Recensor review of {act_id}", operation="recense")


def publish_review(
    context,
    *,
    subject_id: str,
    outcome: str,
    prior: dict | None,
    inputs: list[dict],
    payload: dict,
    check,
) -> dict:
    """Write a review only after rejecting witness-selection vocabulary.

    A review's content can change between passes without the act recovering, because
    page-wide facts come from every act on its page. So the prior review's ordinal is
    tried first (unchanged content reuses byte for byte), and a fresh ordinal is minted
    only when the store proves the content differs. The ordinal is stamped here, never
    trusted from the caller.

    The whole payload is screened, so a future payload field cannot carry
    witness-selection vocabulary unchecked. `check` validates the payload's own
    closed shape.
    """
    refuse_capture_preference(payload, what="a Recensor review")
    check(subject_id, payload)

    def publish_at(ordinal: int) -> dict:
        return context.publish(
            kind="review",
            subject_id=subject_id,
            outcome=outcome,
            attempt=attempt_id(subject_id, "recense", ordinal),
            inputs=inputs,
            payload={**payload, "attempt_ordinal": ordinal},
        )

    if prior is None:
        return publish_at(1)
    try:
        return publish_at(prior["payload"]["attempt_ordinal"])
    except IncompatibleReuse:
        return publish_at(prior["payload"]["attempt_ordinal"] + 1)


def review_a_page_read_run(context, denominator: dict) -> int:
    """The page path (`page_review.py`): every counted unit reviewed, then the v4 receipt."""
    held = page_review.review_pages(
        context,
        denominator,
        page_coverage_findings=page_coverage_findings,
        publish_review=publish_review,
        current_review=current_review,
    )
    # The receipt needs the current manifest and may refuse before the seal.
    context.finish()
    page_review.write_reading_receipt(context, page_coverage_findings=page_coverage_findings)
    context.seal_boundary()
    context.finish()
    return EXIT_HELD if held else EXIT_COMPLETE


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run under the explicitly supplied chair/config implementation."""
    args = stage_parser(DESCRIPTION).parse_args()
    context = open_stage_context(
        args, RECENSOR, registry_factory=registry_factory, serving_reader=SERVING_READER
    )
    return review_a_page_read_run(context, reading_denominator(context))


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
