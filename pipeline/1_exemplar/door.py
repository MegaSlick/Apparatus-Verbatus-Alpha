"""The door: what may enter at all, decided by bytes alone.

Admissions and refusals are written into the Exemplar's directory, so no refusal
is filed where nothing downstream reads it (principle 2).

`admission.py` routes each source by its decoded bytes, never its extension.
Ordinary rasters are decoded; PDF and TIFF containers fan out, one ordinal per page.
PDFium paints the whole visible PDF page, so text beside an image stays in the
sealed pixels. Every refusal is an artifact with a reason from
`admission.RefusalReason`, and an input set that admits nothing fails loudly.

A run is created either from the repository's declared synthetic fixture or from
real input inside an approved storage location; the route is sealed into
`run.json` as `ingress`. Real input needs no per-run approval record: it never
enters git, so the storage-root check in `operations.submit.gate` is the only gate.

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
from common.contracts.serving import SERVING_CONFIG_INPUTS_SCHEMA  # noqa: E402
from common.contracts.stages import DOOR  # noqa: E402
from common.corpus_register import (  # noqa: E402
    membership_heads,
    read_register_path,
    read_snapshot,
    validate_register_bytes,
)
from common.decoding import DEFAULT_DECODING_CONFIG_PATH, load_decoding_policy  # noqa: E402
from common.exemplar_boundary import SEALED_DERIVATIVE_PAGE_KIND  # noqa: E402
from common.hard_failure import load_hard_failure_policy  # noqa: E402
from common.recovery import load_recovery_policy  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402
from common.sealed_config import read_sealed_toml  # noqa: E402
from common.stage import (  # noqa: E402
    DEFAULT_CORPUS_FRAME_CONFIG_PATH,
    DEFAULT_PERLECTOR_AUDIT_CONFIG_PATH,
    DEFAULT_PERLECTOR_PROTOCOL_CONFIG_PATH,
    DEFAULT_POD_PLACEMENT_CONFIG_PATH,
    DEFAULT_SERVING_RECIPES_CONFIG_PATH,
    DEFAULT_TRIAGE_MODES_CONFIG_PATH,
    DEFAULT_WITNESS_CONTEXT_CONFIG_PATH,
    EXIT_COMPLETE,
    REAL_DOOR_ADAPTER_REVISION,
    REAL_SCENARIO,
    StageContext,
    adapter_recipe_for,
    load_corpus_frame_policy,
    load_fixture,
    load_triage_modes,
    real_run_policy_digest,
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

DESCRIPTION = "The door: what may enter at all, decided by bytes alone."


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
    # Set during expansion so membership binds inspected bytes before the run
    # seals; None for unreadable and oversized sources.
    computed_sha256: str | None = None


def _membership_sha256(source: SourceEntry) -> str | None:
    """The digest one page binds into shard membership.

    Inspected bytes win because the ledger is untrusted until admission, after the
    run seals. Container pages also bind their index, since they share one file
    digest. Sources never inspected fall back to their declaration.
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


def _refused(reason: str | None, digest: str | None = None) -> _Decision:
    return _Decision("refused", reason, digest, None, None)


def _refused_for(code: RefusalReason, detail: str, digest: str | None = None) -> _Decision:
    return _refused(admission.reason(code, detail), digest)


def _format_refused(error: FormatRefusal) -> _Decision:
    return _refused_for(admission._refusal_code(error), str(error))


DOOR_REFUSAL_REPORT_SCHEMA: Final = "door-refusal-report.v0"
DOOR_REFUSAL_REPORT_SUBJECT: Final = "refusal-report"
DOOR_DUPLICATE_REPORT_SCHEMA: Final = "door-duplicate-report.v0"
DOOR_DUPLICATE_REPORT_SUBJECT: Final = "duplicate-report"
DOOR_CLUSTER_REPORT_SCHEMA: Final = "door-re-shoot-cluster-report.v1"
_SOURCE_HASH_CHUNK: Final = 1024 * 1024
_SNIFF_BYTES: Final = 4096
# Triage JSON is untrusted input. A run holds at most one 1,000-page shard (the
# corpus-frame validator's ceiling), so bound both before the triage manifest's
# pairwise validation walks attacker-sized lists.
MAX_TRIAGE_DOCUMENT_BYTES: Final = 64 * 1024 * 1024
MAX_TRIAGE_DERIVATIVE_PAGES: Final = 1_000
# `REAL_DOOR_ADAPTER_REVISION` lives in `common/stage.py` because every later
# stage rechecks it and `common/` may not import this file.


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_positive_int(value: Any) -> bool:
    return _is_int(value) and value >= 1


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

    A scenario restriction lets a proof page exercise a narrow path without
    changing the input of every other acceptance run.
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

    A `page_refusal` row declares a digest the bytes cannot match, so refusal
    scenarios run the door's real inspection path, not a test-only branch.
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

    Returns the bytes with their parse, so a digest and the decisions cannot
    straddle a rewrite; duplicate JSON member names are refused.

    A checked pathname can be swapped for a symlink or FIFO, and an unbounded file
    makes JSON parsing a denial of service. So each component opens no-follow
    relative to its parent descriptor, and the leaf must not change during the read.
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
        # An absent file is a plain read failure, not a redirect.
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
    if _file_identity(before) != _file_identity(after):
        raise ContractError(f"the {label} changed while it was being read")
    try:
        return raw, json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ContractError(
            f"the {label} is not valid UTF-8 JSON; no run was created because its decisions "
            "cannot be interpreted; export valid UTF-8 JSON and retry"
        ) from error


def _file_identity(status: os.stat_result) -> tuple[int, ...]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
        status.st_nlink,
    )


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
    """Read the closed triage decision manifest, its clusters and producer recipe.

    Also returns each document's byte digest, taken from the parsed bytes. The
    decisions shape pixels, so the digests enter `config_digest` and re-entering a
    run id after a re-run triage pass is refused by name.
    """
    manifest_bytes, document = _read_triage_document(manifest_path, "triage decision manifest")
    clusters_bytes, clusters_document = _read_optional_triage_document(
        clusters_path, "triage re-shoot cluster records"
    )
    recipe_bytes, recipe_document = _read_optional_triage_document(
        producer_recipe_path, "triage producer recipe"
    )
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


def _read_optional_triage_document(
    path: str | Path | None, label: str
) -> tuple[bytes, Any] | tuple[None, None]:
    return (None, None) if path is None else _read_triage_document(path, label)


def _exceeds_triage_bound(lists: Any) -> bool:
    """Whether the lists among ``lists`` hold more than the bound, stopping once they do."""
    total = 0
    for items in lists:
        if isinstance(items, list):
            total += len(items)
            if total > MAX_TRIAGE_DERIVATIVE_PAGES:
                return True
    return False


def _member(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else None


def _refuse_triage_amplification(document: Any, clusters: Any) -> None:
    """Bound split and cluster fan-out before triage validation walks attacker-sized lists."""
    if isinstance(document, dict) and isinstance(document.get("records"), list):
        split_parts = (_member(_member(row, "split"), "parts") for row in document["records"])
        if _exceeds_triage_bound(split_parts):
            raise ContractError(
                "the triage decision manifest declares more than "
                f"{MAX_TRIAGE_DERIVATIVE_PAGES} derivative pages; no run was created because "
                "attacker-controlled split counts must be bounded before pairwise geometry "
                "validation and source expansion; export one configured shard and retry"
            )
    if isinstance(clusters, dict):
        members = (_member(record, "member_frame_sha256") for record in clusters.values())
        if _exceeds_triage_bound(members):
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

    A real PDF arrives with ``data`` None, as an open PDFium document plus a digest
    streamed from the same anchored descriptor, so PDFium never reopens a mutable
    path or needs the whole file in memory.
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
        return _refused_for(
            RefusalReason.DIGEST_MISMATCH,
            f"computed {whole_digest}, but {source.declared_sha256} was declared",
            whole_digest,
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
            return _refused_for(RefusalReason.DIGEST_MISMATCH, str(error))
        except FormatRefusal as error:
            return _format_refused(error)
        except ContractError as error:
            return _refused_for(RefusalReason.CORRUPT, str(error))
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
            return _refused(checked.reason)
        return _Decision(
            "admitted", None, checked.digest, page_bytes, checked.geometry, rendered_from
        )

    if source.container_page_index is None:
        if verdict == admission.RENDER_PAGES:
            return _refused_for(
                RefusalReason.UNSUPPORTED_VARIANT,
                "a page container must be declared with a page index; this one carries none",
                whole_digest,
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
        return _refused(str(error))
    except FormatRefusal as error:
        return _format_refused(error)

    checked = admission.inspect_source(page_bytes, declared_sha256=None, policy=policy)
    if checked.outcome != "admitted":
        return _refused_for(
            RefusalReason.CORRUPT, f"the rendered page did not itself admit: {checked.reason}"
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

    Counting renders no pixels. A source that cannot be read or counted still
    gets its ordinals (one, or one per declared triage part when it is split), so
    its refusal is published rather than lost.

    ``open_source`` is the real submission's descriptor-anchored opener. PDFium
    parses a file before any digest exists, so a reopened pathname could fan out
    the pages of a document nobody submitted. Each row holds its own descriptor
    only while it is processed.
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
        if declared_size is not None and (not _is_int(declared_size) or declared_size < 0):
            # No path in the message: `run_stage` prints it to stderr, and the
            # data-handling policy keeps declared paths out of logs.
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
                    0 if bound_triage_row is not None else None,
                    computed_sha256,
                )
            )

        def append_declared_pages(
            detected_format: str | None,
            bound_triage_row: dict[str, Any] | None = triage_row,
            computed_sha256: str | None = None,
        ) -> None:
            """One ordinal, or one per declared triage part, whether or not it can be read."""
            if bound_triage_row is None:
                append(None, detected_format, computed_sha256=computed_sha256)
                return
            for part_index in range(len(bound_triage_row["split"]["parts"])):
                append(
                    part_index,
                    detected_format,
                    triage_part_index=part_index,
                    computed_sha256=computed_sha256,
                )

        data: bytes | None = None
        # None means this pass read no complete source and must not claim it did.
        computed: str | None = None
        try:
            if open_source is not None:
                # Classify from the anchored descriptor later used for the digest
                # and PDFium; a path rebuilt from the ledger could be replaced.
                with open_source(path) as opened_source:
                    detected = _sniff_source_stream(opened_source.handle)
                    if detected == "pdf":
                        # First of two digest streams over a real PDF: this one
                        # binds membership before `run.json` exists; the one in
                        # `process_sources` catches bytes replaced after the seal.
                        # Neither can be dropped.
                        computed, _ = _source_digest_stream(opened_source.handle)
                    opened_source.assert_unchanged(expected_sha256=declared_sha256)
            else:
                # Without an anchored opener the caller supplies bytes; PDFium is
                # never handed a reopenable pathname.
                detected = None
            if detected != "pdf":
                # Rasters are read into memory under the size bound; only PDFs stream.
                if declared_size is not None and declared_size > MAX_SOURCE_BYTES:
                    append_declared_pages(detected)
                    continue
                data = read_bytes(path)
                detected = sniff(data)
        except (OSError, inventory.SubmissionInputError):
            append_declared_pages(None)
            continue
        route = admission.classify_detected_format(detected, policy)
        if data is not None and len(data) > MAX_SOURCE_BYTES:
            append_declared_pages(detected)
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
                # The frame does not decode, but the manifest says how many pages
                # it yields; one refused ordinal per part keeps the denominator
                # and shard cap honest.
                append_declared_pages(detected, computed_sha256=computed)
                continue
            if frame_count != 1:
                raise ContractError(
                    "a triage decision row names a multi-page raster container; no source "
                    "expansion was returned because one frame-space recipe cannot describe every "
                    "page; omit that container from the triage manifest and let the Door fan out "
                    "its pages"
                )
            append_declared_pages(detected, computed_sha256=computed)
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
            append(
                0 if route == admission.RENDER_PAGES else None, detected, computed_sha256=computed
            )
            continue
        # PDF and TIFF always fan out. Any other multi-frame image fans out too,
        # or every frame after the first would be dropped downstream.
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
                # Direct callers bypass manifest validation; give them a named
                # refusal, not a KeyError.
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
    # Rows for frames outside this submission are expected: the manifest is
    # corpus-scoped and a submission is one shard.
    return sources


def _require_case_unique_paths(files: list[dict[str, Any]]) -> None:
    """Refuse names that alias on default case-insensitive APFS.

    A ledger made on a case-sensitive host may be admitted on APFS, where
    ``Page.PNG`` and ``page.png`` are one file; an ordinal must name the same
    bytes on every host.
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

    Call it before creating each RunTree: cutting after a run exists would
    change its immutable denominator. The page cap is sealed policy; pass
    ``max_shards`` only for a caller's own ceiling.
    """
    if not _is_positive_int(max_pages_per_shard) or (
        max_shards is not None and not _is_positive_int(max_shards)
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
    # Split parts are adjacent, so block every seam inside one; a cluster may be
    # scattered, so block every seam between its first and last member.
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
                    "shard within the sealed cap, or stop for the project lead if the cap itself conflicts"
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

    Rasters are read once per path. A real PDF's digest is streamed here and
    every page renders from one descriptor-anchored PDFium handle, so a reel
    larger than memory keeps its ordinals and a replaced path cannot separate
    digest from pixels. This stream is the only check that sees bytes replaced
    after the run sealed: the `computed_sha256` comparison below depends on it.

    Per-file, never per-folder: one refused source does not stop the rest.
    Byte-identical pages within one PDF stay distinct, and a second path with the
    same bytes is admitted under its own ordinal with a duplicate fact.
    """
    if pdf_settings is None:
        pdf_settings = render_config.load_pdf_render_settings(minimum_dpi=pdf_render.MIN_RENDER_DPI)
    admitted = 0
    seen_sources: dict[str, tuple[str, int]] = {}
    # One cached raster, not a map: a path's ordinals are contiguous, and a map
    # would grow memory with the number of rasters.
    cached_path: str | None = None
    cached_data: bytes | None = None
    # A container's ordinals are contiguous, so one PDF stream and document are
    # held open at a time, on one reusable stack.
    active_pdf = ExitStack()
    active_pdf_key: str | None = None
    active_pdf_digest: tuple[str, int] | None = None
    active_pdf_document: pdf_render.OpenPdf | None = None
    active_opened_source: inventory.OpenedSubmissionSource | None = None

    with active_pdf:
        for source in sorted(sources, key=lambda item: item.ordinal):
            streamed_pdf = open_source is not None and source.detected_format == "pdf"
            source_key = source.declared_path
            if active_pdf_key is not None and (not streamed_pdf or source_key != active_pdf_key):
                active_pdf.close()
                active_pdf_key = active_pdf_digest = None
                active_pdf_document = active_opened_source = None
            if (
                source.declared_size is not None
                and source.declared_size > MAX_SOURCE_BYTES
                and not streamed_pdf
            ):
                # This cap guards an in-memory read; a streamed PDF makes none.
                _publish_refusal(
                    context,
                    source,
                    RefusalReason.TOO_LARGE,
                    admission.too_large_detail(source.declared_size),
                )
                continue

            data: bytes | None = None
            if streamed_pdf:
                try:
                    if active_pdf_key is None:
                        assert open_source is not None  # narrowed by streamed_pdf
                        candidate_source = active_pdf.enter_context(
                            open_source(source.declared_path)
                        )
                        actual_digest, actual_size = _source_digest_stream(candidate_source.handle)
                        candidate_source.assert_unchanged(expected_sha256=source.declared_sha256)
                        active_pdf_key = source_key
                        active_pdf_digest = (actual_digest, actual_size)
                        active_opened_source = candidate_source
                    assert active_pdf_digest is not None  # set with active_pdf_key
                    actual_digest, actual_size = active_pdf_digest
                except (OSError, inventory.SubmissionInputError) as error:
                    active_pdf.close()
                    _publish_refusal(context, source, RefusalReason.UNREADABLE, str(error))
                    continue
            else:
                try:
                    if cached_path == source.declared_path and cached_data is not None:
                        data = cached_data
                    else:
                        data = read_bytes(source.declared_path)
                        cached_path, cached_data = source.declared_path, data
                except (OSError, inventory.SubmissionInputError) as error:
                    _publish_refusal(context, source, RefusalReason.UNREADABLE, str(error))
                    continue
                actual_digest, actual_size = digest_bytes(data), len(data)

            mismatch = _source_mismatch(source, actual_digest, actual_size)
            if mismatch is not None:
                _publish_refusal(context, source, RefusalReason.DIGEST_MISMATCH, mismatch)
                continue

            opened_pdf = None
            if streamed_pdf:
                try:
                    if active_pdf_document is None:
                        assert active_opened_source is not None
                        active_pdf_document = pdf_render.open_document(active_opened_source.handle)
                        active_pdf.callback(pdf_render.close_document, active_pdf_document)
                    opened_pdf = active_pdf_document
                except pdf_render.PdfRefusal as error:
                    decision = _refused(str(error))
                except (OSError, inventory.SubmissionInputError) as error:
                    # Per-file: a descriptor lost here refuses this source, not
                    # every source after it.
                    decision = _refused_for(RefusalReason.UNREADABLE, str(error))
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
                _publish(context, source, outcome="refused", reason=decision.reason)
                continue

            if streamed_pdf:
                try:
                    assert active_opened_source is not None
                    active_opened_source.assert_unchanged(expected_sha256=source.declared_sha256)
                except inventory.SubmissionInputError as error:
                    _publish_refusal(context, source, RefusalReason.DIGEST_MISMATCH, str(error))
                    continue

            # Only admitted sources register: a corrupt twin gets its own
            # refusal, and a valid second path stays admitted with a duplicate fact.
            first = seen_sources.get(actual_digest)
            duplicate_of = None
            if first is not None and first[0] != source.declared_path:
                duplicate_of = {
                    "first_declared_path": first[0],
                    "first_ordinal": first[1],
                    "source_sha256": actual_digest,
                }
            seen_sources.setdefault(actual_digest, (source.declared_path, source.ordinal))
            _publish_admission(context, tree, source, decision, data, actual_digest, duplicate_of)
            admitted += 1

    return admitted


def _publish_admission(
    context: StageContext,
    tree: RunTree,
    source: SourceEntry,
    decision: _Decision,
    data: bytes | None,
    actual_digest: str,
    duplicate_of: dict[str, Any] | None,
) -> None:
    _, published = tree.put_blob(DOOR, decision.store_bytes)
    inputs = [context.input_ref(published.relative_path)]
    extra: dict[str, Any] = {
        "sha256": decision.digest,
        # The submitted file's digest; differs from `sha256` only for a
        # rendered page. Duplicate accounting groups on it because every
        # admission has one.
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
        # A no-op `keep` over a deterministic PNG makes derivative and
        # master one blob; envelope inputs may not repeat.
        if parent.relative_path != published.relative_path:
            inputs.append(context.input_ref(parent.relative_path))
    if duplicate_of is not None:
        extra["duplicate_of"] = duplicate_of
    _publish(context, source, outcome="admitted", payload_extra=extra, inputs=inputs)


def _source_mismatch(source: SourceEntry, actual_digest: str, actual_size: int) -> str | None:
    """Why the bytes read now are not the bytes this source was declared and sealed with."""
    if source.declared_size is not None and actual_size != source.declared_size:
        return (
            f"the source now has {actual_size} bytes, but {source.declared_size} bytes "
            "were recorded in its filename ledger"
        )
    if source.computed_sha256 is not None and actual_digest != source.computed_sha256:
        return (
            f"computed {actual_digest} at admission, but shard membership was "
            f"sealed from {source.computed_sha256} during source expansion"
        )
    if source.declared_sha256 is not None and actual_digest != source.declared_sha256:
        return f"computed {actual_digest}, but {source.declared_sha256} was declared"
    return None


def _publish_refusal(
    context: StageContext, source: SourceEntry, code: RefusalReason, detail: str
) -> None:
    _publish(context, source, outcome="refused", reason=admission.reason(code, detail))


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
        if not isinstance(parts, list) or not parts or not _is_int(source.triage_part_index):
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


class Report(NamedTuple):
    path: str
    payload: dict[str, Any]


def publish_refusal_report(context: StageContext) -> Report | None:
    """Seal every door refusal into one private, filename-bearing report.

    The admission artifacts stay the authority; this indexes them so filenames
    never reach terminal output.
    """
    rows: list[dict[str, Any]] = []
    inputs: list[dict[str, str]] = []
    for entry, payload in _iter_admissions(context, "refused"):
        # A free-text reason must not seal into the report.
        admission.reason_code(payload["reason"])
        rows.append({field: payload[field] for field in ("ordinal", "declared_path", "reason")})
        inputs.append(_entry_ref(entry))
    if not rows:
        return None
    payload = {
        "schema": DOOR_REFUSAL_REPORT_SCHEMA,
        "refusals": sorted(rows, key=lambda row: row["ordinal"]),
    }
    return _publish_report(
        context, "refusal-report", DOOR_REFUSAL_REPORT_SUBJECT, "refused", inputs, payload
    )


def publish_duplicate_report(context: StageContext) -> Report | None:
    """Seal the duplicate fact without refusing either source.

    Groups every admitted path sharing one submitted digest and names the first
    filename and ordinal, so no later stage rediscovers it from blobs.
    """
    grouped: dict[str, list[tuple[int, str, dict[str, str]]]] = {}
    for entry, payload in _iter_admissions(context, "admitted"):
        # Not the optional `declared_sha256`: every admission carries this digest.
        grouped.setdefault(payload["admitted_source_sha256"], []).append(
            (payload["ordinal"], payload["declared_path"], _entry_ref(entry))
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
        for path in ordered_paths:
            inputs.extend(reference for _ordinal, reference in by_path[path])

    if not groups:
        return None
    payload = {
        "schema": DOOR_DUPLICATE_REPORT_SCHEMA,
        "duplicate_source_count": duplicate_sources,
        "duplicate_ordinal_count": duplicate_ordinals,
        "groups": groups,
    }
    return _publish_report(
        context, "duplicate-report", DOOR_DUPLICATE_REPORT_SUBJECT, "admitted", inputs, payload
    )


def publish_cluster_report(context: StageContext) -> Report | None:
    """Carry corpus-scoped re-shoot links into the sealed run.

    No member is called canonical: every submitted member remains an admission.
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
            inputs.append(_entry_ref(entry))
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
    return _publish_report(
        context, "re-shoot-cluster-report", "re-shoot-cluster-report", "admitted", inputs, payload
    )


def _entry_ref(entry: dict[str, Any]) -> dict[str, str]:
    return {"relative_path": entry["relative_path"], "sha256": entry["sha256"]}


def _publish_report(
    context: StageContext,
    kind: str,
    subject_id: str,
    outcome: str,
    inputs: list[dict[str, str]],
    payload: dict[str, Any],
) -> Report:
    """Seal one self-hashed report and return where it was written, with its payload."""
    payload["self_hash"] = self_hash(payload)
    published = context.publish(
        kind=kind, subject_id=subject_id, outcome=outcome, inputs=inputs, payload=payload
    )
    return Report(published.relative_path, payload)


def require_no_duplicate_sources(duplicate_report: Report | None) -> None:
    """Refuse a submission in which two submitted files derive one page identity.

    Byte-identical files derive one `page_id`, but every later stage works one
    page per submitted row, so the run would read one page where two were submitted.

    The whole submission is refused, never one file, and there is no override
    flag: dropping a copy is an exclusion, which is the project lead's decision,
    and the bytes cannot tell a page shot twice from one scan exported twice.

    The error names ordinals only, since `run_stage` prints it to stderr and the
    data-handling policy keeps paths out of logs; the duplicate report sealed
    before this refusal names the files.

    Only identical submitted bytes are caught here. Different sources whose triage
    derivatives coincide are refused by `common/exemplar_boundary` at the first
    consumer, so page identity is derived in one place.
    """
    if duplicate_report is None:
        return
    named = "; ".join(
        " and ".join(", ".join(map(str, source["ordinals"])) for source in group["sources"])
        for group in duplicate_report.payload["groups"]
    )
    raise ContractError(
        "this submission derives one page identity from more than one submitted file: "
        f"submitted ordinal(s) {named} carry identical bytes. Byte-identical sources "
        "derive one page_id, so the Exemplar would seal one page citing every one of "
        "them while every stage behind it still works one page per submitted row, and "
        "the run would read one page where two files were submitted. Nothing is "
        "excluded here and nothing is dropped: the submission is refused whole, and "
        f"the sealed duplicate report at {duplicate_report.path} names each "
        "filename. Re-submit with a --submission-manifest naming each distinct scan "
        "once, or ask the project lead if a repeated scan is genuinely two pages"
    )


def require_confirmed_re_shoots(context: StageContext, cluster_report: Report | None) -> None:
    """Refuse a submission holding a triage re-shoot the corpus register does not confirm.

    Only a register membership tells later stages that captures show one page; without
    it each capture becomes its own act, and one physical act is read and exported once
    per capture with nothing linking them. A cluster is confirmed when every member sits
    in a current membership of some physical page of the cluster's own corpus: one
    cluster may span several pages with different members (a split opening), and the
    sealed cluster report carries no page ids to check page by page. The submission is
    refused whole before the seal, so no page is lost.
    """
    if cluster_report is None:
        return
    register = read_snapshot(context.tree, context.run)
    corpus_of = {
        record["physical_page_id"]: record["corpus_id"]
        for record in validate_register_bytes(register)["records"]
        if record["kind"] == "physical-page"
    }
    confirmed = {
        (corpus_of[page], capture)
        for page, (_digest, members) in membership_heads(register).items()
        for capture in members
    }
    unconfirmed = sorted(
        cluster["cluster_id"]
        for cluster in cluster_report.payload["clusters"]
        if any(
            (cluster["corpus_id"], member["source_frame_sha256"]) not in confirmed
            for member in cluster["members"]
        )
    )
    named = ", ".join(unconfirmed)
    if unconfirmed:
        raise ContractError(
            f"unconfirmed-re-shoot: triage links re-shoot cluster(s) {named}, but the corpus "
            "register this run was created with does not record every capture in them as a "
            "member of a physical page of that corpus, so each capture would be read and "
            "exported as a separate act. Nothing is sealed and no page is dropped: the "
            f"submission is refused whole, and the sealed cluster report at {cluster_report.path} "
            "names each member. Confirm the cluster into the corpus register (or remove the "
            "triage link if the captures are not one page), then resubmit under a new run id "
            "with --corpus-register; this run id stays bound to the register and triage "
            "inputs it was created with and refuses reuse"
        )


def require_some_admitted(admitted: int, refusal_report: Report | None) -> None:
    """An empty or wholly refused input set is a loud failure.

    The error carries counts and the private report location; only the report
    names files.
    """
    if admitted != 0:
        return
    if refusal_report is None:
        raise ContractError("the door admitted nothing: no source was submitted")
    census = _refusal_census(refusal_report)
    named = ", ".join(f"{code}: {count}" for code, count in sorted(census.items()))
    raise ContractError(
        f"the door admitted nothing: all {sum(census.values())} page ordinal(s) were "
        f"refused ({named}). Private named refusal report: {refusal_report.path}. "
        "An empty or wholly unreadable input set is a loud failure, never a green run with no "
        "output"
    )


def _refusal_census(refusal_report: Report) -> dict[str, int]:
    """Count the reported refusals by closed-set reason code."""
    census: dict[str, int] = {}
    for row in refusal_report.payload["refusals"]:
        code = admission.reason_code(row["reason"]).value
        census[code] = census.get(code, 0) + 1
    return census


def declared_synthetic_fixture_root(requested_root: str) -> Path:
    """The one root in this repository whose contents are declared synthetic.

    A caller pointing `--fixture-root` anywhere else is pointing at real input,
    whatever the folder is called.
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

    Tests inject a deterministic registry through this seam; no command-line
    option chooses among implementations, chairs, revisions, recipes or caches.
    """
    parser = stage_parser(DESCRIPTION)
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
        help="triage-decision-manifest-v1 controlling raster split/crop/rotation",
    )
    parser.add_argument(
        "--triage-clusters",
        help="corpus-scoped triage re-shoot cluster records keyed by cluster id",
    )
    parser.add_argument(
        "--triage-producer-recipe",
        help="sealed triage-producer-recipe.v1 for the pre-door producer run",
    )
    args = parser.parse_args()
    registry = (
        registry_factory(args.models_config, cache_root=args.cache_root)
        if args.cache_root is not None
        else registry_factory(args.models_config)
    )

    if args.submission_folder is not None:
        # The halted-run cap waits for the storage gate: reading `run.json` on an
        # unapproved volume is the read the gate forbids.
        return real_submission(args, registry)
    # The fixture path is not gated, so its cap applies first.
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

    On the real path, call it only after `run_root` passes the storage gate.
    """
    existing_tree = RunTree(run_root, args.run_id)
    if existing_tree.resolve("run.json").exists():
        refuse_halted_run(existing_tree, DOOR, args.hard_failure_config)


def _load_pdf_render_binding(args) -> render_config.PdfRenderBinding:
    """The one place a run's PDF target DPI is resolved, fixture or real.

    Settings and digest come from one read, so a rewrite cannot seal a digest
    for settings the run did not render with.
    """
    return render_config.load_pdf_render_binding(
        Path(args.pdf_render_config),
        target_override=args.pdf_target_dpi,
        minimum_dpi=pdf_render.MIN_RENDER_DPI,
    )


def _finish_door_run(context: StageContext, admitted: int) -> int:
    """The shared close for both entry points: reports, then the loud checks.

    Reports seal first so a refused run still leaves its evidence; both refusals
    fire before `seal_boundary` writes anything else.
    """
    refusal_report = publish_refusal_report(context)
    duplicate_report = publish_duplicate_report(context)
    cluster_report = publish_cluster_report(context)
    _announce_refusal_report(refusal_report)
    _announce_duplicate_report(duplicate_report)
    require_no_duplicate_sources(duplicate_report)
    require_confirmed_re_shoots(context, cluster_report)
    require_some_admitted(admitted, refusal_report)
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
        designator_grouping_config_path=args.designator_grouping_config,
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

    # The manifest carries declared digests, so a refusal matches what it was
    # refused against.
    tree = _create_run(
        args,
        Path(args.run_root),
        [
            {
                "relative_path": page["path"],
                "sha256": declared[page["ordinal"]],
                # The checked-in bytes' digest, bound separately so shard
                # membership never collapses to ordinals.
                "computed_sha256": page["sha256"],
                "ordinal": page["ordinal"],
            }
            for page in pages
        ],
        bindings,
        synthetic_fixture_ingress_record(),
        pdf_settings,
    )
    context = _door_context(tree, fixture, args.scenario, args, registry, bindings)
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
    return _finish_door_run(context, admitted)


def real_submission(args, registry) -> int:
    """Admit a local folder's bytes into a run once it sits in an approved location.

    Order matters: storage roots, inventory, run creation, then publication. A
    folder outside every approved root means nothing was read.
    """
    data_policy_binding = gate.load_policy_binding(Path(args.data_gate_policy))
    data_policy = data_policy_binding.policy
    if args.submission_manifest is None:
        raise ContractError(
            "a real submission requires --submission-manifest: the self-hashed filename "
            "ledger is how its copied bytes are matched back to the original set"
        )

    roots = gate.approved_storage_roots(data_policy)
    # Use the resolved paths from here on: checking one path and opening another
    # is a check-then-use race.
    submission_folder = gate.require_approved_storage_location(
        Path(args.submission_folder), roots, "submitted folder"
    )
    run_root = gate.require_approved_storage_location(Path(args.run_root), roots, "run root")
    manifest_path = gate.require_approved_storage_location(
        Path(args.submission_manifest), roots, "submission filename ledger"
    )
    _refuse_halted_run_root(run_root, args)
    _refuse_inside_submission(run_root, submission_folder, "run root")
    _refuse_inside_submission(manifest_path, submission_folder, "submission filename ledger")
    ledger = submission_ledger.load_manifest(manifest_path)
    if args.triage_clusters is not None and args.triage_decision_manifest is None:
        raise ContractError("triage cluster records require a triage decision manifest")
    if args.triage_producer_recipe is not None and args.triage_decision_manifest is None:
        raise ContractError("triage producer recipe requires a triage decision manifest")
    # Gated like the ledger: every real input must sit in an approved root, and a
    # record inside the submitted folder would be inventoried as a source.
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
        _refuse_inside_submission(resolved, submission_folder, label)
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
    # Inventory keeps no source bodies; later reads reopen by directory
    # descriptor, so digest and render see one file even if its name is replaced.
    # The ledger digests catch an in-place rewrite, which `fstat` alone cannot.
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
                # Bounded even if the untrusted ledger understates a file that
                # grew; `process_sources` refuses the size mismatch.
                data = opened_source.handle.read(MAX_SOURCE_BYTES + 1)
                opened_source.assert_unchanged(expected_sha256=ledger_digests.get(relative_path))
                return data
        except inventory.SubmissionInputError as error:
            # `process_sources` turns this into a per-source refusal.
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
        designator_padding_config_sha256=read_sealed_toml(
            args.designator_padding_config, "Designator padding configuration"
        )[1],
        designator_geometry_config_sha256=read_sealed_toml(
            args.designator_geometry_config, "Designator geometry configuration"
        )[1],
        designator_grouping_config_sha256=read_sealed_toml(
            args.designator_grouping_config, "Designator grouping configuration"
        )[1],
        alignment_config_path=args.alignment_config,
        serving_recipes_config_path=args.serving_recipes_config,
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
        mechanics_qualification=getattr(args, "mechanics_qualification", False),
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
    require_corpus_frame_shard(len(sources), bindings["sealed_config_digests"])
    tree = _create_run(
        args,
        run_root,
        [
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
        bindings,
        real_ingress_record(),
        pdf_settings,
    )
    context = _door_context(tree, None, REAL_SCENARIO, args, registry, bindings)
    context.require_sealed_config("pdf-render", pdf_render_binding.config_sha256)
    # So a reader can tell which data-handling policy admitted the corpus.
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
    return _finish_door_run(context, admitted)


def _refuse_inside_submission(location: Path, submission_folder: Path, label: str) -> None:
    if location.is_relative_to(submission_folder):
        raise ContractError(
            f"the {label} cannot live inside the submitted folder; otherwise the next "
            "inventory includes pipeline-produced records as submitted sources"
        )


def _read_corpus_register(register_path: str | None) -> bytes | None:
    """Read an optional register before run creation, with a recoverable refusal."""
    if register_path is None:
        return None
    try:
        return read_register_path(register_path)
    except (OSError, ContractError) as error:
        raise ContractError(
            "the corpus register could not be read before run creation; no run or admission "
            "record was written; provide a readable canonical register and retry; the file "
            "must be bounded, regular, and not a symlink"
        ) from error


def _announce_refusal_report(refusal_report: Report | None) -> None:
    """Give the terminal only a count and private report location, never a name."""
    if refusal_report is None:
        return
    print(
        f"{len(refusal_report.payload['refusals'])} door refusal(s); "
        f"private refusal report: {refusal_report.path}",
        file=sys.stderr,
    )


def _announce_duplicate_report(duplicate_report: Report | None) -> None:
    """Count duplicate sources in the operator summary without printing filenames."""
    if duplicate_report is None:
        return
    payload = duplicate_report.payload
    # "detected", not "admitted": the whole submission is refused two calls later.
    print(
        f"{payload['duplicate_source_count']} duplicate source(s) detected across "
        f"{payload['duplicate_ordinal_count']} page ordinal(s); "
        f"private duplicate report: {duplicate_report.path}",
        file=sys.stderr,
    )


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
    designator_grouping_config_sha256: str,
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
    mechanics_qualification: bool = False,
    serving_recipes_config_path: str | Path = DEFAULT_SERVING_RECIPES_CONFIG_PATH,
    pod_placement_config_path: str | Path = DEFAULT_POD_PLACEMENT_CONFIG_PATH,
) -> dict[str, Any]:
    """The sealed configuration facts for a real submission.

    The source manifest binds the bytes; `config_digest` binds everything else
    that shaped the door's output, so `RunTree.create` refuses a resume under
    different settings. The data-handling policy digest is provenance, not an
    approval.
    """
    validate_witness_adapter_bindings(models)
    # Bound as on the fixture path, so a changed serving catalogue changes `config_digest`.
    serving_recipes_config_digest = read_sealed_toml(
        serving_recipes_config_path, "serving recipes configuration"
    )[1]
    pod_placement_config_digest = read_sealed_toml(
        pod_placement_config_path, "pod placement configuration"
    )[1]
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
    perlector_protocol_config_sha256 = read_sealed_toml(
        perlector_protocol_config_path, "Perlector protocol configuration"
    )[1]
    perlector_audit_config_sha256 = read_sealed_toml(
        perlector_audit_config_path, "Perlector audit configuration"
    )[1]
    # The shared default that `require_triage_modes` also reads; a second
    # spelling could drift and refuse every triage run as "changed".
    triage_modes_config_sha256 = load_triage_modes(DEFAULT_TRIAGE_MODES_CONFIG_PATH)
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
                # Provenance, not a gate: which policy did the storage-root check.
                "data_handling_policy_sha256": data_handling_config_sha256,
                "door_execution_recipe": _door_execution_recipe(pdf_settings),
                "door_implementation_revision": REAL_DOOR_ADAPTER_REVISION,
                "armarium_formats_config_sha256": armarium_formats_digest,
                "armarium_formats": armarium_formats.to_record(),
                "recovery_policy": recovery_policy,
                "hard_failure_policy": hard_failure_policy,
                "designator_padding_config_sha256": designator_padding_config_sha256,
                "designator_geometry_config_sha256": designator_geometry_config_sha256,
                "designator_grouping_config_sha256": designator_grouping_config_sha256,
                "alignment_config_sha256": alignment_config_sha256,
                "triage_modes_config_sha256": triage_modes_config_sha256,
                # Triage decisions shape pixels, so a re-run triage pass under one
                # run id is refused by name. Empty without split decisions.
                "triage_document_digests": dict(sorted((triage_document_digests or {}).items())),
                "corpus_frame_policy": corpus_frame_policy,
                "corpus_frame_config_sha256": corpus_frame_config_sha256,
                "decoding_config_sha256": decoding_config_sha256,
                "models": models.to_record(),
                # Run-level witness settings, validated and bound as on the
                # fixture path, so a bad declaration refuses before any paid work.
                "witness_context_regime": witness_context,
                "witness_context_declaration_sha256": witness_context_declaration_sha256,
                "nuda_per_mille": nuda_per_mille,
                "nuda_approval_ref": nuda_approval_ref,
                "perlector_instrument_per_mille": perlector_instrument_per_mille,
                "perlector_instrument_approval_ref": perlector_instrument_approval_ref,
                "perlector_protocol_config_sha256": perlector_protocol_config_sha256,
                "perlector_audit_config_sha256": perlector_audit_config_sha256,
                "draft_fed": draft_fed,
                "serving_config_inputs": {
                    "schema": SERVING_CONFIG_INPUTS_SCHEMA,
                    "serving_recipes_sha256": serving_recipes_config_digest,
                    "pod_placement_sha256": pod_placement_config_digest,
                },
            }
        ),
        "adapter_recipes": adapter_recipes,
        # Named as on the fixture path, so point-of-use rechecks find them on
        # real runs too.
        "sealed_config_digests": {
            "designator-padding": designator_padding_config_sha256,
            "designator-geometry": designator_geometry_config_sha256,
            "designator-grouping": designator_grouping_config_sha256,
            "alignment": alignment_config_sha256,
            "corpus-frame-shard": corpus_frame_config_sha256,
            "decoding": decoding_config_sha256,
            "perlector-protocol": perlector_protocol_config_sha256,
            "perlector-audit": perlector_audit_config_sha256,
            "pdf-render": pdf_render_config_sha256,
            "recovery": recovery_policy["config_sha256"],
            "hard-failure": hard_failure_policy["config_sha256"],
            "triage-modes": triage_modes_config_sha256,
            # Real ingress only: the fixture route is not gated.
            "data-handling": data_handling_config_sha256,
            "serving-recipes": serving_recipes_config_digest,
            "pod-placement": pod_placement_config_digest,
            # Real ingress only: downstream stages cannot recompute the real
            # `config_digest` (it binds the ledger and this machine's decoder),
            # so the facts stages 3-7 act on are rechecked by these names at
            # every open.
            "models": models.models_digest,
            "armarium-formats": armarium_formats_digest,
            "run-policy": real_run_policy_digest(
                witness_context=witness_context,
                witness_context_declaration_sha256=witness_context_declaration_sha256,
                nuda_per_mille=nuda_per_mille,
                nuda_approval_ref=nuda_approval_ref,
                perlector_instrument_per_mille=perlector_instrument_per_mille,
                perlector_instrument_approval_ref=perlector_instrument_approval_ref,
                draft_fed=draft_fed,
                mechanics_qualification=mechanics_qualification,
            ),
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


def _create_run(
    args,
    run_root: Path,
    source_manifest: list[dict[str, Any]],
    bindings: dict[str, Any],
    ingress: dict[str, Any],
    pdf_settings: render_config.PdfRenderSettings,
) -> RunTree:
    """The Door creates the run because it is the first to know what arrived."""
    return RunTree.create(
        run_root,
        args.run_id,
        source_manifest=source_manifest,
        config_digest=bindings["config_digest"],
        adapter_recipes=bindings["adapter_recipes"],
        witness_chairs=bindings["witness_chairs"],
        ingress=ingress,
        render_settings={"pdf": pdf_settings.to_record()},
        sealed_config_digests=bindings["sealed_config_digests"],
        register_bytes=_read_corpus_register(args.corpus_register),
        # Only the Door creates the run authority, so only it can seal the commit;
        # None when the orchestrator could not measure it.
        repository_commit=args.repository_commit,
    )


def _door_context(
    tree: RunTree,
    fixture: dict | None,
    scenario: str,
    args,
    registry,
    bindings: dict[str, Any],
) -> StageContext:
    """The door's context carries the sealed digests to prove the policies it uses."""
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
        sealed_config_digests=bindings["sealed_config_digests"],
    )


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
