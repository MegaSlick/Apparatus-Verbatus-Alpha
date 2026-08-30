"""The door: what may enter at all, decided by bytes alone.

The door owns no directory. It writes its admissions and refusals into the
Exemplar's, so the record of what arrived and the record of what was sealed sit
together — a refusal filed somewhere nothing downstream reads is a refusal that has
been lost, which GOVERNANCE 2 does not allow.

The one decoder-routing module (`admission.py`) decides from source bytes, never a
declared extension. Its configuration names how a source is read, not formats to
decline: ordinary rasters are decoder-backed; PDF and TIFF page containers fan out
and render once. PDFium paints the complete visible PDF page rather than extracting
an image XObject, so text beside an image stays in the sealed pixels.

Two invariants from the harvest still shape this. **#1: only images enter, verified
by decoding, not by extension** — now the real structural decode, not a magic-byte
check. **#3: a refused file is never silently omitted** — every refusal is an
artifact with a reason drawn from `admission.RefusalReason`'s closed set, and an
input set that admitted nothing is a loud failure rather than a green run with no
output.

**Two ways in, and the difference between them is not a flag.** The fixture path
runs the walking skeleton on the repository's own declared synthetic pages, and it
refuses to treat any other folder as a fixture — fixture status comes from the
declared fixture root and the `load_fixture` manifest, never from a caller's word
(ruling 2026-08-04, item 1). Everything else is real input: it must live inside an
approved storage location, and which of the two routes created a run is sealed into
`run.json`'s own self-hashed authority as the run's `ingress`, so a later reader
asks the run authority rather than an optional field on a stage artifact that could
simply be absent.

**Cut 2026-08-09, per Tyrel's ruling that session.** Real input used to also need a
current data-gate approval-record artifact before this door would admit it. His
ruling: none of this material ever reaches git regardless of any such sign-off — it
runs through the pipeline on a GPU host, `workbench/` is gitignored, and an ingress
check plus a pre-push payload scan already cover that mechanically — so the
requirement bought nothing and is gone. `operations.submit.gate`'s storage-root check
is untouched; only the approval artifact and its currency check are cut.

Invoked as a program:

    python pipeline/1_exemplar/door.py --run-root <dir> --run-id <id>
    python pipeline/1_exemplar/door.py --run-root <dir> --run-id <id> \
        --submission-folder <dir> --submission-manifest <path>
"""

import hashlib
import json
import os
import stat
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any, BinaryIO, Callable, Final, Mapping, NamedTuple

ROOT = Path(__file__).resolve().parents[2]
# The one folder in this repository whose contents are declared synthetic. A
# caller-named folder is real input, whatever it is called and whatever it holds.
DECLARED_SYNTHETIC_FIXTURE_ROOT: Final = ROOT / "proof"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "pipeline" / "0_triage"))

import admission  # noqa: E402
import manifest as triage_manifest  # noqa: E402
import pdf_render  # noqa: E402
import render_config  # noqa: E402
from admission import RefusalReason  # noqa: E402
from image_formats import (  # noqa: E402
    MAX_DIMENSION,
    MAX_PIXELS,
    MAX_SOURCE_BYTES,
    FormatRefusal,
    count_raster_pages,
    decode_raster,
    raster_renderer_recipe,
    render_raster_page,
    sniff,
)

from common.alignment import DEFAULT_ALIGNMENT_CONFIG_PATH, load_alignment_limits  # noqa: E402
from common.armarium_formats import (  # noqa: E402
    DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH,
    bind_armarium_formats,
)
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.approval import (  # noqa: E402
    real_ingress_record,
    synthetic_fixture_ingress_record,
)
from common.contracts.canonical import digest_bytes, digest_of, self_hash  # noqa: E402
from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.identities import artifact_id  # noqa: E402
from common.contracts.stages import DOOR  # noqa: E402
from common.corpus_register import read_register_file  # noqa: E402
from common.decoding import DEFAULT_DECODING_CONFIG_PATH, load_decoding_policy  # noqa: E402
from common.exemplar_boundary import SEALED_DERIVATIVE_PAGE_KIND  # noqa: E402
from common.hard_failure import load_hard_failure_policy  # noqa: E402
from common.recovery import load_recovery_policy  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402
from common.stage import (  # noqa: E402
    DEFAULT_CORPUS_FRAME_CONFIG_PATH,
    DEFAULT_PERLECTOR_AUDIT_CONFIG_PATH,
    DEFAULT_PERLECTOR_PROTOCOL_CONFIG_PATH,
    DEFAULT_TRIAGE_MODES_CONFIG_PATH,
    DEFAULT_WITNESS_CONTEXT_CONFIG_PATH,
    EXIT_COMPLETE,
    StageContext,
    adapter_recipe_for,
    load_corpus_frame_policy,
    load_fixture,
    refuse_halted_run,
    require_corpus_frame_shard,
    require_triage_modes,
    run_config_bindings,
    run_stage,
    scenario_for,
    stage_parser,
    validate_witness_context_bindings,
)
from common.witness_adapters import validate_witness_adapter_bindings  # noqa: E402
from operations.submit import gate, inventory  # noqa: E402
from operations.submit import submit as submission_ledger  # noqa: E402


class SourceEntry(NamedTuple):
    """One submitted frame may own several rows in the post-fan-out census."""

    ordinal: int
    declared_path: str
    declared_sha256: str | None
    container_page_index: int | None = None
    declared_size: int | None = None
    ledger_sha256: str | None = None
    detected_format: str | None = None
    triage_row: dict[str, Any] | None = None
    triage_part_index: int | None = None
    source_frame_index: int | None = None
    # Computed during expansion so membership binds inspected bytes before the
    # run seals. Unreadable and oversized sources retain None and their ordinal.
    computed_sha256: str | None = None


def _membership_sha256(source: SourceEntry) -> str | None:
    """The digest one page binds into shard membership.

    Membership prefers inspected bytes because ledger declarations remain
    untrusted until admission, after the run authority seals. Container pages
    additionally bind their index because they share one whole-file digest and
    their decoded page digests do not exist yet. Unreadable and oversized
    sources fall back to their declaration so their ordinals remain visible.
    """
    inspected = source.computed_sha256 or source.declared_sha256
    if inspected is None or source.container_page_index is None:
        return inspected
    return digest_of(
        {
            "container_sha256": inspected,
            "container_page_index": source.container_page_index,
        }
    )


class _Decision(NamedTuple):
    outcome: str
    reason: str | None
    digest: str | None
    store_bytes: bytes | None
    geometry: tuple[int, int] | None
    rendered_from: dict[str, Any] | None = None


DOOR_REFUSAL_REPORT_SCHEMA: Final = "door-refusal-report.v0"
DOOR_REFUSAL_REPORT_SUBJECT: Final = "refusal-report"
DOOR_DUPLICATE_REPORT_SCHEMA: Final = "door-duplicate-report.v0"
DOOR_DUPLICATE_REPORT_SUBJECT: Final = "duplicate-report"
DOOR_CLUSTER_REPORT_SCHEMA: Final = "door-re-shoot-cluster-report.v1"
_SOURCE_HASH_CHUNK: Final = 1024 * 1024
_SNIFF_BYTES: Final = 4096
# Triage JSON is untrusted ingress too. The real Door can create at most one
# 1,000-page shard (the corpus-frame validator refuses any configured maximum
# above 1,000), so a larger decision document or derivative census cannot shape
# this run. Bound both before JSON-controlled lists reach Unit 5's pairwise
# partition validation.
MAX_TRIAGE_DOCUMENT_BYTES: Final = 64 * 1024 * 1024
MAX_TRIAGE_DERIVATIVE_PAGES: Final = 1_000
# The real Exemplar Door decodes/renders bytes; it is not the walking skeleton's
# fake adapter. Bump this deliberately whenever source behavior changes so a real
# run cannot resume under pixels made by a different Door implementation.
REAL_DOOR_ADAPTER_REVISION: Final = "exemplar-door-v5"


def _source_digest_stream(handle: BinaryIO) -> tuple[str, int]:
    """Hash an already-open source and reset it for the PDF decoder."""
    digest = hashlib.sha256()
    size = 0
    handle.seek(0)
    while chunk := handle.read(_SOURCE_HASH_CHUNK):
        digest.update(chunk)
        size += len(chunk)
    handle.seek(0)
    return digest.hexdigest(), size


def _sniff_source_stream(handle: BinaryIO) -> str | None:
    """Route an already-open source from a bounded prefix, then reset it."""
    handle.seek(0)
    detected = sniff(handle.read(_SNIFF_BYTES))
    handle.seek(0)
    return detected


def fixture_pages_for_scenario(fixture: dict, scenario: str) -> list[dict]:
    """Return the synthetic pages active in one declared fixture scenario.

    A scenario restriction is fixture data, never a real-ingress filter. It
    exists so an additional proof page can exercise a narrow integration path
    without silently changing the input of every established acceptance run.
    """
    scenario_for(fixture, scenario)
    declared_scenarios = {row["name"] for row in fixture["scenario"]}
    active = []
    for page in fixture["page"]:
        restrictions = page.get("scenarios")
        if restrictions is None:
            active.append(page)
            continue
        if (
            not isinstance(restrictions, list)
            or not restrictions
            or any(not isinstance(item, str) or not item for item in restrictions)
            or len(set(restrictions)) != len(restrictions)
        ):
            raise ContractError(
                f"fixture page {page.get('ordinal')!r} has invalid scenario restrictions"
            )
        unknown = sorted(set(restrictions) - declared_scenarios)
        if unknown:
            raise ContractError(
                f"fixture page {page.get('ordinal')!r} names unknown scenario(s) {unknown}"
            )
        if scenario in restrictions:
            active.append(page)
    if not active:
        raise ContractError(f"fixture scenario {scenario!r} activates no pages")
    return active


def declared_digests(fixture: dict, scenario: str) -> dict[int, str]:
    """The digest each page is declared to have, per ordinal, for this scenario.

    A `page_refusal` row substitutes a declared digest the checked-in bytes cannot
    match, so the refusal scenarios exercise the door's real inspection path — the
    same comparison, the same refusal artifact — rather than any scenario-aware
    branch that a real door would not have.
    """
    declared = {
        page["ordinal"]: page["sha256"] for page in fixture_pages_for_scenario(fixture, scenario)
    }
    for row in fixture.get("page_refusal", []):
        if row["scenario"] != scenario:
            continue
        if row["ordinal"] not in declared:
            raise ContractError(
                f"page_refusal names ordinal {row['ordinal']}, which no declared page has"
            )
        declared[row["ordinal"]] = row["declared_sha256"]
    return declared


def _read_triage_document(path: str | Path, label: str) -> tuple[bytes, Any]:
    """Read one bounded regular file without following or reopening its path.

    Returns the same bytes parsed, so digest and decisions cannot straddle a
    rewrite; JSON objects refuse duplicate member names at parse time.

    These documents cross the pre-Door producer boundary and are parsed wholly in
    memory. A checked pathname is not an anchored input: its leaf can become a
    symlink or FIFO between a check and ``read_bytes()``, and an intermediate
    directory can redirect the same spelling. An unbounded regular file can make
    JSON parsing itself the denial of service. Open every component relative to
    its no-follow directory descriptor, then decide from the one leaf descriptor
    and prove its byte-bearing identity stayed stable across the bounded read.
    """
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory_flag is None:
        raise ContractError(
            f"the {label} cannot be read safely on a platform without no-follow opens"
        )
    components = Path(os.path.abspath(os.fspath(path))).parts
    if len(components) < 2:
        raise ContractError(f"the {label} is not a regular file")
    directory_flags = os.O_RDONLY | os.O_NONBLOCK | no_follow | directory_flag
    file_flags = os.O_RDONLY | os.O_NONBLOCK | no_follow
    parent_descriptors: list[int] = []
    try:
        parent = os.open(components[0], directory_flags)
        parent_descriptors.append(parent)
        for component in components[1:-1]:
            parent = os.open(component, directory_flags, dir_fd=parent)
            parent_descriptors.append(parent)
        descriptor = os.open(components[-1], file_flags, dir_fd=parent)
    except FileNotFoundError as error:
        # Keep the established exact refusal for an absent requested document.
        raise ContractError(f"the {label} could not be read") from error
    except OSError as error:
        raise ContractError(
            f"the {label} could not be opened as a regular file without following path redirects"
        ) from error
    finally:
        for parent_descriptor in reversed(parent_descriptors):
            os.close(parent_descriptor)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ContractError(f"the {label} is not a regular file")
        if before.st_size > MAX_TRIAGE_DOCUMENT_BYTES:
            raise ContractError(
                f"the {label} exceeds the {MAX_TRIAGE_DOCUMENT_BYTES}-byte document bound"
            )
        raw = handle.read(MAX_TRIAGE_DOCUMENT_BYTES + 1)
        after = os.fstat(handle.fileno())
    if len(raw) > MAX_TRIAGE_DOCUMENT_BYTES:
        raise ContractError(
            f"the {label} exceeds the {MAX_TRIAGE_DOCUMENT_BYTES}-byte document bound"
        )
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        before.st_nlink,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        after.st_nlink,
    )
    if before_identity != after_identity:
        raise ContractError(f"the {label} changed while it was being read")
    try:
        return raw, json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ContractError(
            f"the {label} is not valid UTF-8 JSON; no run was created because its decisions "
            "cannot be interpreted; export valid UTF-8 JSON and retry"
        ) from error


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Materialize one JSON object only when every member name occurs once."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object member {key!r}")
        result[key] = value
    return result


def load_triage_decisions(
    manifest_path: str | Path,
    clusters_path: str | Path | None = None,
    producer_recipe_path: str | Path | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]:
    """Read the closed triage decision manifest without inventing another shape.

    The third return value is the byte digest of each document read, for the same
    reason `_real_bindings` digests every configuration it acts on: geometry that
    shaped a run's pixels is bound into `config_digest`, so a re-entry under a
    re-run triage pass is refused by name as a different run wearing an old
    id — rather than left to be caught incidentally, one ordinal at a time, by the
    run tree's write-once artifacts. Digested from the same bytes that were
    parsed, because two reads can straddle a rewrite.
    """
    manifest_bytes, document = _read_triage_document(manifest_path, "triage decision manifest")
    if clusters_path is not None:
        clusters_bytes, clusters_document = _read_triage_document(
            clusters_path, "triage re-shoot cluster records"
        )
    else:
        clusters_bytes = clusters_document = None
    if producer_recipe_path is not None:
        recipe_bytes, recipe_document = _read_triage_document(
            producer_recipe_path, "triage producer recipe"
        )
    else:
        recipe_bytes = recipe_document = None
    digests = {"triage-decision-manifest": digest_bytes(manifest_bytes)}
    if clusters_bytes is not None:
        digests["triage-re-shoot-clusters"] = digest_bytes(clusters_bytes)
    if recipe_bytes is not None:
        from operations.triage.instrument import validate_producer_recipe

        try:
            validate_producer_recipe(recipe_document)
        except ContractError as error:
            raise ContractError(f"the triage producer recipe is invalid: {error}") from error
        digests["triage-producer-recipe"] = digest_bytes(recipe_bytes)
    if clusters_document is not None:
        if not isinstance(clusters_document, dict):
            raise ContractError(
                "the triage re-shoot cluster records are not an object keyed by cluster id; "
                "no run was created because their memberships cannot be resolved; export the "
                "records as one JSON object keyed by cluster id and retry"
            )
        clusters = clusters_document
    else:
        clusters = None
    _refuse_triage_amplification(document, clusters)
    try:
        checked = triage_manifest.validate_manifest(document, clusters)
    except ContractError as error:
        raise ContractError(
            f"the triage decision manifest is invalid ({error}); no run was created because "
            "its page geometry cannot be trusted; correct the named manifest violation and retry"
        ) from error
    if recipe_document is None and any(
        row["actor"]["kind"] == "producer" for row in checked["records"]
    ):
        raise ContractError(
            "the triage decision manifest contains producer rows but no triage producer "
            "recipe was supplied"
        )
    rows: dict[str, dict[str, Any]] = {}
    for row in checked["records"]:
        digest = row["source_frame_sha256"]
        if digest in rows:
            raise ContractError(
                "the triage decision manifest names one submitted frame more than once"
            )
        rows[digest] = row
    return rows, dict(clusters or {}), digests


def _refuse_triage_amplification(document: Any, clusters: Any) -> None:
    """Bound split and cluster fan-out before Unit 5 validates attacker-sized lists."""
    if isinstance(document, dict) and isinstance(document.get("records"), list):
        derivative_pages = 0
        for row in document["records"]:
            split = row.get("split") if isinstance(row, dict) else None
            parts = split.get("parts") if isinstance(split, dict) else None
            if not isinstance(parts, list):
                continue
            derivative_pages += len(parts)
            if derivative_pages > MAX_TRIAGE_DERIVATIVE_PAGES:
                raise ContractError(
                    "the triage decision manifest declares more than "
                    f"{MAX_TRIAGE_DERIVATIVE_PAGES} derivative pages; no run was created because "
                    "attacker-controlled split counts must be bounded before pairwise geometry "
                    "validation and source expansion; export one configured shard and retry"
                )
    if isinstance(clusters, dict):
        member_references = 0
        for record in clusters.values():
            members = record.get("member_frame_sha256") if isinstance(record, dict) else None
            if not isinstance(members, list):
                continue
            member_references += len(members)
            if member_references > MAX_TRIAGE_DERIVATIVE_PAGES:
                raise ContractError(
                    "the triage re-shoot cluster records declare more than "
                    f"{MAX_TRIAGE_DERIVATIVE_PAGES} member references; no run was created because "
                    "attacker-controlled cluster counts must be bounded before set expansion; "
                    "export only the clusters for one configured shard and retry"
                )


def decide(
    data: bytes | None,
    source: SourceEntry,
    policy: dict[str, str],
    pdf_settings: render_config.PdfRenderSettings | None = None,
    *,
    source_digest: str | None = None,
    detected_format: str | None = None,
    opened_pdf: pdf_render.OpenPdf | None = None,
) -> _Decision:
    """Decide one raster or one source-container page by its actual bytes.

    ``data`` is present for ordinary rasters and synthetic PDFs. A real PDF is
    deliberately represented by an already-open PDFium document plus a digest
    streamed from the same anchored descriptor, so PDFium does not receive a
    whole-file bytes allocation or reopen a mutable pathname.
    """
    if pdf_settings is None:
        pdf_settings = render_config.load_pdf_render_settings(minimum_dpi=pdf_render.MIN_RENDER_DPI)
    if data is None:
        if source_digest is None:
            raise ValueError("a streamed decision needs its source digest")
        detected = detected_format or source.detected_format
        if detected is None:
            raise ValueError("a streamed decision needs its detected PDF format")
        whole_digest = source_digest
    else:
        detected = detected_format or sniff(data)
        whole_digest = source_digest or digest_bytes(data)
    verdict = admission.classify_detected_format(detected, policy)
    if source.declared_sha256 is not None and whole_digest != source.declared_sha256:
        return _Decision(
            "refused",
            admission.reason(
                RefusalReason.DIGEST_MISMATCH,
                f"computed {whole_digest}, but {source.declared_sha256} was declared",
            ),
            whole_digest,
            None,
            None,
        )
    if source.triage_row is not None:
        if data is None or source.triage_part_index is None:
            raise ValueError("a triage-derived page needs bytes and an exact split-part index")
        try:
            triage_manifest.verify_submitted_frame(source.triage_row, data)
            frame_index = source.source_frame_index or 0
            decoded = decode_raster(data, page_index=frame_index)
            frame = source.triage_row["frame"]
            if (decoded.width, decoded.height) != (frame["width"], frame["height"]):
                raise ContractError(
                    "triage row frame dimensions do not match the decoded submitted frame; this "
                    "page part was refused because applying those coordinates could omit or shift "
                    "source pixels; regenerate the row against the stored raster dimensions and "
                    "retry"
                )
            part = source.triage_row["split"]["parts"][source.triage_part_index]
            page_bytes, _geometry, contract = render_raster_page(data, frame_index, part)
        except triage_manifest.SchemaRefusal as error:
            return _Decision(
                "refused",
                admission.reason(RefusalReason.DIGEST_MISMATCH, str(error)),
                None,
                None,
                None,
            )
        except FormatRefusal as error:
            return _Decision(
                "refused",
                admission.reason(admission._refusal_code(error), str(error)),
                None,
                None,
                None,
            )
        except ContractError as error:
            return _Decision(
                "refused",
                admission.reason(RefusalReason.CORRUPT, str(error)),
                None,
                None,
                None,
            )
        backlink = triage_manifest.derivative_page_backlink(
            source.triage_row, source.triage_part_index
        )
        rendered_from = {
            "container_format": "triage-split-raster",
            "container_sha256": whole_digest,
            # This index selects a split part, not a page in the submitted raster.
            "container_page_index": source.container_page_index,
            "render_contract": {
                **contract,
                "derivative_page": {
                    "kind": SEALED_DERIVATIVE_PAGE_KIND,
                    "parent_frame_sha256": whole_digest,
                    "parent_frame_page_index": frame_index,
                    "triage_manifest_row": source.triage_row,
                    "triage_backlink": backlink,
                    "operation_order": source.triage_row["split"]["operation_order"],
                    "apply_recipe": {
                        "schema": "triage-raster-apply-v1",
                        "rotation_resample": "Pillow.Resampling.BICUBIC",
                        "rotation_fill": "Pillow-default-zero",
                        "rotation_expand": True,
                        "colour_conversion": "Pillow.Image.convert-direct-or-via-RGB",
                        "encoder": "common.imaging.encode_image_deterministic-v1",
                    },
                    "operations": [
                        {"operation": "split", "region": part["region"]},
                        {"operation": "crop", "bounds": part["crop_box"]},
                        {"operation": "deskew", "rotation": part["rotation"]},
                        {"operation": "convert", "colour_mode": part["colour_mode"]},
                    ],
                },
            },
        }
        checked = admission.inspect_source(page_bytes, declared_sha256=None, policy=policy)
        if checked.outcome != "admitted":
            return _Decision("refused", checked.reason, None, None, None)
        return _Decision(
            "admitted", None, checked.digest, page_bytes, checked.geometry, rendered_from
        )

    if source.container_page_index is None:
        if verdict == admission.RENDER_PAGES:
            return _Decision(
                "refused",
                admission.reason(
                    RefusalReason.UNSUPPORTED_VARIANT,
                    "a page container must be declared with a page index; this one carries none",
                ),
                whole_digest,
                None,
                None,
            )
        if data is None:
            raise ValueError("only a PDF container may be decided without its bytes")
        result = admission.inspect_source(
            data, declared_sha256=source.declared_sha256, policy=policy
        )
        return _Decision(
            result.outcome,
            result.reason,
            result.digest,
            data if result.outcome == "admitted" else None,
            result.geometry,
        )

    try:
        if detected == "pdf":
            if data is None and opened_pdf is None:
                raise ValueError("a streamed PDF has no open document")
            opened = opened_pdf or pdf_render.open_document(data)
            try:
                rendered = pdf_render.render_page(opened, source.container_page_index, pdf_settings)
            finally:
                if opened_pdf is None:
                    pdf_render.close_document(opened)
            page_bytes = rendered.png_bytes
            rendered_from = {
                "container_format": detected,
                "container_sha256": whole_digest,
                "container_page_index": source.container_page_index,
                "render_contract": rendered.contract,
            }
        else:
            if data is None:
                raise ValueError("only a PDF container may be decided without its bytes")
            page_count = count_raster_pages(data)
            if page_count == 1 and verdict != admission.RENDER_PAGES:
                raise ContractError(
                    "the Door's source expansion declared a page index for a "
                    f"single-frame {detected or 'unknown'} raster; this is pipeline "
                    "bookkeeping disagreement, not an unsupported source variant"
                )
            page_bytes, _geometry, contract = render_raster_page(data, source.container_page_index)
            rendered_from = {
                "container_format": detected,
                "container_sha256": whole_digest,
                "container_page_index": source.container_page_index,
                "render_contract": contract,
            }
    except pdf_render.PdfRefusal as error:
        return _Decision("refused", str(error), None, None, None)
    except FormatRefusal as error:
        return _Decision(
            "refused",
            admission.reason(admission._refusal_code(error), str(error)),
            None,
            None,
            None,
        )

    checked = admission.inspect_source(page_bytes, declared_sha256=None, policy=policy)
    if checked.outcome != "admitted":
        return _Decision(
            "refused",
            admission.reason(
                RefusalReason.CORRUPT,
                f"the rendered page did not itself admit: {checked.reason}",
            ),
            None,
            None,
            None,
        )
    return _Decision("admitted", None, checked.digest, page_bytes, checked.geometry, rendered_from)


def expand_sources(
    files: list[dict[str, Any]],
    read_bytes: Callable[[str], bytes],
    policy: dict[str, str],
    *,
    open_source: Callable[[str], Any] | None = None,
    triage_rows: Mapping[str, dict[str, Any]] | None = None,
    triage_clusters: Mapping[str, dict[str, Any]] | None = None,
) -> list[SourceEntry]:
    """Expand source containers and triage split decisions to stable ordinals.

    Counting inspects only enough to learn a page count; it does not produce page
    pixels.  Any source that cannot be read or counted still receives one ordinal,
    so the later decision can publish a named alarm rather than lose it.

    ``open_source`` is the real submission's descriptor-anchored opener
    (`operations.submit.inventory.open_submission_source`).  Counting is where the
    anchoring matters most: PDFium parses whatever it is given *before* any digest
    has been computed, so a pathname reopened here could be fanned out to the page
    ordinals of a document nobody submitted.  Each row's stream is opened and
    closed within its own row, so this pass holds one descriptor at a time rather
    than one per file in the folder.
    """
    if triage_clusters is not None and triage_rows is None:
        raise ContractError(
            "triage cluster records were supplied without a decision manifest; no ordinals "
            "were assigned because cluster evidence cannot be reconciled on its own; supply "
            "the matching triage decision manifest and retry"
        )
    _require_case_unique_paths(files)
    submitted_digests = {row["sha256"] for row in files}
    ordinal = 0
    sources: list[SourceEntry] = []
    for row in sorted(files, key=lambda item: item["relative_path"]):
        path, declared_sha256 = row["relative_path"], row["sha256"]
        declared_size = row.get("bytes")
        ledger_sha256 = row.get("ledger_sha256")
        triage_row = None
        if triage_rows is not None:
            triage_row = triage_rows.get(declared_sha256)
            if triage_row is None:
                raise ContractError(
                    "the triage decision manifest has no row for a submitted source frame; no "
                    "source expansion was returned because that frame would disappear from the "
                    "post-split census; regenerate the manifest with one row for every submitted "
                    "frame digest and retry"
                )
        if declared_size is not None and (
            not isinstance(declared_size, int)
            or isinstance(declared_size, bool)
            or declared_size < 0
        ):
            # No `path` in the message: `run_stage` prints every ContractError to
            # stderr, and a declared path is what the data-handling policy's logging
            # rule keeps out of exactly that channel.
            raise ContractError(
                "a submitted source declares no non-negative byte count; the source "
                "manifest names it by ordinal"
            )

        def append(
            container_page_index: int | None,
            detected_format: str | None,
            *,
            path: str = path,
            declared_sha256: str | None = declared_sha256,
            declared_size: int | None = declared_size,
            ledger_sha256: str | None = ledger_sha256,
            bound_triage_row: dict[str, Any] | None = triage_row,
            triage_part_index: int | None = None,
            source_frame_index: int | None = None,
            computed_sha256: str | None = None,
        ) -> None:
            """Bind row fields before the next loop iteration can reassign them."""
            nonlocal ordinal
            ordinal += 1
            sources.append(
                SourceEntry(
                    ordinal,
                    path,
                    declared_sha256,
                    container_page_index,
                    declared_size,
                    ledger_sha256,
                    detected_format,
                    bound_triage_row,
                    triage_part_index,
                    source_frame_index
                    if source_frame_index is not None
                    else (0 if bound_triage_row is not None else None),
                    computed_sha256,
                )
            )

        def append_refused_source(
            detected_format: str | None,
            bound_triage_row: dict[str, Any] | None = triage_row,
            computed_sha256: str | None = None,
        ) -> None:
            """Keep the manifest's declared post-split denominator on early failure."""
            if bound_triage_row is None:
                append(None, detected_format, computed_sha256=computed_sha256)
                return
            for part_index in range(len(bound_triage_row["split"]["parts"])):
                append(
                    part_index,
                    detected_format,
                    triage_part_index=part_index,
                    source_frame_index=0,
                    computed_sha256=computed_sha256,
                )

        data: bytes | None = None
        # None means this pass read no complete source and must not claim it did.
        computed: str | None = None
        try:
            if open_source is not None:
                # A real source is classified from the same descriptor-relative
                # opener used later for its digest and PDFium document.  A pathname
                # reconstructed from the ledger would have a replacement window.
                with open_source(path) as opened_source:
                    detected = _sniff_source_stream(opened_source.handle)
                    if detected == "pdf":
                        # Streamed PDFs are not loaded into `data`, but their
                        # inspected identity must exist before membership seals.
                        computed, _ = _source_digest_stream(opened_source.handle)
                    opened_source.assert_unchanged(expected_sha256=declared_sha256)
            else:
                # A caller with no anchored opener supplies bytes directly; there is
                # no second, path-based way to classify a source (the door hands
                # PDFium an open stream or nothing, never a reopenable pathname).
                detected = None
            if detected != "pdf":
                # Rasters remain byte-backed. Their existing bounded path is
                # intentionally unchanged; only a PDF container is handed to PDFium
                # as an open stream instead of being read into one bytes object.
                if declared_size is not None and declared_size > MAX_SOURCE_BYTES:
                    append_refused_source(detected)
                    continue
                data = read_bytes(path)
                detected = sniff(data)
        except (OSError, inventory.SubmissionInputError):
            append_refused_source(None)
            continue
        route = admission.classify_detected_format(detected, policy)
        if data is not None and len(data) > MAX_SOURCE_BYTES:
            append_refused_source(detected)
            continue
        if data is not None:
            # `read_bytes` returns only a prefix above the ceiling; never bind it
            # as though it identified the complete source.
            computed = digest_bytes(data)
        if triage_rows is not None:
            assert triage_row is not None
            if data is None or detected == "pdf":
                raise ContractError(
                    "a triage decision row names a source that is not one decodable raster frame; "
                    "no source expansion was returned because its geometry cannot be applied to "
                    "that source; remove the stale row or regenerate it against a single-frame "
                    "raster and retry"
                )
            try:
                frame_count = count_raster_pages(data)
            except FormatRefusal:
                # Geometry cannot be applied until the frame decodes, but the
                # manifest already declares how many pages this frame contributes.
                # Retain one refused ordinal per part so the immutable denominator
                # and configured post-split cap do not undercount a broken frame.
                append_refused_source(detected, computed_sha256=computed)
                continue
            if frame_count != 1:
                raise ContractError(
                    "a triage decision row names a multi-page raster container; no source "
                    "expansion was returned because one frame-space recipe cannot describe every "
                    "page; omit that container from the triage manifest and let the Door fan out "
                    "its pages"
                )
            for part_index in range(len(triage_row["split"]["parts"])):
                append(
                    part_index,
                    detected,
                    triage_part_index=part_index,
                    source_frame_index=0,
                    computed_sha256=computed,
                )
            continue
        try:
            if detected == "pdf" and open_source is not None:
                with open_source(path) as opened_source:
                    page_count = pdf_render.count_pages(opened_source.handle)
                    opened_source.assert_unchanged(expected_sha256=declared_sha256)
            else:
                page_count = (
                    pdf_render.count_pages(data) if detected == "pdf" else count_raster_pages(data)
                )
        except (pdf_render.PdfRefusal, FormatRefusal, inventory.SubmissionInputError, OSError):
            if route != admission.RENDER_PAGES:
                append(None, detected, computed_sha256=computed)
                continue
            append(0, detected, computed_sha256=computed)
            continue
        # PDF/TIFF are declared page containers even when there is one page. For
        # every other decoder-backed image, a reported multi-frame source is also
        # fanned out. Retaining an animation as one raster would silently drop all
        # but frame zero downstream; one-frame rasters retain their original bytes.
        if route != admission.RENDER_PAGES and page_count == 1:
            append(None, detected, computed_sha256=computed)
            continue
        for page_index in range(page_count):
            append(page_index, detected, computed_sha256=computed)
    if triage_rows is not None and triage_clusters is not None:
        named_clusters = {
            row["re_shoot_cluster_id"]
            for digest, row in triage_rows.items()
            if digest in submitted_digests and row["re_shoot_cluster_id"] is not None
        }
        for cluster_id in named_clusters:
            record = triage_clusters.get(cluster_id)
            if not isinstance(record, dict) or "member_frame_sha256" not in record:
                # Direct callers can bypass manifest validation; they still need a
                # named contract refusal rather than a bare missing-key exception.
                raise ContractError(
                    "a submitted frame names a re-shoot cluster with no supplied cluster record; "
                    "no source expansion was returned because the cluster cannot be reconciled; "
                    "supply the matching corpus-scoped cluster record and retry"
                )
            members = set(record["member_frame_sha256"])
            if not members <= submitted_digests:
                raise ContractError(
                    "a re-shoot cluster would cross this submitted shard; no source expansion was "
                    "returned because every member must remain visible together and no canonical "
                    "frame may be selected; submit every cluster member in the same shard and retry"
                )
    # Deliberately no refusal for rows naming frames outside this submission:
    # the decision manifest is corpus-scoped and a submission is one shard of
    # it (Unit 8's 1,000-page sharding), so rows for other shards are the
    # ordinary case, not lost evidence. The evidence guarantee runs in the
    # other direction twice: the producer proves exact coverage over what it
    # was handed, and this expansion refuses any submitted frame without a row.
    return sources


def _require_case_unique_paths(files: list[dict[str, Any]]) -> None:
    """Refuse names that alias on default case-insensitive APFS.

    A filename ledger is made on one host and may be admitted on another. Exact
    string uniqueness is therefore insufficient: ``Page.PNG`` and ``page.png``
    are two rows on a case-sensitive filesystem but one pathname on default APFS.
    If the Door accepted both, which bytes an ordinal named would depend on the
    host that happened to open it.
    """
    seen: set[str] = set()
    for row in files:
        path = row.get("relative_path") if isinstance(row, dict) else None
        if not isinstance(path, str) or not path:
            raise ContractError(
                "a submitted source has no non-empty declared path; no source expansion was "
                "returned because every ordinal must name one portable file"
            )
        portable = path.casefold()
        if portable in seen:
            raise ContractError(
                "the submitted source manifest has case-variant path collisions; no source "
                "expansion was returned because those rows alias on default APFS and cannot "
                "name portable evidence uniquely"
            )
        seen.add(portable)


def content_aware_shards(
    sources: list[SourceEntry], *, max_pages_per_shard: int, max_shards: int | None = None
) -> list[list[SourceEntry]]:
    """Choose only seams that keep a split pair and re-shoot cluster whole.

    This is intentionally a planning function: a caller selects the resulting
    source-manifest shard *before* creating each RunTree.  Cutting after a run
    exists would change its immutable denominator.

    The page cap is sealed policy (`config/corpus_frame.toml`, checked at
    `require_corpus_frame_shard`); shard count is a consequence, not an implicit
    second ceiling. A caller with its own ceiling must pass ``max_shards``.
    """
    if (
        not isinstance(max_pages_per_shard, int)
        or isinstance(max_pages_per_shard, bool)
        or max_pages_per_shard < 1
        or (
            max_shards is not None
            and (not isinstance(max_shards, int) or isinstance(max_shards, bool) or max_shards < 1)
        )
    ):
        raise ContractError(
            "content-aware sharding received a non-positive or non-integer page or shard limit; "
            "no shard plan was returned because slice boundaries must be exact page counts; "
            "pass positive integer limits and retry"
        )
    ordered = sorted(sources, key=lambda source: source.ordinal)
    if not ordered:
        raise ContractError(
            "content-aware sharding received no submitted pages; no shard plan was returned "
            "because an empty plan would hide an empty submission; supply a non-empty post-split "
            "page census and retry"
        )
    blocked: set[int] = set()
    # A split fan-out is adjacent by construction; do not place a seam after
    # its first (or any non-final) part.  A cluster may not be adjacent, so every
    # boundary between its first and final member is blocked as well.
    for left, right in zip(ordered, ordered[1:], strict=False):
        if (
            left.triage_row is not None
            and right.triage_row is not None
            and left.declared_path == right.declared_path
            and left.declared_sha256 == right.declared_sha256
            and left.triage_part_index is not None
            and right.triage_part_index is not None
        ):
            blocked.add(left.ordinal)
    clusters: dict[str, list[int]] = {}
    for source in ordered:
        if source.triage_row is None:
            continue
        cluster_id = source.triage_row["re_shoot_cluster_id"]
        if cluster_id is not None:
            clusters.setdefault(cluster_id, []).append(source.ordinal)
    for ordinals in clusters.values():
        blocked.update(range(min(ordinals), max(ordinals)))
    shards: list[list[SourceEntry]] = []
    start = 0
    while start < len(ordered):
        end = min(start + max_pages_per_shard, len(ordered))
        if end < len(ordered):
            while end > start and ordered[end - 1].ordinal in blocked:
                end -= 1
            if end == start:
                raise ContractError(
                    "content-aware shard refusal: every legal seam within the configured "
                    "page cap would cut a split pair or re-shoot cluster; no shard plan was "
                    "returned because those units must remain whole; place the whole unit in a "
                    "shard within the sealed cap, or stop for Tyrel if the cap itself conflicts"
                )
        shards.append(ordered[start:end])
        start = end
    if max_shards is not None and len(shards) > max_shards:
        raise ContractError(
            "content-aware shard refusal: the configured shard count is exhausted without "
            "cutting a split pair or re-shoot cluster; no shard plan was returned because the "
            "caller ceiling cannot be met honestly; remove or increase that caller-supplied "
            "ceiling and retry"
        )
    return shards


def process_sources(
    context: StageContext,
    tree: RunTree,
    sources: list[SourceEntry],
    read_bytes: Callable[[str], bytes],
    *,
    policy: dict[str, str],
    pdf_settings: render_config.PdfRenderSettings | None = None,
    open_source: Callable[[str], Any] | None = None,
) -> int:
    """Admit or refuse every declared source. Returns the count admitted.

    `read_bytes` is called once per distinct raster path within this call. A real
    PDF is different: its digest is streamed once, then PDFium holds one native
    descriptor-anchored document handle while every fanned page renders from it.
    This lets a reel be larger than available Python memory without losing its
    page ordinals or filename ledger link, and prevents a pathname replacement
    from separating the digest from the pixels that are sealed.

    Per-file, never per-folder (harvest #2): one unreadable or refused source does
    not stop the rest from being decided. Byte-identical pages within one PDF remain
    distinct pages. A second source path with the same bytes is admitted under its
    own ordinal and records the first path as a duplicate fact; it never loses a
    citation link merely because its blob is already content-addressed.
    """
    if pdf_settings is None:
        pdf_settings = render_config.load_pdf_render_settings(minimum_dpi=pdf_render.MIN_RENDER_DPI)
    admitted = 0
    seen_sources: dict[str, tuple[str, int]] = {}
    # One entry, never a growing map. `expand_sources` assigns ordinals row by row,
    # so every ordinal of one declared path is contiguous in this sorted iteration
    # and a single slot avoids the same re-reads a full cache did. A full cache
    # retained every distinct raster body for the whole call, so peak memory grew
    # with the number of raster sources in the submission rather than with the
    # largest one — the opposite of the guarantee the docstring above makes for a
    # reel, granted for PDFs and then given back on the raster path.
    cached_path: str | None = None
    cached_data: bytes | None = None
    # `expand_sources()` assigns all page ordinals from one container together.
    # Keep exactly that one PDF stream/document alive while those pages render,
    # rather than retaining a descriptor for every PDF in a large submission.
    active_pdf_key: str | None = None
    active_pdf_digest: tuple[str, int] | None = None
    active_pdf_document: pdf_render.OpenPdf | None = None
    active_opened_source: inventory.OpenedSubmissionSource | None = None
    active_context: ExitStack | None = None

    def close_active_pdf() -> None:
        nonlocal active_pdf_key
        nonlocal active_pdf_digest
        nonlocal active_pdf_document
        nonlocal active_opened_source
        nonlocal active_context
        # Taken and cleared before anything can raise. These used to be cleared
        # after the try/finally, so a failing close left them set: the loop's outer
        # `finally` then called this again, closed the same native handle a second
        # time, and that second failure replaced the first as the raised exception —
        # the operator read a duplicate-close message instead of the resource failure
        # that actually happened. The run failed loudly either way; it named the
        # wrong cause.
        document, context_stack = active_pdf_document, active_context
        active_pdf_key = None
        active_pdf_digest = None
        active_pdf_document = None
        active_opened_source = None
        active_context = None
        try:
            if document is not None:
                try:
                    pdf_render.close_document(document)
                except pdf_render.PdfRefusal as error:
                    # A native document that cannot be released is a pipeline
                    # resource failure, not a property of one page. Stop loudly
                    # without a traceback or a green stage completion.
                    raise ContractError(str(error)) from error
        finally:
            # The stream outlives the document by construction, so it is released
            # second — and in a `finally`, because a document that fails to close
            # must not also strand the descriptor it was reading through.
            if context_stack is not None:
                context_stack.close()

    try:
        for source in sorted(sources, key=lambda item: item.ordinal):
            streamed_pdf = open_source is not None and source.detected_format == "pdf"
            source_key = source.declared_path
            if active_pdf_key is not None and (not streamed_pdf or source_key != active_pdf_key):
                close_active_pdf()
            if (
                source.declared_size is not None
                and source.declared_size > MAX_SOURCE_BYTES
                and not streamed_pdf
            ):
                # The raster path still receives bytes today, so retaining its
                # allocation guard is honest. A streamed PDF does not allocate
                # those bytes and must not be refused by a cap that guarded a
                # retired allocation.
                _publish(
                    context,
                    source,
                    outcome="refused",
                    reason=admission.reason(
                        RefusalReason.TOO_LARGE, admission.too_large_detail(source.declared_size)
                    ),
                )
                continue

            data: bytes | None = None
            if streamed_pdf:
                try:
                    if active_pdf_key is None:
                        assert open_source is not None  # narrowed by streamed_pdf
                        candidate_context = ExitStack()
                        try:
                            candidate_source = candidate_context.enter_context(
                                open_source(source.declared_path)
                            )
                            actual_digest, actual_size = _source_digest_stream(
                                candidate_source.handle
                            )
                            candidate_source.assert_unchanged(
                                expected_sha256=source.declared_sha256
                            )
                        except BaseException:
                            candidate_context.close()
                            raise
                        active_pdf_key = source_key
                        active_pdf_digest = (actual_digest, actual_size)
                        active_opened_source = candidate_source
                        active_context = candidate_context
                    assert active_pdf_digest is not None  # set with active_pdf_key
                    actual_digest, actual_size = active_pdf_digest
                except (OSError, inventory.SubmissionInputError) as error:
                    _publish(
                        context,
                        source,
                        outcome="refused",
                        reason=admission.reason(RefusalReason.UNREADABLE, str(error)),
                    )
                    continue
            else:
                try:
                    if cached_path == source.declared_path and cached_data is not None:
                        data = cached_data
                    else:
                        data = read_bytes(source.declared_path)
                        cached_path, cached_data = source.declared_path, data
                except (OSError, inventory.SubmissionInputError) as error:
                    _publish(
                        context,
                        source,
                        outcome="refused",
                        reason=admission.reason(RefusalReason.UNREADABLE, str(error)),
                    )
                    continue
                actual_digest, actual_size = digest_bytes(data), len(data)

            if source.declared_size is not None and actual_size != source.declared_size:
                _publish(
                    context,
                    source,
                    outcome="refused",
                    reason=admission.reason(
                        RefusalReason.DIGEST_MISMATCH,
                        f"the source now has {actual_size} bytes, but {source.declared_size} bytes "
                        "were recorded in its filename ledger",
                    ),
                )
                continue

            if source.computed_sha256 is not None and actual_digest != source.computed_sha256:
                _publish(
                    context,
                    source,
                    outcome="refused",
                    reason=admission.reason(
                        RefusalReason.DIGEST_MISMATCH,
                        f"computed {actual_digest} at admission, but shard membership was "
                        f"sealed from {source.computed_sha256} during source expansion",
                    ),
                )
                continue

            if source.declared_sha256 is not None and actual_digest != source.declared_sha256:
                _publish(
                    context,
                    source,
                    outcome="refused",
                    reason=admission.reason(
                        RefusalReason.DIGEST_MISMATCH,
                        f"computed {actual_digest}, but {source.declared_sha256} was declared",
                    ),
                )
                continue

            opened_pdf = None
            if streamed_pdf:
                try:
                    if active_pdf_document is None:
                        assert active_opened_source is not None
                        active_pdf_document = pdf_render.open_document(active_opened_source.handle)
                    opened_pdf = active_pdf_document
                except pdf_render.PdfRefusal as error:
                    decision = _Decision("refused", str(error), None, None, None)
                except (OSError, inventory.SubmissionInputError) as error:
                    # Per-file, never per-folder. A descriptor that dies between
                    # the digest and the document open is this one source's named
                    # alarm; letting it escape would abandon every source after it.
                    decision = _Decision(
                        "refused",
                        admission.reason(RefusalReason.UNREADABLE, str(error)),
                        None,
                        None,
                        None,
                    )
                else:
                    decision = decide(
                        None,
                        source,
                        policy,
                        pdf_settings,
                        source_digest=actual_digest,
                        detected_format="pdf",
                        opened_pdf=opened_pdf,
                    )
            else:
                decision = decide(data, source, policy, pdf_settings, source_digest=actual_digest)

            if decision.outcome == "refused":
                _publish(
                    context,
                    source,
                    outcome="refused",
                    reason=decision.reason,
                )
                continue

            if streamed_pdf:
                try:
                    assert active_opened_source is not None
                    active_opened_source.assert_unchanged(expected_sha256=source.declared_sha256)
                except inventory.SubmissionInputError as error:
                    _publish(
                        context,
                        source,
                        outcome="refused",
                        reason=admission.reason(RefusalReason.DIGEST_MISMATCH, str(error)),
                    )
                    continue

            # Register only an admitted source. A corrupt twin needs its own
            # corruption alarm rather than a duplicate claim about a source whose
            # pixels never entered the Exemplar. The second *valid* filename now
            # remains admitted, and this immutable fact makes the duplicate visible
            # without discarding its citation link.
            first = seen_sources.get(actual_digest)
            duplicate_of = None
            if first is not None and first[0] != source.declared_path:
                duplicate_of = {
                    "first_declared_path": first[0],
                    "first_ordinal": first[1],
                    "source_sha256": actual_digest,
                }
            seen_sources.setdefault(actual_digest, (source.declared_path, source.ordinal))
            _, published = tree.put_blob(DOOR, decision.store_bytes)
            inputs = [context.input_ref(published.relative_path)]
            extra: dict[str, Any] = {
                "sha256": decision.digest,
                # The digest of the submitted source file, as this door computed it.
                # For an ordinary raster this equals `sha256` above and, when the
                # ledger declared one, `declared_sha256` too -- the digest check
                # earlier in this loop refuses before `decide()` runs otherwise. The
                # three names genuinely diverge only for a rendered PDF/TIFF page,
                # where `sha256` is the *render's* digest rather than the source's.
                # Duplicate accounting groups on this field because the door always
                # knows it for anything admitted, render or not.
                "admitted_source_sha256": actual_digest,
                "stored_at": published.relative_path,
                "geometry": {"width": decision.geometry[0], "height": decision.geometry[1]},
            }
            if decision.rendered_from is not None:
                extra["rendered_from"] = decision.rendered_from
            if source.triage_row is not None:
                # The master must remain addressable independently so the sealed
                # derivative can be re-applied from its exact source bytes.
                assert data is not None
                _, parent = tree.put_blob(DOOR, data)
                extra["parent_frame"] = {
                    "sha256": actual_digest,
                    "stored_at": parent.relative_path,
                    "source_frame_index": source.source_frame_index or 0,
                }
                # Content addressing can make the derivative and master the same
                # blob: a full-frame, zero-rotation ``keep`` decision over a PNG
                # already written by the deterministic encoder is a real example.
                # One reference then proves both roles. Publishing it twice would
                # violate the envelope's no-duplicate-input contract and turn an
                # exact no-op into a fatal artifact error.
                if parent.relative_path != published.relative_path:
                    inputs.append(context.input_ref(parent.relative_path))
            if duplicate_of is not None:
                extra["duplicate_of"] = duplicate_of
            _publish(
                context,
                source,
                outcome="admitted",
                payload_extra=extra,
                inputs=inputs,
            )
            admitted += 1
    except BaseException as primary:
        # Cleanup must not replace a security refusal already in flight. A native
        # close failure still remains visible on the primary exception, while the
        # refusal that stopped admission keeps its type, message, and control flow.
        try:
            close_active_pdf()
        except BaseException as cleanup:
            primary.add_note(f"PDF cleanup also failed: {cleanup}")
        raise
    else:
        close_active_pdf()

    return admitted


def _publish(
    context: StageContext,
    source: SourceEntry,
    *,
    outcome: str,
    reason: str | None = None,
    payload_extra: dict | None = None,
    inputs: list[dict[str, str]] | None = None,
) -> None:
    payload: dict = {
        "declared_path": source.declared_path,
        "declared_sha256": source.declared_sha256,
        "ordinal": source.ordinal,
    }
    if source.declared_size is not None:
        payload["declared_bytes"] = source.declared_size
    if source.ledger_sha256 is not None:
        payload["ledger_sha256"] = source.ledger_sha256
    if source.triage_row is not None:
        row = source.triage_row
        split = row.get("split")
        parts = split.get("parts") if isinstance(split, dict) else None
        if (
            not isinstance(parts, list)
            or not parts
            or not isinstance(source.triage_part_index, int)
            or isinstance(source.triage_part_index, bool)
        ):
            raise ContractError("a triage admission has no declared split-part identity")
        backlink = triage_manifest.derivative_page_backlink(row, source.triage_part_index)
        # A refused page has no derivative contract, so it needs this sibling link
        # for its re-shoot membership to remain reportable.
        payload["triage_link"] = {
            "schema": "door-triage-admission-link.v0",
            **backlink,
            "declared_split_part_count": len(parts),
            "re_shoot_cluster_id": row.get("re_shoot_cluster_id"),
        }
    if outcome == "refused":
        payload["reason"] = reason
    else:
        payload.update(payload_extra or {})
    context.publish(
        kind="admission",
        subject_id=f"source-{source.ordinal}",
        outcome=outcome,
        inputs=inputs or [],
        payload=payload,
    )


def _iter_admissions(context: StageContext, outcome: str):
    """Every published admission artifact with the given outcome, entry and payload."""
    for entry in context.tree.build_manifest(DOOR)["artifacts"]:
        if entry["kind"] != "admission" or entry["outcome"] != outcome:
            continue
        record = context.tree.read_artifact(DOOR, "admission", entry["artifact_id"])
        yield entry, record["payload"]


def publish_refusal_report(context: StageContext) -> str | None:
    """Seal every door alarm into one private, filename-bearing report.

    The per-source admission artifacts remain the authority.  This report is their
    self-hashed, input-referenced index for an operator who needs a named list
    without putting filenames or image bytes into terminal output.  It is an
    ordinary run-tree artifact, not a sixth on-disk file shape.
    """
    rows: list[dict[str, Any]] = []
    inputs: list[dict[str, str]] = []
    for entry, payload in _iter_admissions(context, "refused"):
        ordinal, path, refusal = (
            payload.get("ordinal"),
            payload.get("declared_path"),
            payload.get("reason"),
        )
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise ContractError("a refused door admission has no integer source ordinal")
        if not isinstance(path, str) or not path:
            raise ContractError("a refused door admission has no declared filename")
        # Reading the closed code back is what prevents a free-text report from
        # turning a producer bug into the operator's only explanation.
        admission.reason_code(refusal)
        rows.append({"ordinal": ordinal, "declared_path": path, "reason": refusal})
        inputs.append({"relative_path": entry["relative_path"], "sha256": entry["sha256"]})
    if not rows:
        return None
    payload: dict[str, Any] = {
        "schema": DOOR_REFUSAL_REPORT_SCHEMA,
        "refusals": sorted(rows, key=lambda row: row["ordinal"]),
    }
    payload["self_hash"] = self_hash(payload)
    published = context.publish(
        kind="refusal-report",
        subject_id=DOOR_REFUSAL_REPORT_SUBJECT,
        outcome="refused",
        inputs=inputs,
        payload=payload,
    )
    return published.relative_path


def publish_duplicate_report(context: StageContext) -> str | None:
    """Seal an operator-readable duplicate fact without refusing either source.

    The admission records remain per ordinal. This companion record groups every
    admitted source path that shares one submitted digest, names the first observed
    filename and ordinal, and makes the changed denominator inspectable without
    asking a later stage to rediscover it from blobs.
    """
    grouped: dict[str, list[tuple[int, str, dict[str, str]]]] = {}
    for entry, payload in _iter_admissions(context, "admitted"):
        ordinal = payload.get("ordinal")
        path = payload.get("declared_path")
        # Not `declared_sha256`: that one is optional on a `SourceEntry`, so grouping
        # on it made a legal admission fatal here — and fatal *after* the run had
        # already published every admission, which is the worst moment to discover it.
        # The door computes this one for everything it admits, so its absence really
        # is a contract breach and stays loud rather than being skipped (GOVERNANCE 2).
        source_digest = payload.get("admitted_source_sha256")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise ContractError(
                "an admitted door source has no integer ordinal for duplicate accounting"
            )
        if not isinstance(path, str) or not path:
            raise ContractError(
                "an admitted door source has no declared filename for duplicate accounting"
            )
        if not isinstance(source_digest, str) or len(source_digest) != 64:
            raise ContractError(
                "an admitted door source has no source digest for duplicate accounting"
            )
        grouped.setdefault(source_digest, []).append(
            (ordinal, path, {"relative_path": entry["relative_path"], "sha256": entry["sha256"]})
        )

    groups: list[dict[str, Any]] = []
    inputs: list[dict[str, str]] = []
    duplicate_sources = duplicate_ordinals = 0
    for source_digest, rows in sorted(grouped.items()):
        by_path: dict[str, list[tuple[int, dict[str, str]]]] = {}
        for ordinal, path, reference in rows:
            by_path.setdefault(path, []).append((ordinal, reference))
        if len(by_path) < 2:
            continue
        ordered_paths = sorted(
            by_path, key=lambda path: min(ordinal for ordinal, _ in by_path[path])
        )
        first_path = ordered_paths[0]
        first_ordinal = min(ordinal for ordinal, _ in by_path[first_path])
        sources = [
            {
                "declared_path": path,
                "ordinals": sorted(ordinal for ordinal, _ in by_path[path]),
            }
            for path in ordered_paths
        ]
        groups.append(
            {
                "source_sha256": source_digest,
                "first_declared_path": first_path,
                "first_ordinal": first_ordinal,
                "sources": sources,
            }
        )
        duplicate_sources += len(sources) - 1
        duplicate_ordinals += sum(len(source["ordinals"]) for source in sources[1:])
        inputs.extend(reference for _ordinal, reference in by_path[first_path])
        for path in ordered_paths[1:]:
            inputs.extend(reference for _ordinal, reference in by_path[path])

    if not groups:
        return None
    payload: dict[str, Any] = {
        "schema": DOOR_DUPLICATE_REPORT_SCHEMA,
        "duplicate_source_count": duplicate_sources,
        "duplicate_ordinal_count": duplicate_ordinals,
        "groups": groups,
    }
    payload["self_hash"] = self_hash(payload)
    published = context.publish(
        kind="duplicate-report",
        subject_id=DOOR_DUPLICATE_REPORT_SUBJECT,
        outcome="admitted",
        inputs=inputs,
        payload=payload,
    )
    return published.relative_path


def publish_cluster_report(context: StageContext) -> str | None:
    """Carry corpus-scoped re-shoot links into the sealed run.

    Unlike a duplicate report, this never calls one member canonical: every
    submitted member remains an admission and the record makes that fact visible
    to an operator without asking a later stage to reconstruct it from geometry.
    """
    groups: dict[str, dict[str, Any]] = {}
    inputs: list[dict[str, str]] = []
    for outcome in ("admitted", "refused"):
        for entry, payload in _iter_admissions(context, outcome):
            link = payload.get("triage_link")
            if not isinstance(link, dict) or link.get("re_shoot_cluster_id") is None:
                continue
            cluster_id = link["re_shoot_cluster_id"]
            group = groups.setdefault(
                cluster_id,
                {"corpus_id": link["corpus_id"], "cluster_id": cluster_id, "members": {}},
            )
            if group["corpus_id"] != link["corpus_id"]:
                raise ContractError("a re-shoot cluster id is reused across two corpora")
            member = group["members"].setdefault(
                link["source_frame_sha256"],
                {
                    "source_frame_sha256": link["source_frame_sha256"],
                    "triage_manifest_row_sha256": link["triage_manifest_row_sha256"],
                    "declared_split_part_count": link["declared_split_part_count"],
                    "pages": [],
                },
            )
            expected_member = {
                "source_frame_sha256": link["source_frame_sha256"],
                "triage_manifest_row_sha256": link["triage_manifest_row_sha256"],
                "declared_split_part_count": link["declared_split_part_count"],
            }
            if any(member.get(field) != value for field, value in expected_member.items()):
                raise ContractError("a re-shoot cluster member carries contradictory triage links")
            member["pages"].append(
                {
                    "ordinal": payload["ordinal"],
                    "triage_part_index": link["triage_part_index"],
                    "outcome": outcome,
                }
            )
            inputs.append({"relative_path": entry["relative_path"], "sha256": entry["sha256"]})
    if not groups:
        return None
    payload = {
        "schema": DOOR_CLUSTER_REPORT_SCHEMA,
        "clusters": [
            {
                "corpus_id": group["corpus_id"],
                "cluster_id": group["cluster_id"],
                "members": sorted(
                    (
                        {
                            **member,
                            "pages": sorted(member["pages"], key=lambda page: page["ordinal"]),
                        }
                        for member in group["members"].values()
                    ),
                    key=lambda member: member["source_frame_sha256"],
                ),
            }
            for _cluster_id, group in sorted(groups.items())
        ],
    }
    payload["self_hash"] = self_hash(payload)
    published = context.publish(
        kind="re-shoot-cluster-report",
        subject_id="re-shoot-cluster-report",
        outcome="admitted",
        inputs=inputs,
        payload=payload,
    )
    return published.relative_path


def require_some_admitted(admitted: int, tree: RunTree, refusal_report: str | None) -> None:
    """An empty or wholly refused input set is a loud failure (harvest #3).

    The terminal carries the count and private report location, while the report
    itself names every source and reason. This preserves filenames as citation links
    without placing them in a captured terminal stream.
    """
    if admitted != 0:
        return
    total, census = _refusal_census(tree)
    named = ", ".join(f"{code}: {count}" for code, count in sorted(census.items()))
    raise ContractError(
        f"the door admitted nothing: {total} source(s) submitted, "
        f"{sum(census.values())} refused ({named or 'no refusal was recorded either'}). "
        f"Private named refusal report: {refusal_report or 'unavailable'}. "
        "An empty or wholly unreadable input set is a loud failure, never a green run with no "
        "output (harvest #3)"
    )


def _refusal_census(tree: RunTree) -> tuple[int, dict[str, int]]:
    """Count the published refusals by closed-set reason code.

    **Nothing in here may raise.** It runs only on the failure path, to describe a
    failure that has already happened, and an exception from reading a damaged
    artifact would replace "the door admitted nothing" with something about JSON —
    the primary failure masked by a secondary one, which is a worse answer to
    GOVERNANCE 2 than a partial census. So an artifact that cannot be read or whose
    reason is outside the closed set is counted under a name that says so, and the
    loud failure still says what it is.
    """
    census: dict[str, int] = {}
    total = 0
    try:
        entries = tree.build_manifest(DOOR)["artifacts"]
    except (OSError, ValueError, ContractError):
        return 0, {"the door's own census could not be read": 1}
    for entry in entries:
        if entry.get("kind") != "admission":
            continue
        total += 1
        try:
            record = json.loads(tree.read_bytes(entry["relative_path"]).decode("utf-8"))
            if record["outcome"] != "refused":
                continue
            code = admission.reason_code(record["payload"].get("reason")).value
        # TypeError belongs here with the rest: a damaged artifact that decodes to a
        # JSON list, string or number makes `record["outcome"]` raise it, and this
        # function's whole contract is that it never replaces the primary failure
        # with a secondary one about JSON.
        except (OSError, TypeError, ValueError, KeyError, ContractError):
            code = "unreadable record"
        census[code] = census.get(code, 0) + 1
    return total, census


def declared_synthetic_fixture_root(requested_root: str) -> Path:
    """The one root in this repository whose contents are declared synthetic.

    Ruling 2026-08-04, item 1: fixture status comes from the declared fixture
    manifest, never from a caller flag, a filename suffix, or a folder name. A
    caller pointing `--fixture-root` at its own directory is pointing at real
    input, and this is what says so instead of believing it.
    """
    try:
        candidate = Path(requested_root).resolve(strict=True)
    except OSError as error:
        raise ContractError(
            f"the declared synthetic fixture root {requested_root!r} could not be resolved"
        ) from error
    if candidate != DECLARED_SYNTHETIC_FIXTURE_ROOT.resolve():
        raise ContractError(
            f"{requested_root!r} is not the declared synthetic fixture root "
            f"({DECLARED_SYNTHETIC_FIXTURE_ROOT}); a caller-owned folder is real input "
            "and goes through --submission-folder, where the data-handling gate is"
        )
    return candidate


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Create the run with an explicitly supplied chair implementation.

    The command-line default is the production registry. Tests supply an
    independent deterministic implementation through this seam; no command-line
    option chooses among implementations, chairs, revisions, recipes, or caches.
    """
    parser = stage_parser(__doc__.splitlines()[0])
    parser.add_argument(
        "--submission-folder",
        help="a real local submission; must live inside an approved storage root",
    )
    parser.add_argument(
        "--submission-manifest",
        help=(
            "self-hashed local filename ledger made before transfer; required with a real "
            "submission folder"
        ),
    )
    parser.add_argument(
        "--data-gate-policy",
        default=str(gate.DEFAULT_POLICY_PATH),
        help="the data-handling policy naming this run's approved storage locations",
    )
    parser.add_argument(
        "--triage-decision-manifest",
        help="Unit 5 triage-decision-manifest-v1 controlling raster split/crop/rotation",
    )
    parser.add_argument(
        "--triage-clusters",
        help="corpus-scoped Unit 5 re-shoot cluster records keyed by cluster id",
    )
    parser.add_argument(
        "--triage-producer-recipe",
        help="sealed triage-producer-recipe.v1 for the pre-door producer run",
    )
    args = parser.parse_args()
    registry = registry_factory(args.models_config)

    if args.submission_folder is not None:
        # The run-level cap is applied inside `real_submission`, once the run root
        # has passed the storage gate. Reading a run authority here would open and
        # self-hash a file in a directory the data-handling policy never approved
        # — the exact read the gate exists to stop — and an operator who mistyped
        # the run root onto an unapproved volume holding a run.json would be told
        # the run was halted rather than that the root is not approved.
        return real_submission(args, registry)
    # The fixture path is declared synthetic input and is not gated, so its cap
    # check has no earlier gate to stand behind.
    _refuse_halted_run_root(Path(args.run_root), args)
    if args.submission_manifest is not None:
        raise ContractError(
            "a submission filename ledger is meaningful only with a real submission folder; "
            "the walking skeleton's declared synthetic pages are not gated input"
        )
    if (
        args.triage_decision_manifest is not None
        or args.triage_clusters is not None
        or args.triage_producer_recipe is not None
    ):
        raise ContractError("triage geometry is meaningful only with a real submission folder")
    return fixture_submission(args, registry)


def _refuse_halted_run_root(run_root: Path, args) -> None:
    """Apply the sealed run-level hard-failure cap to an existing run tree.

    Called on the fixture path before anything is written, and on the real path
    only after `run_root` has passed the approved-storage gate — reading a run
    authority is a read, and a read outside an approved location is what the gate
    is for.
    """
    existing_tree = RunTree(run_root, args.run_id)
    if existing_tree.resolve("run.json").exists():
        refuse_halted_run(existing_tree, DOOR, args.hard_failure_config)


def _load_pdf_render_binding(args) -> render_config.PdfRenderBinding:
    """The one place a run's PDF target DPI is resolved, fixture or real.

    One read, returning the settings and the digest of the bytes they were parsed
    from. The door used to resolve the settings here and then let the binding step
    open `pdf_render.toml` again for its digest, so a rewrite between the two
    reads sealed a run whose `render_settings` recorded one target while its
    `config_digest` bound the bytes of another — a run claiming a configuration it
    did not execute (audit S6). The digest travels into `config_digest` and into
    the run's `sealed_config_digests`, and the door proves at its point of use
    that the settings it renders with are the ones the run sealed.
    """
    return render_config.load_pdf_render_binding(
        Path(args.pdf_render_config),
        target_override=args.pdf_target_dpi,
        minimum_dpi=pdf_render.MIN_RENDER_DPI,
    )


def _finish_door_run(context: StageContext, tree: RunTree, admitted: int) -> int:
    """The one shared close for both entry points: reports, then the loud check."""
    refusal_report = publish_refusal_report(context)
    duplicate_report = publish_duplicate_report(context)
    publish_cluster_report(context)
    _announce_refusal_report(tree, refusal_report)
    _announce_duplicate_report(tree, duplicate_report)
    require_some_admitted(admitted, tree, refusal_report)
    context.seal_boundary()
    context.finish(DOOR)
    return EXIT_COMPLETE


def fixture_submission(args, registry) -> int:
    """The walking skeleton: declared synthetic pages, no gate, sealed as such."""
    fixture_root = declared_synthetic_fixture_root(args.fixture_root)
    fixture = load_fixture(str(fixture_root))
    pages = fixture_pages_for_scenario(fixture, args.scenario)
    declared = declared_digests(fixture, args.scenario)
    policy = admission.load_format_policy()
    pdf_render_binding = _load_pdf_render_binding(args)
    pdf_settings = pdf_render_binding.settings
    bindings = run_config_bindings(
        registry.config,
        fixture,
        args.scenario,
        pdf_render_config_path=args.pdf_render_config,
        pdf_render_config_sha256=pdf_render_binding.config_sha256,
        designator_padding_config_path=args.designator_padding_config,
        designator_geometry_config_path=args.designator_geometry_config,
        alignment_config_path=args.alignment_config,
        pdf_target_dpi=args.pdf_target_dpi,
        armarium_formats_config_path=args.formats_config,
        recovery_config_path=args.recovery_config,
        hard_failure_config_path=args.hard_failure_config,
        witness_context=args.witness_context,
        witness_context_config_path=args.witness_context_config,
        nuda_per_mille=args.nuda_per_mille,
        nuda_approval_ref=args.nuda_approval_ref,
        perlector_instrument_per_mille=args.perlector_instrument_per_mille,
        perlector_instrument_approval_ref=args.perlector_instrument_approval_ref,
        perlector_protocol_config_path=args.perlector_protocol_config,
        perlector_audit_config_path=args.perlector_audit_config,
        draft_fed=args.draft_fed,
        serving_recipes_config_path=args.serving_recipes_config,
        decoding_config_path=args.decoding_config,
    )
    require_corpus_frame_shard(len(pages), bindings["sealed_config_digests"])

    # The door creates the run: it is the first thing that knows what arrived, so
    # it is the only stage that can bind a run id to its inputs. The manifest
    # carries the *declared* digests — what this run believed about its sources —
    # so a refusal and the declaration it was refused against tell one story.
    tree = RunTree.create(
        Path(args.run_root),
        args.run_id,
        source_manifest=[
            {
                "relative_path": page["path"],
                "sha256": declared[page["ordinal"]],
                # The fixture declaration's digest is checked against these
                # bytes by the Door.  Bind that computed page-set identity
                # separately so shard membership never collapses to ordinals.
                "computed_sha256": page["sha256"],
                "ordinal": page["ordinal"],
            }
            for page in pages
        ],
        config_digest=bindings["config_digest"],
        adapter_recipes=bindings["adapter_recipes"],
        witness_chairs=bindings["witness_chairs"],
        ingress=synthetic_fixture_ingress_record(),
        render_settings={"pdf": pdf_settings.to_record()},
        sealed_config_digests=bindings["sealed_config_digests"],
        register_bytes=_read_corpus_register(args.corpus_register),
    )
    context = _door_context(
        tree,
        fixture,
        args.scenario,
        args,
        registry,
        sealed_config_digests=bindings["sealed_config_digests"],
    )
    # The point of use, on the same terms as the Designator's padding recheck: the
    # bytes these settings were parsed from must be the bytes this run sealed. One
    # read makes them so; this is what refuses if a later change reintroduces a
    # second one, or seals the digest under a name nothing renders with.
    context.require_sealed_config("pdf-render", pdf_render_binding.config_sha256)
    sources = [
        SourceEntry(page["ordinal"], page["path"], declared[page["ordinal"]]) for page in pages
    ]
    admitted = process_sources(
        context,
        tree,
        sources,
        lambda declared_path: (fixture_root / declared_path).read_bytes(),
        policy=policy,
        pdf_settings=pdf_settings,
    )
    return _finish_door_run(context, tree, admitted)


def real_submission(args, registry) -> int:
    """Admit a local folder's bytes into a run, once it is proven to sit inside an
    approved storage location.

    Order matters: the storage roots are checked, then the folder is inventoried,
    then the run is created, and only then is a byte published. A folder outside
    every approved root means nothing was read and nothing exists.
    """
    data_policy_binding = gate.load_policy_binding(Path(args.data_gate_policy))
    data_policy = data_policy_binding.policy
    if args.submission_manifest is None:
        raise ContractError(
            "a real submission requires --submission-manifest: the self-hashed filename "
            "ledger is how its copied bytes are matched back to the original set"
        )

    roots = gate.approved_storage_roots(data_policy)
    # The *resolved* paths are used from here on, as `submit.py` does and for the
    # same reason: checking one path and then opening another is where a
    # check-then-use race lives, and the resolved values are already in hand.
    submission_folder = gate.require_approved_storage_location(
        Path(args.submission_folder), roots, "submitted folder"
    )
    run_root = gate.require_approved_storage_location(Path(args.run_root), roots, "run root")
    manifest_path = gate.require_approved_storage_location(
        Path(args.submission_manifest), roots, "submission filename ledger"
    )
    # The run-level cap, now that the root it reads is a location the policy
    # approved, and against the resolved path rather than the typed one.
    _refuse_halted_run_root(run_root, args)
    for location, label in (
        (run_root, "run root"),
        (manifest_path, "submission filename ledger"),
    ):
        if location.is_relative_to(submission_folder):
            raise ContractError(
                f"the {label} cannot live inside the submitted folder; otherwise the next "
                "inventory includes pipeline-produced records as submitted sources"
            )
    ledger = submission_ledger.load_manifest(manifest_path)
    if args.triage_clusters is not None and args.triage_decision_manifest is None:
        raise ContractError("triage cluster records require a triage decision manifest")
    if args.triage_producer_recipe is not None and args.triage_decision_manifest is None:
        raise ContractError("triage producer recipe requires a triage decision manifest")
    # Gated exactly as the submission filename ledger above is, and for the same
    # two reasons: a real-path input read from disk is inside the data-handling
    # policy's approved roots or it was never read, and a decision record sitting
    # inside the submitted folder would be inventoried as a submitted source.
    triage_paths = [
        (args.triage_decision_manifest, "triage decision manifest"),
        (args.triage_clusters, "triage re-shoot cluster records"),
        (args.triage_producer_recipe, "triage producer recipe"),
    ]
    gated_triage: dict[str, Path] = {}
    for location, label in triage_paths:
        if location is None:
            continue
        resolved = gate.require_approved_storage_location(Path(location), roots, label)
        if resolved.is_relative_to(submission_folder):
            raise ContractError(
                f"the {label} cannot live inside the submitted folder; otherwise the next "
                "inventory includes pipeline-produced records as submitted sources"
            )
        gated_triage[label] = resolved
    triage_rows, triage_clusters, triage_digests = (
        load_triage_decisions(
            gated_triage["triage decision manifest"],
            gated_triage.get("triage re-shoot cluster records"),
            gated_triage.get("triage producer recipe"),
        )
        if args.triage_decision_manifest is not None
        else (None, None, {})
    )

    format_policy = admission.load_format_policy()
    pdf_render_binding = _load_pdf_render_binding(args)
    pdf_settings = pdf_render_binding.settings
    # Inventory streams every digest and retains no source body.  Later reads
    # reopen by directory descriptor, never by a reconstructed ordinary path: a
    # 15 GB PDF remains a stream, and the digest and PDFium renderer hold the same
    # submitted file even if its name is replaced after inventory.
    # The digest each submitted path is already bound to, for the one check that
    # cannot be settled by `fstat` alone: a rewrite of a held inode can imitate a
    # name replacement exactly (`inventory.OpenedSubmissionSource.assert_unchanged`).
    ledger_digests = {row["relative_path"]: row["sha256"] for row in ledger["files"]}
    found = inventory.read_submission(submission_folder, max_bytes=0)
    found_paths = {source.relative_path for source in found}
    declared_paths = {row["relative_path"] for row in ledger["files"]}
    unexpected = found_paths - declared_paths
    if unexpected:
        raise ContractError(
            "the submitted folder contains file(s) absent from its self-hashed filename "
            f"ledger ({len(unexpected)} extra); no run was created over an ambiguous set"
        )

    def read_bytes(relative_path: str) -> bytes:
        try:
            with inventory.open_submission_source(
                submission_folder, relative_path
            ) as opened_source:
                # The raster path is deliberately bounded even if an untrusted
                # filename ledger lies about a file that grew after inventory.
                # `process_sources` compares this observed size to the ledger and
                # records the mismatch; it never allocates an arbitrary replacement.
                data = opened_source.handle.read(MAX_SOURCE_BYTES + 1)
                opened_source.assert_unchanged(expected_sha256=ledger_digests.get(relative_path))
                return data
        except inventory.SubmissionInputError as error:
            # `process_sources` turns a per-source read failure into its ordinary,
            # private named refusal artifact rather than failing the entire census.
            raise OSError(str(error)) from error

    def open_source(relative_path: str):
        return inventory.open_submission_source(submission_folder, relative_path)

    bindings = _real_bindings(
        registry.config,
        ledger,
        format_policy,
        pdf_settings,
        load_recovery_policy(args.recovery_config),
        load_hard_failure_policy(args.hard_failure_config),
        args.formats_config,
        pdf_render_config_sha256=pdf_render_binding.config_sha256,
        data_handling_config_sha256=data_policy_binding.config_sha256,
        designator_padding_config_sha256=_padding_config_digest(args.designator_padding_config),
        designator_geometry_config_sha256=_geometry_config_digest(args.designator_geometry_config),
        alignment_config_path=args.alignment_config,
        triage_document_digests=triage_digests,
        witness_context=args.witness_context,
        witness_context_config_path=args.witness_context_config,
        nuda_per_mille=args.nuda_per_mille,
        nuda_approval_ref=args.nuda_approval_ref,
        perlector_instrument_per_mille=args.perlector_instrument_per_mille,
        perlector_instrument_approval_ref=args.perlector_instrument_approval_ref,
        perlector_protocol_config_path=args.perlector_protocol_config,
        perlector_audit_config_path=args.perlector_audit_config,
        decoding_config_path=args.decoding_config,
        draft_fed=args.draft_fed,
    )
    # The modes seal must be proved before triage rows can shape master-frame geometry.
    if triage_rows is not None:
        require_triage_modes(bindings["sealed_config_digests"])
    sources = expand_sources(
        [
            {
                "relative_path": source["relative_path"],
                "sha256": source["sha256"],
                "bytes": source["bytes"],
                "ledger_sha256": ledger["self_hash"],
            }
            for source in ledger["files"]
        ],
        read_bytes,
        format_policy,
        open_source=open_source,
        triage_rows=triage_rows,
        triage_clusters=triage_clusters,
    )
    # Real ingress binds the same bounded shard policy before its RunTree exists.
    require_corpus_frame_shard(len(sources), bindings["sealed_config_digests"])
    tree = RunTree.create(
        run_root,
        args.run_id,
        source_manifest=[
            {
                "relative_path": source.declared_path,
                "sha256": source.declared_sha256,
                # Membership seals before the ledger declaration is checked.
                "computed_sha256": _membership_sha256(source),
                "ordinal": source.ordinal,
                "bytes": source.declared_size,
                "ledger_sha256": source.ledger_sha256,
                "container_page_index": source.container_page_index,
            }
            for source in sources
        ],
        config_digest=bindings["config_digest"],
        adapter_recipes=bindings["adapter_recipes"],
        witness_chairs=bindings["witness_chairs"],
        ingress=real_ingress_record(),
        render_settings={"pdf": pdf_settings.to_record()},
        sealed_config_digests=bindings["sealed_config_digests"],
        register_bytes=_read_corpus_register(args.corpus_register),
    )

    context = _door_context(
        tree,
        {},
        "real-submission",
        args,
        registry,
        sealed_config_digests=bindings["sealed_config_digests"],
    )
    context.require_sealed_config("pdf-render", pdf_render_binding.config_sha256)
    # CF01: the caller-selected data-handling policy that decided where this
    # material may live is now named by the run it admitted. The gate itself
    # already worked from one in-memory record, so this closes the *evidence*
    # gap rather than a race — `config/README.md` said outright that nothing
    # bound a run to the policy version governing it, and a later reader could
    # not say which of two policy files admitted a corpus.
    context.require_sealed_config("data-handling", data_policy_binding.config_sha256)
    admitted = process_sources(
        context,
        tree,
        sources,
        read_bytes,
        policy=format_policy,
        pdf_settings=pdf_settings,
        open_source=open_source,
    )
    return _finish_door_run(context, tree, admitted)


def _read_corpus_register(register_path: str | None) -> bytes | None:
    """Read an optional register before run creation, with a recoverable refusal."""
    if register_path is None:
        return None
    try:
        return read_register_file(register_path)
    except (OSError, ContractError) as error:
        raise ContractError(
            "the corpus register could not be read before run creation; no run or admission "
            "record was written; provide a readable canonical register and retry; the file "
            "must be bounded, regular, and not a symlink"
        ) from error


def _announce_refusal_report(tree: RunTree, refusal_report: str | None) -> None:
    """Give the terminal only a count and private report location, never a name."""
    if refusal_report is None:
        return
    _total, census = _refusal_census(tree)
    print(
        f"{sum(census.values())} door refusal(s); private refusal report: {refusal_report}",
        file=sys.stderr,
    )


def _announce_duplicate_report(tree: RunTree, duplicate_report: str | None) -> None:
    """Name duplicate sources in the operator summary without printing filenames."""
    if duplicate_report is None:
        return
    record = tree.read_artifact(
        DOOR,
        "duplicate-report",
        artifact_id(DOOR, "duplicate-report", DOOR_DUPLICATE_REPORT_SUBJECT),
    )
    payload = record["payload"]
    sources = payload.get("duplicate_source_count")
    ordinals = payload.get("duplicate_ordinal_count")
    if (
        not isinstance(sources, int)
        or isinstance(sources, bool)
        or not isinstance(ordinals, int)
        or isinstance(ordinals, bool)
    ):
        raise ContractError("the door duplicate report has no integer source and ordinal counts")
    print(
        f"{sources} duplicate source(s) admitted across {ordinals} page ordinal(s); "
        f"private duplicate report: {duplicate_report}",
        file=sys.stderr,
    )


def _padding_config_digest(path: str) -> str:
    """The Designator padding policy's digest, read at the door like every other.

    A real submission stops before the Designator cuts anything today, so this
    binds nothing a real run currently uses. It is sealed anyway, because the
    day a real structure pass exists is the day crops start depending on it, and
    a configuration that entered the digest only once it mattered would leave
    every earlier run id reusable across a geometry change.
    """
    try:
        return digest_bytes(Path(path).read_bytes())
    except OSError as error:
        raise ContractError(
            f"the Designator padding configuration binding at {path} could not be read"
        ) from error


def _geometry_config_digest(path: str) -> str:
    """The Designator geometry policy's digest, sealed for the same reason.

    Unlike padding, this one is load-bearing today: `pipeline/2_designator/run.py`
    re-reads the geometry policy at point of use and proves it read what was
    bound via `context.require_sealed_config("designator-geometry", ...)`, so a
    real run whose door never sealed this name would refuse at the Designator
    unconditionally — the exact defect F-S5 named for padding.
    """
    try:
        return digest_bytes(Path(path).read_bytes())
    except OSError as error:
        raise ContractError(
            f"the Designator geometry configuration binding at {path} could not be read"
        ) from error


def _real_bindings(
    models,
    ledger,
    format_policy,
    pdf_settings,
    recovery_policy,
    hard_failure_policy,
    armarium_formats_config_path=DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH,
    corpus_frame_config_path=DEFAULT_CORPUS_FRAME_CONFIG_PATH,
    *,
    pdf_render_config_sha256: str,
    data_handling_config_sha256: str,
    designator_padding_config_sha256: str,
    designator_geometry_config_sha256: str,
    alignment_config_path=DEFAULT_ALIGNMENT_CONFIG_PATH,
    triage_document_digests: dict[str, str] | None = None,
    witness_context: str = "named",
    witness_context_config_path: str | Path = DEFAULT_WITNESS_CONTEXT_CONFIG_PATH,
    nuda_per_mille: int = 0,
    nuda_approval_ref: str = "",
    perlector_instrument_per_mille: int = 0,
    perlector_instrument_approval_ref: str = "",
    perlector_protocol_config_path=DEFAULT_PERLECTOR_PROTOCOL_CONFIG_PATH,
    perlector_audit_config_path=DEFAULT_PERLECTOR_AUDIT_CONFIG_PATH,
    decoding_config_path=DEFAULT_DECODING_CONFIG_PATH,
    draft_fed: bool = True,
) -> dict[str, Any]:
    """The sealed configuration facts for a real submission.

    The source manifest binds the bytes. The configuration digest binds everything
    else that shaped what the door did: the model roster, decoder routing, and the
    versions/settings that render pages. A run resumed under different versions or
    routing is a different run wearing an old name, and `RunTree.create` refuses
    it before anything is written.

    **Cut 2026-08-09, rebound for provenance under CF01.** The per-run APPROVAL
    record and its currency check are gone and stay gone: real input is no
    longer approval-gated. The data-handling policy's byte digest is bound
    again — `data_handling_policy_sha256` above and the `data-handling` sealed
    name — but as provenance and tamper-evidence only (WHICH caller-selected
    policy performed the storage-root check), never as a sign-off.
    """
    validate_witness_adapter_bindings(models)
    witness_context_declaration_sha256 = validate_witness_context_bindings(
        models,
        witness_context=witness_context,
        witness_context_config_path=witness_context_config_path,
        nuda_per_mille=nuda_per_mille,
        nuda_approval_ref=nuda_approval_ref,
        perlector_instrument_per_mille=perlector_instrument_per_mille,
        perlector_instrument_approval_ref=perlector_instrument_approval_ref,
    )
    _, alignment_config_sha256 = load_alignment_limits(alignment_config_path)
    _decoding_policy, decoding_config_sha256 = load_decoding_policy(decoding_config_path)
    adapter_recipes = dict(sorted(models.adapter_recipes.items()))
    adapter_recipes[DOOR] = REAL_DOOR_ADAPTER_REVISION
    armarium_formats_digest, armarium_formats = bind_armarium_formats(armarium_formats_config_path)
    corpus_frame_policy, corpus_frame_config_sha256 = load_corpus_frame_policy(
        corpus_frame_config_path
    )
    # Read once and named, for the two reasons the fixture path
    # (`common.stage.run_config_bindings`) already reads it this way: the digest
    # sealed into `config_digest` and the one published as the point-of-use
    # recheck must be of the same bytes -- two reads can straddle a rewrite --
    # and an unreadable declaration is a named ContractError here rather than an
    # OSError traceback out of the middle of a dict literal.
    try:
        perlector_protocol_config_sha256 = digest_bytes(
            Path(perlector_protocol_config_path).read_bytes()
        )
    except OSError as error:
        raise ContractError(
            "the Perlector protocol configuration binding at "
            f"{perlector_protocol_config_path} could not be read"
        ) from error
    # Same discipline, same reasons, for the audit policy: one read feeding
    # both digests, and a named refusal instead of an OSError traceback.
    try:
        perlector_audit_config_sha256 = digest_bytes(Path(perlector_audit_config_path).read_bytes())
    except OSError as error:
        raise ContractError(
            "the Perlector audit configuration binding at "
            f"{perlector_audit_config_path} could not be read"
        ) from error
    # The shared constant, not a second spelling of it: `require_triage_modes` reads
    # this same file at its point of use through the default below, and a moved path
    # would otherwise refuse every real submission carrying triage geometry with
    # "changed between run binding" — pointing the operator at a rewrite that never
    # happened.
    triage_modes_config_path = Path(DEFAULT_TRIAGE_MODES_CONFIG_PATH)
    try:
        triage_modes_config_sha256 = digest_bytes(triage_modes_config_path.read_bytes())
    except OSError as error:
        raise ContractError(
            f"the triage modes configuration binding at {triage_modes_config_path} could not be read"
        ) from error
    return {
        "witness_chairs": list(models.witness_chairs),
        "config_digest": digest_of(
            {
                "submission": [
                    {
                        "relative_path": source["relative_path"],
                        "sha256": source["sha256"],
                        "bytes": source["bytes"],
                    }
                    for source in ledger["files"]
                ],
                "submission_ledger_sha256": ledger["self_hash"],
                "format_policy": format_policy,
                "pdf_render_config_sha256": pdf_render_config_sha256,
                # CodeRabbit CF01. Not an approval record and not a gate: the door
                # still admits real material on the storage-root check alone. This
                # binds *which* caller-selected policy performed that check, so a
                # run can be reconciled against the policy that governed it instead
                # of against whichever file happens to sit at the default path now.
                "data_handling_policy_sha256": data_handling_config_sha256,
                "door_execution_recipe": _door_execution_recipe(pdf_settings),
                "door_implementation_revision": REAL_DOOR_ADAPTER_REVISION,
                "armarium_formats_config_sha256": armarium_formats_digest,
                "armarium_formats": armarium_formats.to_record(),
                "recovery_policy": recovery_policy,
                "hard_failure_policy": hard_failure_policy,
                "designator_padding_config_sha256": designator_padding_config_sha256,
                "designator_geometry_config_sha256": designator_geometry_config_sha256,
                "alignment_config_sha256": alignment_config_sha256,
                "triage_modes_config_sha256": triage_modes_config_sha256,
                # Unit 5's decisions are geometry that shaped these pixels, so
                # they are bound like any other fact that did. A triage pass re-run
                # between two attempts at one run id changes these digests, and
                # `RunTree.create` then refuses the reuse by name instead of the
                # swap being caught only where a changed row happens to reach an
                # already-published admission. Empty for a submission with no
                # split decisions, so an ordinary run's digest is unchanged.
                "triage_document_digests": dict(sorted((triage_document_digests or {}).items())),
                "corpus_frame_policy": corpus_frame_policy,
                "corpus_frame_config_sha256": corpus_frame_config_sha256,
                "decoding_config_sha256": decoding_config_sha256,
                "models": models.to_record(),
                # Spec 08's run-level settings, bound on the real path exactly as
                # `run_config_bindings` binds them on the fixture path: a resumed
                # run under a different witness regime, declaration, or nuda
                # design is a different run wearing an old name. Validated by the
                # same shared function the fixture path uses, so an unapproved
                # nuda sample or a misdeclared witness refuses before the run
                # tree exists, never after the Attestatores leg has been paid for.
                "witness_context_regime": witness_context,
                "witness_context_declaration_sha256": witness_context_declaration_sha256,
                "nuda_per_mille": nuda_per_mille,
                "nuda_approval_ref": nuda_approval_ref,
                "perlector_instrument_per_mille": perlector_instrument_per_mille,
                "perlector_instrument_approval_ref": perlector_instrument_approval_ref,
                "perlector_protocol_config_sha256": perlector_protocol_config_sha256,
                "perlector_audit_config_sha256": perlector_audit_config_sha256,
                "draft_fed": draft_fed,
            }
        ),
        "adapter_recipes": adapter_recipes,
        # Both entries named here exactly as the fixture path's
        # `run_config_bindings` names them (`common/stage.py`), so the two paths'
        # `sealed_config_digests` share one shape. `designator-padding`'s bytes
        # were already folded into `config_digest` above (`_padding_config_digest`
        # docstring: "sealed anyway... the day a real structure pass exists is the
        # day crops start depending on it"), but the named point-of-use-recheck
        # entry itself was missing here -- a real Designator run reaching
        # `context.require_sealed_config("designator-padding", ...)`
        # (`pipeline/2_designator/run.py`) over real ingress would have refused
        # with "this context sealed no digest for the designator-padding
        # configuration" on every real run, unconditionally, the day R2 lands.
        # Found in audit (S5); F-S5.
        "sealed_config_digests": {
            "designator-padding": designator_padding_config_sha256,
            "designator-geometry": designator_geometry_config_sha256,
            "alignment": alignment_config_sha256,
            "corpus-frame-shard": corpus_frame_config_sha256,
            "decoding": decoding_config_sha256,
            "perlector-protocol": perlector_protocol_config_sha256,
            "perlector-audit": perlector_audit_config_sha256,
            "pdf-render": pdf_render_config_sha256,
            "recovery": recovery_policy["config_sha256"],
            "hard-failure": hard_failure_policy["config_sha256"],
            "triage-modes": triage_modes_config_sha256,
            # Real ingress only: the fixture route is not gated, so a fixture run
            # seals no data-handling name and a point of use that asked for one
            # there would be asking about a check that never happened.
            "data-handling": data_handling_config_sha256,
        },
    }


def _door_execution_recipe(pdf_settings) -> dict[str, Any]:
    """Facts that change page admission or pixels, sealed before real writes."""
    return {
        "pdf": pdf_render.renderer_recipe(pdf_settings),
        "raster": raster_renderer_recipe(),
        "limits": {
            "max_source_bytes": MAX_SOURCE_BYTES,
            "max_dimension": MAX_DIMENSION,
            "max_pixels": MAX_PIXELS,
        },
    }


def _door_context(
    tree: RunTree,
    fixture: dict,
    scenario: str,
    args,
    registry,
    *,
    sealed_config_digests: dict[str, str] | None = None,
) -> StageContext:
    """The door's own context.

    It carries the sealed digests because the door is a point of use as well as the
    binding step: it parses the PDF render policy and, on the real route, the
    data-handling policy, and it must be able to prove that what it acted on is what
    the run recorded.
    """
    run = tree.read_run()
    return StageContext(
        tree=tree,
        run=run,
        fixture=fixture,
        scenario=scenario,
        stage=DOOR,
        adapter_revision=adapter_recipe_for(run, DOOR),
        args=args,
        registry=registry,
        sealed_config_digests=sealed_config_digests,
    )


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
