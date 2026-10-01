"""Armarium: where the output is written, and where the totals must reconcile.

The pipeline ends here, so this is the last place a missing act could hide. Every
reading the denominator counts gets exactly one manifest category, and the
categories are the closed five the contracts define. An act in none of them is a
fatal accounting imbalance and stops the run — never a warning, never a shrug.

**What leaves carries the Perlector's established reading and nothing else.**
Witness testimony informed that reading and its digest-checked references and
provenance leave alongside the result; witness words never become the delivered
text. There is no branch in this file that could put a witness's words into a
delivered text, and that is the point rather than an accident of the fixture.

**Partial cannot look complete.** A held act has no Archetypus, so it has no text
to export; it appears in the review output, and the run's aggregate says `partial`
with every reason named. "Complete" is refused unless everything reconciles —
which includes the witness roster the run was authorized with, not only the acts.

    python pipeline/7_armarium/run.py --run-root <dir> --run-id <id>
"""

import dataclasses
import sys
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# Second, so it lands ahead of the repository root: `spec_from_file_location` does not
# put a loaded file's own directory on `sys.path`, and
# `pipeline/orchestrator/test_terminal_guards.py` loads this file exactly that way.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from armarium_export import (  # noqa: E402
    ARMARIUM_ARCHIVE_NAME,
    NOT_MEASURED_BASIS_SCHEMA,
    NOT_MEASURED_INSTRUMENTS,
    ArmariumProjection,
    act_key_sort_key,
    build_armarium_bundle,
    continuation_join_row,
    edge_hold_pages_from_rows,
    unpaired_continuations,
)

from common import page_path  # noqa: E402
from common.background import (  # noqa: E402
    validate_ink_not_measurable_payload,
    validate_measured_ink_map_payload,
)
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.annotations import validate_annotations  # noqa: E402
from common.contracts.canonical import verify_self_hash  # noqa: E402
from common.contracts.envelope import read_verified  # noqa: E402
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal  # noqa: E402
from common.contracts.outcomes import (  # noqa: E402
    CONTINUATION_FLAGS,
    ArmariumCategory,
    derive_record_text_status,
    run_aggregate,
    terminal_category,
)
from common.contracts.stages import (  # noqa: E402
    ARCHETYPUS,
    ARMARIUM,
    ATTESTATORES,
    EXEMPLAR,
    INK_MAP,
    PERLECTOR,
    RECENSOR,
)
from common.contracts.uncertainty import from_page_perlectio  # noqa: E402
from common.exemplar_boundary import (  # noqa: E402
    verify_exemplar_corpus_seal,
    verify_reading_region_lineage,
    verify_sealed_page_pixels,
)
from common.imaging import dimensions  # noqa: E402
from common.page_accounting import require_page_accounting_policy  # noqa: E402
from common.page_review import (  # noqa: E402
    continuation_links,
    current_page_reviews,
    require_establishable,
    review_coverage,
    review_notes,
    review_reason,
    reviewed_rows,
)
from common.page_testimonia import (  # noqa: E402
    chair_was_served,
    current_page_testimonia,
    declared_page_witness_chairs,
    shown_page_witnesses,
)
from common.residual_ink import (  # noqa: E402
    INK_NOT_MEASURABLE,
    MINIMUM_CONTRAST_BELOW_BACKGROUND,
    edge_ink_from_runs,
    load_coverage_audit_config,
    reconcile_edge_finding_with_runs,
    resolve_coverage_audit_policy,
)
from common.sealed_config import read_sealed_toml
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    EXIT_HELD,
    PAGE_REFUSED_CLASS,
    canary_ordinals,
    open_stage_context,
    reading_denominator,
    run_stage,
    stage_parser,
    submission_identity,
    unaddressed_chairs,
    validate_serving_provenance,
)

DESCRIPTION = "Armarium: where the output is written, and where the totals must reconcile."


def page_census(context) -> dict[int, dict]:
    """Every page's Exemplar outcome, by ordinal, reconciled against the sources.

    The act-level accounting counts readings, so a page the door refused leaves
    no hole in it. The census closes that — every source the run declared must
    have exactly one page outcome, and
    a page with none, or with two, is invariant #10's imbalance at the last
    boundary.
    """
    declared_rows = context.run["source_manifest"]
    sources: dict[int, dict] = {}
    for source in declared_rows:
        ordinal = source.get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise FatalAccounting("the run declares a source without an integer ordinal")
        if ordinal in sources:
            raise FatalAccounting(f"the run declares source ordinal {ordinal} more than once")
        sources[ordinal] = source

    exemplar_manifest = context.tree.build_manifest(EXEMPLAR)
    census: dict[int, dict] = {}
    records: dict[int, dict] = {}
    entries_by_ordinal: dict[int, dict] = {}
    for entry in exemplar_manifest["artifacts"]:
        if entry["kind"] != "page":
            continue
        record = context.tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        ordinal = record["payload"].get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise FatalAccounting(
                f"page outcome {record['artifact_id']} carries no ordinal and cannot "
                "be reconciled against the sources that arrived"
            )
        if ordinal in census:
            raise FatalAccounting(f"page {ordinal} carries two Exemplar outcomes")
        source = sources.get(ordinal)
        if source is None:
            raise FatalAccounting(
                f"the Exemplar produced page ordinal {ordinal}, which run.json never submitted"
            )
        payload = record["payload"]
        if payload.get("declared_path") != source.get("relative_path") or payload.get(
            "declared_sha256"
        ) != source.get("sha256"):
            raise FatalAccounting(
                f"the Exemplar page for ordinal {ordinal} no longer matches its submitted "
                "filename and digest"
            )
        item = {
            "outcome": record["outcome"],
            "reason": payload.get("reason", ""),
            "declared_path": source["relative_path"],
            "declared_sha256": source["sha256"],
            "page_id": record["subject_id"] if record["outcome"] == "sealed" else None,
        }
        if record["outcome"] == "sealed":
            image_path, image_sha256 = payload.get("image_path"), payload.get("source_sha256")
            if not isinstance(image_path, str) or not isinstance(image_sha256, str):
                raise FatalAccounting(
                    f"the sealed Exemplar page for ordinal {ordinal} has no pixel reference"
                )
            item["image_path"] = image_path
            item["image_sha256"] = image_sha256
        if "bytes" in source:
            item["declared_bytes"] = source["bytes"]
        if "ledger_sha256" in source:
            item["ledger_sha256"] = source["ledger_sha256"]
        if source.get("container_page_index") is not None:
            if payload.get("container_page_index") != source["container_page_index"]:
                raise FatalAccounting(
                    f"the Exemplar page for ordinal {ordinal} no longer matches its submitted "
                    "container page index"
                )
            item["container_page_index"] = source["container_page_index"]
        census[ordinal] = item
        records[ordinal] = record
        entries_by_ordinal[ordinal] = entry
        if record["outcome"] == "sealed":
            try:
                page_bytes = verify_sealed_page_pixels(context.tree, context.run, source, record)
                item["_pixel_dimensions"] = dimensions(page_bytes)
            except (ContractError, TypeError, ValueError) as error:
                raise FatalAccounting(
                    "the final Exemplar pixel boundary is not immutable; no export may be "
                    "written over altered or undecodable source bytes"
                ) from error

    # Counted before it is compared as a set: a set comparison cannot tell two pages
    # sharing an ordinal from one page, and that is precisely the arithmetic by which
    # a lost page reconciles. `RunTree.create` refuses a repeated ordinal, but this is
    # the last boundary in the pipeline and it reads a `run.json` written earlier.
    declared_ordinals = [page["ordinal"] for page in declared_rows]
    declared = set(declared_ordinals)
    if len(declared) != len(declared_ordinals):
        raise FatalAccounting(
            f"the run declared {len(declared_ordinals)} source pages under only "
            f"{len(declared)} distinct ordinals; the run's own record cannot say "
            "how many pages arrived, so nothing downstream can balance against it"
        )
    if set(census) != declared:
        raise FatalAccounting(
            f"the run declared source pages {sorted(declared)} but the Exemplar "
            f"accounted for {sorted(census)}; a page in neither the sealed nor the "
            "refused set is a fatal accounting imbalance, never a warning"
        )
    try:
        verify_exemplar_corpus_seal(
            context.tree,
            context.run,
            exemplar_manifest,
            sources,
            records,
            entries_by_ordinal,
        )
    except ContractError as error:
        raise FatalAccounting(str(error)) from error
    return census


def ink_map_page_rows(
    context, census: dict[int, dict], claimed_bounds: dict[int, list[dict]]
) -> tuple[dict, ...]:
    """Re-measure the Ink Map against the readings' actual regions, and say so.

    The pre-proposal map cannot know whether edge ink belongs to an act. Its
    lossless page-space runs let this final boundary apply the verified
    act-region geometry to the *same measurement*, so a claimed edge mark
    releases and a genuinely unclaimed one remains visible for review.

    One row per sealed page, carrying what was found and what was re-measured
    rather than only the resulting hold. The export's clean-machine verifier
    recomputes the held set from these numbers with the ink map's own gate; a
    bare list of held ordinals would be a claim it could only check against
    itself. A page the map never flagged is re-measured by nobody and records
    `remeasured: None`, because writing zeros would record a measurement that
    never occurred. The producer's closed edge summary and initial outcome are
    first reconciled with the retained runs against the original empty crop
    set, independently of the later re-measurement against the act-regions.

    The sealed `[coverage_audit]` policy is read here, out of the same file and
    under the same `ink-map` seal every other reader of it proves
    its bytes against, and resolved for each page's own dimensions: the band
    this stage re-measures in and the gate it releases by have to be the ones
    the Ink Map's own finding was taken under, and a policy resolved for another
    page would make this a second detector rather than the same one.
    """
    coverage_config = load_coverage_audit_config(context.args.ink_map_config)
    context.require_sealed_config("ink-map", coverage_config["config_sha256"])
    found: dict[int, dict] = {}
    for entry in context.tree.build_manifest(INK_MAP)["artifacts"]:
        if entry["kind"] != "ink-map":
            continue
        record = context.tree.read_artifact(INK_MAP, "ink-map", entry["artifact_id"])
        payload = record.get("payload", {})
        ordinal = payload.get("page_ordinal") if isinstance(payload, dict) else None
        # Duplicate page records are refused because manifest order cannot
        # decide which retained finding controls the hold.
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise FatalAccounting(
                "ink-map has a record without an integer page ordinal. The Armarium cannot bind "
                "its finding to a sealed page. Restore the sealed Ink Map inventory or restart "
                "the run before exporting."
            )
        if ordinal not in census or census[ordinal].get("outcome") != "sealed":
            raise FatalAccounting(
                f"ink-map page denominator does not match the Armarium page census: "
                f"page {ordinal} is not sealed. Restore the sealed stage inventories "
                "or restart the run before exporting."
            )
        if ordinal in found:
            raise FatalAccounting(
                f"ink-map repeats page ordinal {ordinal}. The Armarium cannot choose which page "
                "record decides the edge hold. Restore the sealed Ink Map inventory or restart "
                "the run before exporting."
            )
        if record["outcome"] not in {"mapped", "unclaimed-edge-ink", INK_NOT_MEASURABLE}:
            raise FatalAccounting(
                "ink-map has an unknown page finding outcome. The Armarium cannot determine "
                "whether the page remains held. Rebuild the Ink Map under this version before "
                "exporting."
            )
        if record["outcome"] == INK_NOT_MEASURABLE:
            # Validate the sealed, closed refusal before publishing an explicit
            # absence of page-space runs. A bare outcome is not evidence that the
            # page's ink was unavailable to measure.
            try:
                refusal = validate_ink_not_measurable_payload(payload)
                context.require_sealed_config("ink-map", refusal["background_config_sha256"])
            except ContractError as error:
                raise FatalAccounting(
                    f"ink-map page {ordinal} has an invalid sealed ink-not-measurable "
                    "payload. Restore the sealed Ink Map artifact or restart the run before "
                    "exporting."
                ) from error
            found[ordinal] = {"outcome": record["outcome"], "evidence": None}
            continue
        try:
            measured = validate_measured_ink_map_payload(
                payload, audit_contrast=MINIMUM_CONTRAST_BELOW_BACKGROUND
            )
            context.require_sealed_config("ink-map", measured["background_config_sha256"])
        except ContractError as error:
            raise FatalAccounting(
                f"ink-map page {ordinal} has an invalid sealed measured payload. Restore the "
                "sealed Ink Map artifact or restart the run before exporting."
            ) from error
        evidence = payload.get("edge_findings")
        try:
            # Inside the try, and `ContractError` inside the caught set: the
            # policy is resolved for THIS page's own dimensions, which come out
            # of the same evidence blob the measure reads, so damaged evidence
            # can fail here as easily as one line later. Resolved outside, a
            # width of zero left this stage by ContractError naming page
            # geometry instead of by the named refusal that tells an operator
            # which artifact to restore.
            if not isinstance(evidence, dict):
                raise ContractError("the measured payload has no ink-run evidence object")
            sealed_page = census.get(ordinal)
            sealed_dimensions = (
                sealed_page.get("_pixel_dimensions")
                if isinstance(sealed_page, dict) and sealed_page.get("outcome") == "sealed"
                else None
            )
            if (evidence.get("width"), evidence.get("height")) != sealed_dimensions:
                raise ContractError(
                    "the retained ink-run dimensions do not match the sealed Exemplar pixels"
                )
            coverage_policy = resolve_coverage_audit_policy(
                coverage_config, evidence.get("width"), evidence.get("height")
            )
            initial_measure = reconcile_edge_finding_with_runs(
                payload.get("edge"),
                evidence,
                coverage_policy=coverage_policy,
            )
        except (ContractError, KeyError, TypeError, ValueError) as error:
            raise FatalAccounting(
                f"ink-map page {ordinal} has an edge finding that does not reconcile with its "
                "retained page-space evidence. The Armarium cannot verify the page finding "
                "that decides whether edge ink must remain held. Restore the sealed Ink Map "
                "artifact or restart the run before exporting."
            ) from error
        measured_outcome = "unclaimed-edge-ink" if initial_measure["flagged"] else "mapped"
        if record["outcome"] != measured_outcome:
            raise FatalAccounting(
                f"ink-map page {ordinal} records outcome {record['outcome']!r}, but its retained "
                f"page-space evidence measures {measured_outcome!r}. The Armarium cannot choose "
                "between a page finding and the evidence meant to prove it. Repair or restart "
                "the Ink Map stage before exporting."
            )
        found[ordinal] = {
            "outcome": record["outcome"],
            "evidence": evidence,
            "coverage_policy": coverage_policy,
        }
    sealed = {ordinal for ordinal, page in census.items() if page.get("outcome") == "sealed"}
    if set(found) != sealed:
        raise FatalAccounting(
            "ink-map page denominator does not match the Armarium page census. At least one "
            "sealed page lacks a finding or an unsealed page gained one, so page coverage cannot "
            "reconcile. Restore the sealed stage inventories or restart the run before exporting."
        )
    rows = []
    for ordinal in sorted(found):
        finding = found[ordinal]
        remeasured = None
        if finding["outcome"] == "unclaimed-edge-ink":
            try:
                measure = edge_ink_from_runs(
                    finding["evidence"],
                    claimed_bounds.get(ordinal, []),
                    coverage_policy=finding["coverage_policy"],
                )
            except (ContractError, KeyError, TypeError, ValueError) as error:
                raise FatalAccounting(
                    "ink-map page-space edge evidence cannot be re-measured. The Armarium cannot "
                    "decide whether later crops released the page hold. Restore the sealed Ink "
                    "Map artifact or restart the run before exporting."
                ) from error
            remeasured = {
                "total_ink_pixels": measure["total_ink_pixels"],
                "outside_ink_pixels": measure["outside_ink_pixels"],
                "edge_band_pixels": measure["edge_band_pixels"],
                "substantial_ink_pixels": measure["substantial_ink_pixels"],
                # The sealed noise floor and fraction gate the page was judged
                # under, on the row so the export verifier recomputes the hold
                # from the row alone on a clean machine.
                "minimum_ink_pixels": finding["coverage_policy"]["minimum_ink_pixels"],
                "minimum_fraction_outside_bp": finding["coverage_policy"][
                    "minimum_fraction_outside_bp"
                ],
            }
        rows.append(
            {
                "ordinal": ordinal,
                "initial_outcome": finding["outcome"],
                "remeasured": remeasured,
            }
        )
    return tuple(rows)


# The sealed configurations whose own `provenance` block says whether the
# numbers in them were ever measured against this project's corpus. Named by
# the CLI attribute the run seals, so a file renamed in `config/` moves here
# rather than leaving the export quietly reporting one fewer caveat.
#
# `perlector-protocol`'s `[truncation]` block decides whether an act is held as
# truncated, and an uncalibrated instrument deciding a hold is exactly what
# this survey exists to disclose; a caveat that stayed in `config/` and never
# reached the bundle would be one the product does not carry.
_CALIBRATED_CONFIG_ATTRIBUTES: Final = (
    ("designator-geometry", "designator_geometry_config", "geometry"),
    ("perlector-protocol", "perlector_protocol_config", "truncation"),
)


def _config_provenance(context, name: str, path, table: str) -> dict:
    """One sealed configuration's `provenance` block, refused if it has none.

    `table` is the block's own table, because a configuration may declare more
    than one and the survey reports the table whose numbers the run used --
    `[truncation]` in the Perlector protocol, the file's own in each Designator
    file.
    """
    try:
        record, digest = read_sealed_toml(path, f"sealed {name} configuration")
    except ContractError as error:
        raise FatalAccounting(
            f"the sealed configuration at {path} could not be read for its calibration "
            "provenance; the export may not report a caveat it did not read"
        ) from error
    context.require_sealed_config(name, digest)
    provenance = record.get(table, {}).get("provenance")
    if not isinstance(provenance, dict) or "calibrated_for_this_corpus" not in provenance:
        raise FatalAccounting(
            f"the sealed configuration at {path} declares no calibration provenance in "
            f"[{table}]; the export cannot say whether the instrument it reads from that "
            "table was ever measured"
        )
    return provenance


def _typed_calibration_flag(provenance: dict, name: str) -> bool:
    value = provenance["calibrated_for_this_corpus"]
    if not isinstance(value, bool):
        raise FatalAccounting(
            f"the sealed {name} calibration provenance has a non-boolean calibrated_for_this_corpus flag"
        )
    return value


def _typed_sample_count(provenance: dict, name: str) -> int | None:
    if "sample_count" not in provenance:
        return None
    value = provenance["sample_count"]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise FatalAccounting(
            f"the sealed {name} calibration provenance has an invalid sample_count"
        )
    return value


def geometry_calibration_rows(context) -> list[dict]:
    """What each surveyed sealed configuration says about its own calibration.

    Read from the same policy the run sealed (`sealed_config_digests`), so a row
    here is the caveat the run actually ran under rather than whatever is in
    `config/` now. `sample_count` is `None` where the file declares none, which
    is not a zero: `designator_geometry.toml` carries no sample field at all,
    and reporting 0 for it would be a measurement nobody took. The function
    keeps the name the export instrument has; the list it walks is wider than
    Designator geometry (see `_CALIBRATED_CONFIG_ATTRIBUTES`).
    """
    rows = []
    for name, attribute, table in _CALIBRATED_CONFIG_ATTRIBUTES:
        provenance = _config_provenance(context, name, getattr(context.args, attribute), table)
        rows.append(
            {
                "configuration": name,
                "calibrated_for_this_corpus": _typed_calibration_flag(provenance, name),
                "sample_count": _typed_sample_count(provenance, name),
            }
        )
    return rows


def sealed_audit_round_cap(context) -> int:
    """The `round_cap` this run sealed, which decides whether a span can exist."""
    try:
        record, digest = read_sealed_toml(
            context.perlector_audit_config_path, "sealed Perlector audit policy"
        )
    except ContractError as error:
        raise FatalAccounting(
            "the sealed Perlector audit policy could not be read; the export cannot say "
            "whether an uncertain span was reachable on this run"
        ) from error
    context.require_sealed_config("perlector-audit", digest)
    cap = record.get("round_cap")
    if not isinstance(cap, int) or isinstance(cap, bool):
        raise FatalAccounting("the sealed Perlector audit policy declares no integer round cap")
    return cap


def _cached_manifest(context, stage: str, manifest_cache: dict[str, dict]) -> dict:
    """``context.tree.build_manifest(stage)``, read and revalidated once per run.

    Reusing a manifest is only safe because Armarium never writes into the stages
    it reads during its own run, so their artifact sets are static for the whole
    invocation. It is worth doing because ``build_manifest`` walks a stage's whole
    artifact directory and revalidates every envelope, binding and input each time
    it is called, once per row otherwise.
    """
    if stage not in manifest_cache:
        manifest_cache[stage] = context.tree.build_manifest(stage)
    return manifest_cache[stage]


def artifacts_for(
    context, stage: str, kind: str, subject: str, manifest_cache: dict[str, dict]
) -> list[dict]:
    records = []
    for entry in _cached_manifest(context, stage, manifest_cache)["artifacts"]:
        if entry["kind"] == kind and entry["subject_id"] == subject:
            records.append(context.tree.read_artifact(stage, kind, entry["artifact_id"]))
    return records


def export_source_regions(tree, regions: list[dict], census: dict[int, dict]) -> list[dict]:
    """Attach every delivered crop to the original filename-ledger page it used.

    A region's image digest proves the crop bytes, but an export needs the other
    half of the citation link too: which original source file/frame those bytes
    came from.  The act-region retains its transform's Exemplar page
    locator; reconcile it against the final census rather than trusting a bare
    ordinal in a downstream record.
    """
    linked: list[dict] = []
    for region in regions:
        if not isinstance(region, dict):
            raise FatalAccounting("an established reading has a non-object source region")
        ordinal = region.get("source_page_ordinal")
        page_id = region.get("source_page_id")
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or not isinstance(page_id, str)
        ):
            raise FatalAccounting("an established source region has no Exemplar page locator")
        source = census.get(ordinal)
        if source is None or source.get("outcome") != "sealed":
            raise FatalAccounting(
                "an established source region names a page absent from the final sealed census"
            )
        if page_id != source.get("page_id"):
            raise FatalAccounting(
                "an established source region's Exemplar page id disagrees with the final census"
            )
        transform = region.get("transform")
        if (
            not isinstance(transform, dict)
            or transform.get("operation") != "crop"
            or transform.get("source_page_ordinal") != ordinal
            or transform.get("source_page_id") != page_id
            or not isinstance(transform.get("bounds"), dict)
        ):
            raise FatalAccounting(
                "an established source region does not retain its complete crop transform"
            )
        image_path, image_sha256 = region.get("image_path"), region.get("image_sha256")
        if not isinstance(image_path, str) or not isinstance(image_sha256, str):
            raise FatalAccounting("an established source region names no sealed crop")
        crop_ref = {"relative_path": image_path, "sha256": image_sha256}
        what = "an established source region's crop"
        read_verified(tree.read_bytes, crop_ref, what, FatalAccounting)
        entry = dict(region)
        for field in (
            "declared_path",
            "declared_sha256",
            "declared_bytes",
            "ledger_sha256",
            "container_page_index",
        ):
            if field in source:
                entry[field] = source[field]
        linked.append(entry)
    return linked


def missing_export_provenance(payload: object) -> str | None:
    """Name a reading that cannot travel as an exportable, cited result.

    This is deliberately narrower than the lineage checks in
    ``verify_established_page_record``.  A damaged envelope or a disagreement with a
    parent remains fatal accounting; an otherwise sealed established reading
    that simply lacks identity or region provenance is a refused unit with a
    visible review record, not a dropped one.
    """
    if not isinstance(payload, dict) or not verify_self_hash(payload):
        # This does not mean provenance is complete. A damaged envelope is fatal
        # accounting, and `verify_established_page_record` immediately raises on this same
        # self-hash. Returning None delegates to that check; callers must preserve the
        # order rather than treating this helper alone as an exportability decision.
        return None
    if not isinstance(payload.get("provenance"), dict):
        return "the established reading has no model identity provenance"
    regions = payload.get("regions")
    if not isinstance(regions, list) or not regions:
        return "the established reading has no source-region provenance"
    for index, region in enumerate(regions):
        if not isinstance(region, dict):
            return f"source region {index} is not an object"
        required = (
            "region_id",
            "image_path",
            "image_sha256",
            "source_page_ordinal",
            "source_page_id",
        )
        missing = [field for field in required if region.get(field) is None]
        if missing:
            return f"source region {index} lacks {', '.join(missing)} provenance"
    return None


def export_evidence_refs(context, review: dict, established: dict | None) -> list[dict[str, str]]:
    """The small, digest-checked record a review item keeps after text is refused."""
    references = [context.artifact_ref(RECENSOR, "review", review["artifact_id"])]
    if established is not None:
        references.append(
            context.artifact_ref(ARCHETYPUS, "archetypus", established["artifact_id"])
        )
    return sorted(references, key=lambda reference: reference["relative_path"])


def export_run_identity(context) -> tuple[str | None, str | None, dict[str, str]]:
    """The manifest's run identity: a fixture id on a fixture run, a real
    submission's filename-ledger self-hash on a real one -- never both, never
    neither. `submission_identity` itself decides which route this run took
    from `context.run` alone; `context.fixture` -- the refusing accessor -- is
    asked only once that has already ruled out real ingress, so it is never
    touched on the route where it would refuse.

    Returns `(submission_id, fixture_id, run_identity)`: the two raw values a
    caller may still need individually, and the one-key dict `main` splices
    straight into the `export` artifact's payload.
    """
    submission_id = submission_identity(context.run)
    fixture_id = context.fixture["fixture_id"] if submission_id is None else None
    run_identity: dict[str, str] = (
        {"submission_id": submission_id}
        if submission_id is not None
        else {"fixture_id": fixture_id}
    )
    return submission_id, fixture_id, run_identity


# --- Page-read runs: the readings the Perlector established on each page it read whole
#
# The denominator is `common.stage.reading_acts`. Rows of kind `act` are the act
# partition; rows of kind `other` are a separate, labelled layer that is never
# counted as an act. A row standing for a page with no reading (`page-unread`,
# `page-blank`) is an act-partition unit with no text, held or confirmed blank by
# its review. Regions come from the Perlector's `act-region` records, each proven
# from the Exemplar by `verify_reading_region_lineage`.


def _page_category(
    context, row: dict, review: dict, manifest_cache: dict[str, dict]
) -> tuple[ArmariumCategory, dict | None]:
    """One row's terminal category, from its review and (when accepted) its one Archetypus."""
    established = artifacts_for(context, ARCHETYPUS, "archetypus", row["act_id"], manifest_cache)
    outcome = review["outcome"]
    terminal = terminal_category(RECENSOR, outcome)
    if terminal is not None:
        if established:
            raise FatalAccounting(
                f"{row['act_key']} is {outcome!r} at the Recensor but carries an Archetypus "
                "record anyway; a reading not accepted may not also be established"
            )
        if terminal is ArmariumCategory.CONFIRMED_BLANK and row["class"] != "page-blank":
            raise FatalAccounting(
                f"{row['act_key']} is a {row['class']} row, and only a page read as blank can "
                "be confirmed blank"
            )
        return terminal, None
    if outcome != "accepted":
        raise FatalAccounting(
            f"{row['act_key']} has Recensor outcome {outcome!r}, which reaches no terminal "
            "category; the export may not complete over an undecided reading"
        )
    # The denominator's disposition is binding: a held row is accepted only
    # when the review releases exactly its releasable holds by name.
    require_establishable(row, review)
    if len(established) != 1:
        raise FatalAccounting(
            f"{row['act_key']} was accepted by the Recensor but carries {len(established)} "
            "Archetypus records; exactly one established reading is exported"
        )
    return terminal_category(ARCHETYPUS, established[0]["outcome"]), established[0]


def verify_established_page_record(
    context, row: dict, review: dict, established: dict
) -> tuple[dict, dict]:
    """Reconcile a page-path Archetypus against its row, its review and its reading.

    Returns the record's payload and the reading. The region is re-proven from
    the Exemplar here, not read out of the record, and the damage layers are
    recomputed from the reading.
    """
    payload = established.get("payload")
    if not isinstance(payload, dict) or not verify_self_hash(payload):
        raise FatalAccounting("an Archetypus payload fails its own self-hash before export")
    expected = {
        "act_id": row["act_id"],
        "act_key": row["act_key"],
        "page_id": row["page_id"],
        "kind": row["kind"],
        "status": "established",
    }
    if any(payload.get(field) != value for field, value in expected.items()):
        raise FatalAccounting(
            f"the Archetypus of {row['act_key']} does not describe the reading being exported"
        )
    review_ref = context.artifact_ref(RECENSOR, "review", review["artifact_id"])
    reading_ref = row["perlectio_ref"]
    region_ref = row["region_ref"]
    if (
        review["outcome"] != "accepted"
        or payload.get("recensor_ref") != review_ref
        or payload.get("perlectio_ref") != reading_ref
        or payload.get("dissent_ref") != reading_ref
        or review["payload"].get("perlectio_ref") != reading_ref
        or reading_ref not in review.get("inputs", [])
    ):
        raise FatalAccounting(
            f"the Archetypus of {row['act_key']} is not bound to the accepted review of its "
            "counted reading"
        )
    reading = context.tree.read_artifact_reference(
        reading_ref, stage=PERLECTOR, kind="perlectio", subject_id=row["act_id"]
    )
    reading_payload = reading.get("payload")
    if not isinstance(reading_payload, dict) or reading_payload.get("act_region_ref") != region_ref:
        raise FatalAccounting(f"the reading of {row['act_key']} names another act-region")
    if (
        reading.get("outcome") != "read"
        or reading_payload.get("schema") != page_path.PERLECTIO_SCHEMA
        or reading_payload.get("kind") != row["kind"]
        or reading_payload.get("holds") != []
        or reading_payload.get("page_holds") != []
    ):
        raise FatalAccounting(
            f"the reading of {row['act_key']} is held or is not the row's own page reading, "
            "yet an Archetypus established it"
        )
    region_record = context.tree.read_artifact_reference(
        region_ref, stage=PERLECTOR, kind="act-region", subject_id=row["act_id"]
    )
    try:
        region = verify_reading_region_lineage(context.tree, context.run, region_record)
    except ContractError as error:
        raise FatalAccounting(
            f"the act-region of {row['act_key']} does not trace to the Exemplar: {error}"
        ) from error
    try:
        annotations = validate_annotations(
            reading_payload.get("annotations", []),
            reading_payload.get("text"),
            None,
            f"the reading of {row['act_key']} annotations",
        )
        uncertainty = from_page_perlectio(reading_payload)
        text_status = derive_record_text_status(payload.get("text"), annotations, uncertainty)
    except SchemaRefusal as error:
        raise FatalAccounting(
            f"the damage layers of {row['act_key']} cannot be reconciled with its reading"
        ) from error
    if (
        payload.get("text") != reading_payload.get("text")
        or payload.get("regions") != [region]
        or payload.get("provenance") != reading_payload.get("provenance")
        or payload.get("annotations") != annotations
        or payload.get("uncertainty") != uncertainty
        or payload.get("text_status") != text_status
    ):
        raise FatalAccounting(
            f"the Archetypus of {row['act_key']} does not exactly preserve the reading its "
            "review accepted"
        )
    expected_inputs = [review_ref, reading_ref, region_ref, context.input_ref(region["image_path"])]
    if sorted(established.get("inputs", []), key=lambda item: item["relative_path"]) != sorted(
        expected_inputs, key=lambda item: item["relative_path"]
    ):
        raise FatalAccounting(
            f"the Archetypus of {row['act_key']} does not input exactly its review, reading, "
            "act-region and crop"
        )
    return payload, reading


def export_page_witnesses(context, reading: dict, page_testimonia: list[dict]) -> list[dict]:
    """The page witnesses a reading was shown, each its chair's current page Testimonium.

    `page_testimonia` is the reading's page's entry in `current_page_testimonia`.
    Each witness is exported under its chair, read from the Testimonium it
    names, and the label the reader saw it under (a pseudonym when the run was
    blinded).
    """
    payload = reading["payload"]
    shown = shown_page_witnesses(
        context,
        reading,
        page_testimonia,
        f"the established page reading of {payload['page_id']} entry {payload.get('n')}",
    )
    witnesses = []
    for witness in shown:
        record = witness["testimonium"]
        validate_serving_provenance(
            context,
            record["payload"].get("provenance"),
            producer_stage=ATTESTATORES,
            require_receipt=chair_was_served(context, record),
        )
        witnesses.append(
            {
                "chair": witness["chair"],
                "witness_label": witness["witness_label"],
                "outcome": record["outcome"],
                "testimonium_ref": witness["testimonium_ref"],
                "provenance": record["payload"]["provenance"],
            }
        )
    return sorted(witnesses, key=lambda witness: witness["chair"])


def reading_region_bounds_by_page(context, rows: list[dict]) -> dict[int, list[dict]]:
    """The rectangles every placed reading (act or other) was cut from, proven from the Exemplar.

    Only these may release unclaimed edge ink on the page path: the Perlector's
    `act-region` records, each re-verified here by the function that uses it.
    """
    claimed: dict[int, list[dict]] = {}
    for row in rows:
        if row["region_ref"] is None or row["class"] != "reading":
            continue
        region = context.tree.read_artifact_reference(
            row["region_ref"], stage=PERLECTOR, kind="act-region", subject_id=row["act_id"]
        )
        try:
            verified = verify_reading_region_lineage(context.tree, context.run, region)
        except ContractError as error:
            raise FatalAccounting(
                f"the act-region of {row['act_key']} cannot be verified as a crop of its page, "
                "so its bounds cannot release any page ink"
            ) from error
        claimed.setdefault(verified["source_page_ordinal"], []).append(
            verified["transform"]["bounds"]
        )
    return claimed


def page_continuation_joins(
    links: list[dict], projected_acts: list[dict], formats: tuple[str, ...]
) -> tuple[dict, ...]:
    """Each Recensor continuation link as a join row over the delivered literals.

    Every flagged page break is a join, so each keeps the run partial with its
    reason: an agreed link with both sides delivered is reconstructed; a side
    with no `act` entry names no act (`side-names-no-act`); a link whose two
    readings' flags disagree reconstructs nothing (`flags-disagree`). Each
    link is one `page_review.continuation_links` proved to join `act` edges.
    """
    delivered_texts = {
        act["act_id"]: act["canonical_clean_text"]
        for act in projected_acts
        if act["category"] == ArmariumCategory.DELIVERED.value
    }
    joins = []
    for index, link in enumerate(links):
        sides = [link["head_act_id"], link["tail_act_id"]]
        head, tail = link["from_page_ordinal"], link["to_page_ordinal"]
        joins.append(
            continuation_join_row(
                join_id=f"join-{head}-{tail}-{index}",
                candidate_ref=link["ref"],
                head_page_ordinal=head,
                tail_page_ordinal=tail,
                head_act_ids=[sides[0]] if sides[0] else [],
                tail_act_ids=[sides[1]] if sides[1] else [],
                delivered_texts=delivered_texts,
                selected_formats=formats,
                flags_disagree=not link["agreed"],
            )
        )
    return tuple(joins)


def page_accounting_rows(context, pages: dict[int, dict], real: set[int]) -> list[dict]:
    """Each real sealed page's accounting: rule statuses, hold codes and policy digest.

    The policy a page names must be the one this run sealed.
    """
    policy = require_page_accounting_policy(context, context.page_accounting_config_path)
    rows = []
    for ordinal in sorted(real):
        page = pages[ordinal]
        record = context.tree.read_artifact_reference(
            page["accounting_ref"],
            stage=PERLECTOR,
            kind="page-accounting",
            subject_id=page["page_id"],
        )
        payload = record["payload"]
        if payload.get("policy_sha256") != policy.sha256:
            raise FatalAccounting(
                f"page {ordinal}'s accounting was measured under another page-accounting "
                "policy than this run sealed"
            )
        rules = payload.get("rules")
        if not isinstance(rules, dict) or not all(
            isinstance(rule, dict) and isinstance(rule.get("status"), str)
            for rule in rules.values()
        ):
            raise FatalAccounting(f"page {ordinal}'s accounting records no rule statuses")
        rows.append(
            {
                "ordinal": ordinal,
                "page_id": page["page_id"],
                "rules": {name: rules[name]["status"] for name in sorted(rules)},
                "hold_codes": sorted(set(payload["holds"])),
                "policy_sha256": payload["policy_sha256"],
                "accounting_ref": page["accounting_ref"],
            }
        )
    return rows


def page_not_measured_basis(context, pages: dict[int, dict], projected_acts: list[dict]) -> dict:
    """What a page-read run did not measure, from its own records and sealed configurations."""
    policy = require_page_accounting_policy(context, context.page_accounting_config_path)
    thresholds = []
    for field in dataclasses.fields(policy):
        if field.name == "sha256":
            continue
        value = getattr(policy, field.name)
        if not isinstance(value, int) or isinstance(value, bool):
            raise FatalAccounting(
                f"the sealed page-accounting threshold {field.name} is {value!r}, not an integer; "
                "the export discloses every threshold and cannot state this one"
            )
        thresholds.append((field.name, value))
    thresholds.sort()
    audits = []
    for page in pages.values():
        reading = context.tree.read_artifact_reference(
            page["reading_ref"], stage=PERLECTOR, kind="page-reading", subject_id=page["page_id"]
        )
        audit = reading["payload"].get("audit")
        if not isinstance(audit, dict) or not isinstance(audit.get("state"), str):
            raise FatalAccounting(
                f"page {page['page_ordinal']}'s reading records no audit state; the export "
                "cannot say whether Pass C ran on it"
            )
        audits.append(audit["state"])
    delivered = [
        act for act in projected_acts if act["category"] == ArmariumCategory.DELIVERED.value
    ]
    states = [act["uncertainty"]["assessment"]["state"] for act in delivered]
    basis = {
        "schema": NOT_MEASURED_BASIS_SCHEMA,
        "perlector-uncertain-spans": {
            "sealed_audit_round_cap": sealed_audit_round_cap(context),
            "acts_delivered": len(delivered),
            "acts_with_uncertain_spans": sum(
                1 for act in delivered if act["uncertainty"].get("uncertain_spans")
            ),
            "acts_assessed": states.count("assessed"),
            "acts_not_assessed": states.count("not-assessed"),
        },
        "designator-geometry-calibration": {"configurations": geometry_calibration_rows(context)},
        # The page accounting's policy carries no calibration record; every value
        # in it is a starting value (`config/page_accounting.toml`).
        "page-accounting-thresholds": {
            "policy_sha256": policy.sha256,
            "thresholds": [{"name": name, "value": value} for name, value in thresholds],
            "calibrated_for_this_corpus": False,
            "sample_count": None,
        },
        "perlector-pass-c": {
            "pages_read": len(audits),
            "pages_audit_not_run": audits.count("not-run"),
            "sealed_audit_round_cap": sealed_audit_round_cap(context),
        },
    }
    missing = [name for name in NOT_MEASURED_INSTRUMENTS if name not in basis]
    if missing:
        raise FatalAccounting(f"the page-read not-measured basis names no record for {missing}")
    return basis


def _export(context, formats, census: dict[int, dict], canaries: set[int]) -> int:
    """Export the run: acts, the other layer, page rows, and the page accounting."""
    submission_id, fixture_id, run_identity = export_run_identity(context)
    real_census = {ordinal: page for ordinal, page in census.items() if ordinal not in canaries}
    denominator = reading_denominator(context)
    pages, rows = denominator["pages"], denominator["acts"]
    # A refused page is the census's to report, with the Door's reason; it is
    # never reviewed and never counted.
    for row in rows:
        if row["class"] == PAGE_REFUSED_CLASS:
            _require_refused_in_census(row, census)
    rows = reviewed_rows(rows)
    reviews = current_page_reviews(context, rows)
    links = continuation_links(context, rows)
    testimonia = current_page_testimonia(context)
    manifest_cache: dict[str, dict] = {}
    categories: dict[str, ArmariumCategory] = {}
    coverages: dict[str, dict] = {}
    act_text_status: dict[str, str] = {}
    act_pages: dict[str, list[int]] = {}
    # Each delivered act's raised continuation flags, so one no link pairs is named.
    continuation_flags: dict[str, list[str]] = {}
    projected_acts: list[dict] = []
    projected_others: list[dict] = []
    delivered: list[dict] = []
    non_delivered: list[dict] = []
    canary_acts: list[dict] = []
    for row in rows:
        review = reviews[row["act_id"]]
        category, established = _page_category(context, row, review, manifest_cache)
        if row["page_ordinal"] in canaries:
            canary_entry = {
                "act_id": row["act_id"],
                "act_key": row["act_key"],
                "category": category.value,
                "page_ordinals": [row["page_ordinal"]],
            }
            context.publish(
                kind="manifest-entry",
                subject_id=row["act_id"],
                outcome=category.value,
                payload=canary_entry,
            )
            canary_acts.append(canary_entry)
            continue
        entry = {
            "act_id": row["act_id"],
            "act_key": row["act_key"],
            "kind": row["kind"],
            "class": row["class"],
            "page_ordinal": row["page_ordinal"],
            "category": category.value,
            "hold_codes": row["hold_codes"],
            "witness_coverage": review_coverage(review),
            "review_notes": review_notes(review),
            "evidence_refs": export_evidence_refs(context, review, established),
        }
        if established is not None and category is ArmariumCategory.DELIVERED:
            refusal = missing_export_provenance(established.get("payload"))
            if refusal is None:
                payload, reading = verify_established_page_record(context, row, review, established)
                try:
                    validate_serving_provenance(
                        context,
                        payload.get("provenance"),
                        producer_stage=PERLECTOR,
                        require_receipt=True,
                    )
                except SchemaRefusal as error:
                    refusal = f"the established reading's provenance was refused: {error}"
                else:
                    entry.update(
                        {
                            "text": payload["text"],
                            "text_status": payload["text_status"],
                            "transcription_annotations": payload["annotations"],
                            "provenance": payload["provenance"],
                            "source_regions": export_source_regions(
                                context.tree, payload["regions"], census
                            ),
                            "perlectio_ref": payload["perlectio_ref"],
                            "recensor_ref": payload["recensor_ref"],
                            "witnesses": export_page_witnesses(
                                context, reading, testimonia.get(row["page_id"], [])
                            ),
                            "dissent_ref": payload["dissent_ref"],
                            "uncertainty": payload["uncertainty"],
                        }
                    )
                    delivered.append(entry)
            if refusal is not None:
                category = ArmariumCategory.REFUSED_WITH_REASON
                entry["category"] = category.value
                entry["reason"] = refusal
                non_delivered.append(entry)
        else:
            reason = review_reason(review)
            if not reason and row["hold_codes"]:
                reason = f"holds: {', '.join(row['hold_codes'])}"
            entry["reason"] = reason
            non_delivered.append(entry)
        is_delivered = category is ArmariumCategory.DELIVERED
        projected = {
            "act_id": row["act_id"],
            "act_key": row["act_key"],
            "category": category.value,
            "canonical_clean_text": entry.get("text") if is_delivered else None,
            "text_status": entry.get("text_status"),
            "transcription_annotations": entry.get("transcription_annotations"),
            "provenance": entry.get("provenance"),
            "source_regions": entry.get("source_regions", []),
            "reason": entry.get("reason"),
            "evidence_refs": entry["evidence_refs"],
            "witnesses": entry.get("witnesses", []),
            "perlectio_ref": entry.get("perlectio_ref"),
            "recensor_ref": entry.get("recensor_ref"),
            "dissent_ref": entry.get("dissent_ref"),
            "approval_ref": None,
            "uncertainty": entry.get("uncertainty"),
        }
        if row["kind"] == "other":
            projected_others.append({**projected, "page_ordinal": row["page_ordinal"]})
        else:
            categories[row["act_key"]] = category
            coverages[row["act_key"]] = entry["witness_coverage"]
            act_pages[row["act_key"]] = sorted(
                {row["page_ordinal"]}
                | {region["source_page_ordinal"] for region in entry.get("source_regions", [])}
            )
            if is_delivered:
                act_text_status[row["act_key"]] = entry["text_status"]
                raised = [flag for flag in CONTINUATION_FLAGS if row[flag] is True]
                if raised:
                    continuation_flags[row["act_key"]] = raised
            projected_acts.append(projected)
        context.publish(
            kind="manifest-entry",
            subject_id=row["act_id"],
            outcome=category.value,
            payload=entry,
        )

    all_ink_map_pages = ink_map_page_rows(
        context, census, reading_region_bounds_by_page(context, rows)
    )
    ink_map_pages = [row for row in all_ink_map_pages if row["ordinal"] not in canaries]
    joins = page_continuation_joins(links, projected_acts, formats.formats)
    unaddressed = list(unaddressed_chairs(context.registry.config))
    other_categories_by_page: dict[int, list[str]] = {}
    for other in projected_others:
        other_categories_by_page.setdefault(other["page_ordinal"], []).append(other["category"])
    aggregate = run_aggregate(
        categories,
        coverages,
        real_census,
        unaddressed_chairs=unaddressed,
        act_pages=act_pages,
        act_text_status=act_text_status,
        edge_hold_pages=edge_hold_pages_from_rows(ink_map_pages),
        continuation_joins=joins,
        page_read=True,
        other_categories_by_page=other_categories_by_page,
        unpaired_continuations=unpaired_continuations(
            continuation_flags,
            list(joins),
            {act["act_id"]: act["act_key"] for act in projected_acts},
        ),
    )
    real_sealed = {
        ordinal for ordinal, page in real_census.items() if page.get("outcome") == "sealed"
    }
    page_rows = [
        {
            "ordinal": ordinal,
            **{
                key: value
                for key, value in real_census[ordinal].items()
                if key != "_pixel_dimensions"
            },
        }
        for ordinal in sorted(real_census)
    ]
    bundle = build_armarium_bundle(
        ArmariumProjection(
            fixture_id=fixture_id,
            submission_id=submission_id,
            scenario=context.scenario,
            config_digest=context.config_digest,
            aggregate=aggregate,
            acts=tuple(projected_acts),
            pages=tuple(page_rows),
            source_manifest=tuple(
                row for row in context.run["source_manifest"] if row["ordinal"] not in canaries
            ),
            expected_acts=len(projected_acts),
            witness_chairs=tuple(context.witness_chairs),
            witness_floor=context.witness_floor,
            aggregate_basis={
                "coverage_records": coverages,
                "unaddressed_chairs": unaddressed,
                "act_pages": act_pages,
                "act_text_status": act_text_status,
                "continuation_flags": continuation_flags,
                "page_witness_chairs": sorted(declared_page_witness_chairs(context)),
            },
            ink_map_pages=ink_map_pages,
            not_measured_basis=page_not_measured_basis(
                context,
                {ordinal: page for ordinal, page in pages.items() if ordinal in real_sealed},
                projected_acts,
            ),
            continuation_joins=joins,
            other_readings=tuple(projected_others),
            page_accounting=tuple(page_accounting_rows(context, pages, real_sealed)),
        ),
        formats,
        context.tree.read_bytes,
    )
    bundle_ref = context.retain(bundle.data)
    export_status = bundle.manifest["claims"]["status"]
    by_key = lambda item: act_key_sort_key(item["act_key"])  # noqa: E731
    context.publish(
        kind="export",
        subject_id="export",
        outcome=(
            ArmariumCategory.DELIVERED.value
            if export_status == "complete"
            else ArmariumCategory.HELD_FOR_REVIEW.value
        ),
        payload={
            **run_identity,
            "scenario": context.scenario,
            "aggregate": aggregate,
            "expected_acts": len(projected_acts),
            "delivered": sorted((item for item in delivered if item["kind"] == "act"), key=by_key),
            "non_delivered": sorted(
                (item for item in non_delivered if item["kind"] == "act"), key=by_key
            ),
            # The labelled other layer, never counted in `expected_acts`.
            "other_readings": sorted(
                (item for item in delivered + non_delivered if item["kind"] == "other"),
                key=by_key,
            ),
            "pages": page_rows,
            **(
                {
                    "canary": {
                        "ordinals": sorted(canaries),
                        "acts": sorted(canary_acts, key=by_key),
                    }
                }
                if canaries
                else {}
            ),
            "witness_chairs": context.witness_chairs,
            "witness_floor": context.witness_floor,
            "bundle": {
                "filename": ARMARIUM_ARCHIVE_NAME,
                "format": "zip",
                "reference": bundle_ref,
                "sha256": bundle_ref["sha256"],
                "manifest_member": "EXPORT_MANIFEST.json",
                "manifest_self_hash": bundle.manifest["self_hash"],
                "claims_status": export_status,
            },
        },
        inputs=[bundle_ref],
    )
    context.seal_boundary()
    context.finish()
    return EXIT_COMPLETE if export_status == "complete" else EXIT_HELD


def _require_refused_in_census(row: dict, census: dict[int, dict]) -> None:
    """A `page-refused` row must stand for a page the census has refused."""
    page = census.get(row["page_ordinal"])
    if page is None or page.get("outcome") == "sealed":
        raise FatalAccounting(
            f"{row['act_key']} stands for a refused page, but the census has page "
            f"{row['page_ordinal']} {'sealed' if page else 'absent'}"
        )


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run under the explicitly supplied chair/config implementation."""
    args = stage_parser(DESCRIPTION).parse_args()
    context = open_stage_context(args, ARMARIUM, registry_factory=registry_factory)
    formats = context.armarium_formats
    if formats is None:
        raise FatalAccounting("Armarium has no format projection bound to the run configuration")
    # Verify the source ledger's final boundary before publishing even a reusable
    # manifest entry. A damaged Exemplar seal must stop export at once.
    census = page_census(context)
    canaries = canary_ordinals(context.run)
    if not canaries <= set(census):
        raise FatalAccounting("sealed canary ordinals are absent from the Exemplar page census")
    return _export(context, formats, census, canaries)


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
